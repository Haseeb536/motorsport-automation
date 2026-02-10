#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a merged YMM + SKU table from:
  1. Live Google Sheet (products + options tabs) — SKUs with fitment columns
  2. duplicate solution/07_products_for_upload.csv — duplicate SKUs + sheet fitment
  3. combined_final_cleaned.csv (prior WCPE + export merge)
  4. WCPE JT-Products.eu - Blad1.csv — adds only SKUs not already on each vehicle row

Writes:
  - combined_final_merged.csv (local)
  - New tab on the MotorSports_updated spreadsheet (default: combined_final_merged)

Sheet: configure GOOGLE_SPREADSHEET_ID in your .env file.
Options tab gid: 1688032297

Usage:
  python merge_google_sheet_with_combined_final.py
  python merge_google_sheet_with_combined_final.py --dry-run
  python merge_google_sheet_with_combined_final.py --no-upload
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import random
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError as GSpreadAPIError

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLEANED_CSV = os.path.join(SCRIPT_DIR, "combined_final_cleaned.csv")
DEFAULT_OUTPUT_CSV = os.path.join(SCRIPT_DIR, "combined_final_merged.csv")
DUPLICATE_SOLUTION_DIR = os.path.join(SCRIPT_DIR, "duplicate solution")
DEFAULT_DUPLICATE_PRODUCTS_CSV = os.path.join(
    DUPLICATE_SOLUTION_DIR, "07_products_for_upload.csv"
)
DUPLICATE_PRODUCTS_FALLBACK_CSV = os.path.join(
    DUPLICATE_SOLUTION_DIR, "05_final_ready.csv"
)
DEFAULT_WCPE_CSV = os.path.join(SCRIPT_DIR, "WCPE JT-Products.eu - Blad1.csv")

SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
OPTIONS_WORKSHEET_GID = 1688032297
SHEET_PRODUCTS = "products"
SHEET_OPTIONS = "options"
DEFAULT_OUTPUT_WORKSHEET = "combined_final_merged"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SERVICE_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "credentials.json")

OUTPUT_COLUMNS = ["Merk", "Model", "Modelcode", "Uitvoering", "SKU"]
SKU_TO_STRIP = "vwr120000"  # same rule as ymm_sheet_cleaner.py


def safe_gs_call(func, *args, max_retries=6, base_delay=1.25, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (HttpError, GSpreadAPIError) as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            msg = str(e).lower()
            retryable = status in (429, 500, 503) or "quota" in msg or (
                "rate" in msg and "limit" in msg
            )
            if not retryable or attempt >= max_retries - 1:
                raise
            time.sleep((base_delay * (2**attempt)) + random.uniform(0.0, 0.6))


def open_worksheet_by_gid(client: gspread.Client, spreadsheet_id: str, gid: int):
    spreadsheet = safe_gs_call(client.open_by_key, spreadsheet_id)
    for ws in spreadsheet.worksheets():
        ws_id = getattr(ws, "id", None)
        if ws_id is None:
            try:
                ws_id = ws._properties.get("sheetId")
            except Exception:
                ws_id = None
        if ws_id is not None and int(ws_id) == int(gid):
            return spreadsheet, ws
    raise ValueError(
        f"No worksheet with gid={gid} in spreadsheet {spreadsheet_id}. "
        f"Use tab name '{SHEET_OPTIONS}' if gid changed."
    )


def sheet_to_dataframe(worksheet: gspread.Worksheet) -> pd.DataFrame:
    values = safe_gs_call(worksheet.get_all_values)
    if not values:
        return pd.DataFrame()
    headers = [str(h).strip() for h in values[0]]
    rows = values[1:]
    df = pd.DataFrame(rows, columns=headers)
    return df.fillna("").astype(str)


def normalize_sku(value: str) -> str:
    return str(value or "").strip()


def read_google_sheet_products_options(
    client: gspread.Client,
) -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Load products + options tabs from MotorSports_updated."""
    spreadsheet = safe_gs_call(client.open_by_key, SPREADSHEET_ID)
    products_ws = safe_gs_call(spreadsheet.worksheet, SHEET_PRODUCTS)
    try:
        _, options_ws = open_worksheet_by_gid(client, SPREADSHEET_ID, OPTIONS_WORKSHEET_GID)
    except ValueError:
        options_ws = safe_gs_call(spreadsheet.worksheet, SHEET_OPTIONS)

    products_df = sheet_to_dataframe(products_ws)
    options_df = sheet_to_dataframe(options_ws)

    stats = {
        "products_rows": len(products_df),
        "options_rows": len(options_df),
    }

    for col in ("product_id", "Reference"):
        if col not in products_df.columns:
            raise ValueError(
                f"products tab missing column '{col}'. Headers: {list(products_df.columns)[:15]}"
            )
    for col in ("product_id", "Brand", "Model", "Type", "Version"):
        if col not in options_df.columns:
            raise ValueError(
                f"options tab missing column '{col}'. Headers: {list(options_df.columns)[:15]}"
            )

    products_df = products_df[["product_id", "Reference"]].copy()
    options_df = options_df[["product_id", "Brand", "Model", "Type", "Version"]].copy()

    products_df["product_id"] = products_df["product_id"].astype(str).str.strip()
    options_df["product_id"] = options_df["product_id"].astype(str).str.strip()
    products_df["Reference"] = products_df["Reference"].map(normalize_sku)
    for col in ("Brand", "Model", "Type", "Version"):
        options_df[col] = options_df[col].astype(str).str.strip()

    return products_df, options_df, stats


def aggregate_ymm_products_options(
    products_df: pd.DataFrame,
    options_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, int]:
    """Join fitment options with product SKUs; one row per vehicle with combined SKU list."""
    merged = pd.merge(options_df, products_df, on="product_id", how="inner")
    joined_rows = len(merged)

    merged = merged.rename(
        columns={
            "Brand": "Merk",
            "Type": "Modelcode",
            "Version": "Uitvoering",
        }
    )

    def first_nonempty_merk(series: pd.Series) -> str:
        for val in series:
            s = str(val).strip()
            if s:
                return s
        return ""

    merged["Merk"] = merged.groupby(
        ["Model", "Modelcode", "Uitvoering"], group_keys=False
    )["Merk"].transform(first_nonempty_merk)

    def join_unique_skus(series: pd.Series) -> str:
        seen: List[str] = []
        for ref in series:
            ref = normalize_sku(ref)
            if ref and ref not in seen:
                seen.append(ref)
        return ", ".join(sorted(seen, key=lambda s: s.lower()))

    result = (
        merged.groupby(["Merk", "Model", "Modelcode", "Uitvoering"], as_index=False)["Reference"]
        .agg(join_unique_skus)
        .rename(columns={"Reference": "SKU"})
    )
    return result, joined_rows


def ymm_rows_from_google_sheet(
    client: gspread.Client,
) -> Tuple[pd.DataFrame, dict, pd.DataFrame]:
    """Main catalog: sheet products + options."""
    products_df, options_df, stats = read_google_sheet_products_options(client)
    products_df = products_df.drop_duplicates(subset=["product_id"])
    result, joined = aggregate_ymm_products_options(products_df, options_df)
    stats["joined_rows"] = joined
    stats["unique_vehicles"] = len(result)
    return result, stats, options_df


def load_duplicate_products_csv(path: str) -> pd.DataFrame:
    """Load duplicate-solution product rows (Product_ID + Reference / SKU)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Duplicate products CSV not found: {path}")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]

    pid_col = None
    for name in ("Product_ID", "product_id", "Product_id"):
        if name in df.columns:
            pid_col = name
            break
    if not pid_col:
        raise ValueError(
            f"Duplicate products file needs Product_ID column. Headers: {list(df.columns)[:20]}"
        )

    ref_col = None
    for name in ("Reference", "Shopify_SKU", "SKU"):
        if name in df.columns:
            ref_col = name
            break
    if not ref_col:
        raise ValueError(
            f"Duplicate products file needs Reference column. Headers: {list(df.columns)[:20]}"
        )

    out = df[[pid_col, ref_col]].rename(
        columns={pid_col: "product_id", ref_col: "Reference"}
    )
    out["product_id"] = out["product_id"].astype(str).str.strip()
    out["Reference"] = out["Reference"].map(normalize_sku)
    out = out[out["product_id"].astype(bool) & out["Reference"].astype(bool)]
    return out


def resolve_duplicate_products_path(path: Optional[str]) -> str:
    if path and os.path.isfile(path):
        return path
    if os.path.isfile(DEFAULT_DUPLICATE_PRODUCTS_CSV):
        return DEFAULT_DUPLICATE_PRODUCTS_CSV
    if os.path.isfile(DUPLICATE_PRODUCTS_FALLBACK_CSV):
        return DUPLICATE_PRODUCTS_FALLBACK_CSV
    raise FileNotFoundError(
        "No duplicate products CSV found. Expected:\n"
        f"  {DEFAULT_DUPLICATE_PRODUCTS_CSV}\n"
        f"  {DUPLICATE_PRODUCTS_FALLBACK_CSV}"
    )


def ymm_rows_from_duplicate_solution(
    options_df: pd.DataFrame,
    duplicate_products_path: str,
) -> Tuple[pd.DataFrame, dict]:
    """
    Duplicate pipeline SKUs with fitment from the same Google Sheet options tab.

    Each row in duplicate solution/07_products_for_upload.csv has its own Reference
    (e.g. #SVWX036-1_1). Fitment (Brand/Model/Type/Version) comes from options rows
    matching that product_id.
    """
    dup_products = load_duplicate_products_csv(duplicate_products_path)
    dup_ids: Set[str] = set(dup_products["product_id"].unique())

    options_sub = options_df[options_df["product_id"].isin(dup_ids)].copy()
    result, joined = aggregate_ymm_products_options(dup_products, options_sub)

    stats = {
        "duplicate_product_rows": len(dup_products),
        "duplicate_product_ids": len(dup_ids),
        "options_rows_matched": len(options_sub),
        "joined_rows": joined,
        "unique_vehicles": len(result),
        "source_file": duplicate_products_path,
    }
    return result, stats


def ymm_vehicle_key(row: Dict[str, str]) -> Tuple[str, str, str, str]:
    return (
        row.get("Merk", "").strip(),
        row.get("Model", "").strip(),
        row.get("Modelcode", "").strip(),
        row.get("Uitvoering", "").strip(),
    )


def sku_dedup_key(sku: str) -> str:
    """Normalize SKU for duplicate checks (# prefix, case)."""
    return str(sku or "").strip().lstrip("#").lower()


def parse_sku_list(sku_field: str) -> List[str]:
    return [p.strip() for p in str(sku_field or "").split(",") if p.strip()]


def _parse_combined_ymm_line(line: str) -> Optional[Dict[str, str]]:
    """Parse one CSV line into Merk/Model/Modelcode/Uitvoering/SKU columns."""
    reader = csv.reader(io.StringIO(line))
    parts = next(reader, None)
    if not parts or len(parts) < 5:
        return None
    return dict(zip(OUTPUT_COLUMNS, [p.strip() for p in parts[:5]]))


def _fix_single_column_ymm_row(raw: Dict[str, str]) -> Dict[str, str]:
    """
    WCPE exports sometimes become one column:
      'Merk,Model,Modelcode,Uitvoering,SKU' -> 'Abarth / Fiat,595 / 695,...'
    """
    if len(raw) != 1:
        return raw
    header_key, value = next(iter(raw.items()))
    if "Merk" in header_key and "SKU" in header_key:
        parsed = _parse_combined_ymm_line(value)
        if parsed:
            return parsed
    return raw


def read_ymm_csv_line_by_line(path: str, encoding: str = "utf-8-sig") -> List[Dict[str, str]]:
    """Read WCPE-style files where each physical line is one quoted CSV record."""
    rows: List[Dict[str, str]] = []
    with open(path, "r", encoding=encoding, newline="") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            if line.startswith('"') and line.endswith('"'):
                line = line[1:-1].replace('""', '"')
            if line.lower().startswith("merk,model,"):
                continue
            parsed = _parse_combined_ymm_line(line)
            if parsed:
                rows.append(parsed)
    return rows


def read_ymm_csv_robust(path: str) -> List[Dict[str, str]]:
    """Read YMM CSV (handles WCPE export quoting via csv.Sniffer)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV not found: {path}")

    def _read(encoding: str) -> List[Dict[str, str]]:
        data: List[Dict[str, str]] = []
        with open(path, "r", encoding=encoding, newline="") as file:
            sample = file.read(4096)
            file.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
                reader = csv.DictReader(file, dialect=dialect)
            except csv.Error:
                file.seek(0)
                reader = csv.DictReader(file)
            if not reader.fieldnames:
                return data
            reader.fieldnames = [str(h).strip() for h in reader.fieldnames]
            for raw in reader:
                cleaned = {
                    str(k).strip(): (str(v).strip() if v is not None else "")
                    for k, v in raw.items()
                    if k is not None
                }
                cleaned = _fix_single_column_ymm_row(cleaned)
                data.append(cleaned)
        return data

    try:
        raw_rows = _read("utf-8-sig")
    except UnicodeDecodeError:
        raw_rows = _read("latin-1")

    rows: List[Dict[str, str]] = []
    for raw in raw_rows:
        row = {col: "" for col in OUTPUT_COLUMNS}
        for col in OUTPUT_COLUMNS:
            if col in raw:
                row[col] = raw[col]
        if any(row.values()):
            rows.append(row)

    if not rows:
        try:
            rows = read_ymm_csv_line_by_line(path, "utf-8-sig")
        except UnicodeDecodeError:
            rows = read_ymm_csv_line_by_line(path, "latin-1")
    return rows


def load_ymm_csv(path: str) -> List[Dict[str, str]]:
    return read_ymm_csv_robust(path)


def load_wcpe_csv(path: str) -> List[Dict[str, str]]:
    rows = read_ymm_csv_robust(path)
    if not rows:
        raise ValueError(f"No data rows in WCPE file: {path}")
    missing = [c for c in OUTPUT_COLUMNS if c not in rows[0]]
    if missing and not all(rows[0].get(c) for c in OUTPUT_COLUMNS):
        first_keys = list(rows[0].keys())
        if len(first_keys) == 1 and "," in first_keys[0]:
            raise ValueError(
                f"WCPE CSV columns not parsed correctly. Re-export with headers: "
                f"{', '.join(OUTPUT_COLUMNS)}"
            )
    return rows


def add_missing_skus_from_source(
    base_rows: List[Dict[str, str]],
    addition_rows: List[Dict[str, str]],
    source_label: str,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """
    Add SKUs from addition_rows onto matching vehicle keys in base_rows.

    Only SKUs not already present (case-insensitive, ignores leading #) are added.
    New vehicle configurations from additions are appended.
    """
    by_key: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
    seen_skus: Dict[Tuple[str, str, str, str], Set[str]] = {}

    for row in base_rows:
        key = ymm_vehicle_key(row)
        by_key[key] = {col: row.get(col, "").strip() for col in OUTPUT_COLUMNS}
        seen_skus[key] = {sku_dedup_key(s) for s in parse_sku_list(row.get("SKU", ""))}

    skus_added = 0
    new_vehicles = 0

    for row in addition_rows:
        key = ymm_vehicle_key(row)
        if not any(key):
            continue

        if key not in by_key:
            by_key[key] = {col: row.get(col, "").strip() for col in OUTPUT_COLUMNS}
            seen_skus[key] = set()
            new_vehicles += 1

        existing = seen_skus[key]
        to_append: List[str] = []
        for sku in parse_sku_list(row.get("SKU", "")):
            dk = sku_dedup_key(sku)
            if not dk or dk in existing:
                continue
            existing.add(dk)
            to_append.append(sku)
            skus_added += 1

        if to_append:
            current = parse_sku_list(by_key[key].get("SKU", ""))
            by_key[key]["SKU"] = ", ".join(current + to_append)

    result = list(by_key.values())
    result.sort(key=lambda r: (r["Merk"], r["Model"], r["Modelcode"], r["Uitvoering"]))
    stats = {
        "source": source_label,
        "skus_added": skus_added,
        "new_vehicle_rows": new_vehicles,
        "total_rows": len(result),
    }
    return result, stats


def dataframe_to_row_dicts(df: pd.DataFrame) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for _, series in df.iterrows():
        rows.append({col: str(series.get(col, "")).strip() for col in OUTPUT_COLUMNS})
    return rows


def merge_ymm_sources(
    sources: List[Tuple[str, List[Dict[str, str]]]],
) -> List[Dict[str, str]]:
    """Merge multiple YMM datasets; combine SKU lists per vehicle key."""
    all_rows: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
    sku_buckets: Dict[Tuple[str, str, str, str], List[str]] = defaultdict(list)

    seen_per_key: Dict[Tuple[str, str, str, str], Set[str]] = defaultdict(set)

    def collect(data: List[Dict[str, str]]) -> None:
        for row in data:
            key = ymm_vehicle_key(row)
            if key not in all_rows:
                all_rows[key] = {
                    "Merk": key[0],
                    "Model": key[1],
                    "Modelcode": key[2],
                    "Uitvoering": key[3],
                    "SKU": "",
                }
            for part in parse_sku_list(row.get("SKU", "")):
                dk = sku_dedup_key(part)
                if not dk or dk in seen_per_key[key]:
                    continue
                seen_per_key[key].add(dk)
                sku_buckets[key].append(part)

    for _name, data in sources:
        collect(data)

    combined: List[Dict[str, str]] = []
    for key, row_data in all_rows.items():
        row_data["SKU"] = ", ".join(sku_buckets.get(key, []))
        combined.append(row_data)

    combined.sort(key=lambda r: (r["Merk"], r["Model"], r["Modelcode"], r["Uitvoering"]))
    return combined


def strip_sku_from_rows(rows: List[Dict[str, str]], sku: str) -> Tuple[List[Dict[str, str]], int, int]:
    """Remove a SKU from all rows; drop rows with empty SKU. Returns (rows, modified, deleted)."""
    target = sku.lower()
    out: List[Dict[str, str]] = []
    modified = 0
    deleted = 0
    for row in rows:
        parts = [p.strip() for p in row.get("SKU", "").split(",") if p.strip()]
        new_parts = [p for p in parts if p.lower() != target]
        if len(new_parts) != len(parts):
            modified += 1
        if not new_parts:
            deleted += 1
            continue
        row = dict(row)
        row["SKU"] = ", ".join(new_parts)
        out.append(row)
    return out, modified, deleted


def write_csv(path: str, rows: List[Dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def upload_to_worksheet(
    client: gspread.Client,
    worksheet_name: str,
    rows: List[Dict[str, str]],
) -> str:
    spreadsheet = safe_gs_call(client.open_by_key, SPREADSHEET_ID)
    try:
        ws = safe_gs_call(spreadsheet.worksheet, worksheet_name)
    except gspread.WorksheetNotFound:
        ws = safe_gs_call(
            spreadsheet.add_worksheet,
            title=worksheet_name,
            rows=max(len(rows) + 10, 100),
            cols=len(OUTPUT_COLUMNS) + 2,
        )

    payload = [OUTPUT_COLUMNS] + [[row[c] for c in OUTPUT_COLUMNS] for row in rows]
    safe_gs_call(ws.clear)
    safe_gs_call(ws.update, payload, "A1", value_input_option="RAW")

    url = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        f"#gid={ws.id}"
    )
    return url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge live Google Sheet fitment/SKUs with combined_final_cleaned.csv.",
    )
    parser.add_argument(
        "--cleaned-csv",
        default=DEFAULT_CLEANED_CSV,
        help=f"Path to combined_final_cleaned.csv (default: {DEFAULT_CLEANED_CSV})",
    )
    parser.add_argument(
        "--output-csv",
        default=DEFAULT_OUTPUT_CSV,
        help=f"Local output CSV (default: {DEFAULT_OUTPUT_CSV})",
    )
    parser.add_argument(
        "--worksheet-name",
        default=DEFAULT_OUTPUT_WORKSHEET,
        help=f"New tab name on the spreadsheet (default: {DEFAULT_OUTPUT_WORKSHEET})",
    )
    parser.add_argument(
        "--strip-sku",
        default=SKU_TO_STRIP,
        help=f"Remove this SKU from merged output (default: {SKU_TO_STRIP}); use '' to skip",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build merge in memory only; do not write CSV or Google Sheet",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Write local CSV only; do not create/update the Google Sheet tab",
    )
    parser.add_argument(
        "--duplicate-products",
        default=None,
        help=(
            "CSV with duplicate Product_ID + Reference "
            f"(default: {DEFAULT_DUPLICATE_PRODUCTS_CSV})"
        ),
    )
    parser.add_argument(
        "--no-duplicate",
        action="store_true",
        help="Skip duplicate solution product SKUs",
    )
    parser.add_argument(
        "--wcpe-csv",
        default=DEFAULT_WCPE_CSV,
        help=f"WCPE master YMM file (default: {DEFAULT_WCPE_CSV})",
    )
    parser.add_argument(
        "--no-wcpe",
        action="store_true",
        help="Do not add SKUs from WCPE CSV",
    )
    args = parser.parse_args()

    if not os.path.isfile(SERVICE_ACCOUNT_FILE):
        print(f"Missing credentials: {SERVICE_ACCOUNT_FILE}")
        return 1

    print("=" * 70)
    print("Merge Google Sheet fitment + combined_final_cleaned.csv")
    print("=" * 70)
    print(f"Spreadsheet: {SPREADSHEET_ID}")
    print(f"Options gid: {OPTIONS_WORKSHEET_GID}")
    print(f"Cleaned CSV: {args.cleaned_csv}")
    print(f"Output CSV:  {args.output_csv}")
    print(f"Sheet tab:   {args.worksheet_name}")
    print(f"Mode:        {'DRY-RUN' if args.dry_run else 'APPLY'}")
    print()

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)

    print("Reading products + options from Google Sheet...")
    sheet_df, sheet_stats, options_df = ymm_rows_from_google_sheet(client)
    sheet_rows = dataframe_to_row_dicts(sheet_df)
    print(
        f"  products rows: {sheet_stats['products_rows']}, "
        f"options rows: {sheet_stats['options_rows']}, "
        f"joined: {sheet_stats['joined_rows']}, "
        f"unique vehicles: {sheet_stats['unique_vehicles']}"
    )

    merge_sources: List[Tuple[str, List[Dict[str, str]]]] = [
        ("google_sheet", sheet_rows),
    ]

    if not args.no_duplicate:
        dup_path = resolve_duplicate_products_path(args.duplicate_products)
        print(f"Reading duplicate solution products: {dup_path}")
        dup_df, dup_stats = ymm_rows_from_duplicate_solution(options_df, dup_path)
        dup_rows = dataframe_to_row_dicts(dup_df)
        print(
            f"  product rows: {dup_stats['duplicate_product_rows']}, "
            f"product ids: {dup_stats['duplicate_product_ids']}, "
            f"options matched: {dup_stats['options_rows_matched']}, "
            f"joined: {dup_stats['joined_rows']}, "
            f"unique vehicles: {dup_stats['unique_vehicles']}"
        )
        if dup_rows:
            merge_sources.append(("duplicate_solution", dup_rows))
        else:
            print("  (no duplicate YMM rows to merge)")

    print(f"Reading {args.cleaned_csv}...")
    cleaned_rows = load_ymm_csv(args.cleaned_csv)
    print(f"  rows: {len(cleaned_rows)}")
    merge_sources.append(("combined_final_cleaned", cleaned_rows))

    merged = merge_ymm_sources(merge_sources)
    print(f"Merged base (before WCPE top-up): {len(merged)} unique vehicle rows")

    if not args.no_wcpe:
        if not os.path.isfile(args.wcpe_csv):
            print(f"WARNING: WCPE file not found: {args.wcpe_csv}")
        else:
            print(f"Adding missing SKUs from WCPE: {args.wcpe_csv}")
            wcpe_rows = load_wcpe_csv(args.wcpe_csv)
            print(f"  WCPE rows: {len(wcpe_rows)}")
            merged, wcpe_stats = add_missing_skus_from_source(
                merged,
                wcpe_rows,
                "WCPE JT-Products.eu",
            )
            print(
                f"  SKUs added (not already present): {wcpe_stats['skus_added']}, "
                f"new vehicle rows: {wcpe_stats['new_vehicle_rows']}, "
                f"total rows: {wcpe_stats['total_rows']}"
            )

    print(f"Final row count (before SKU strip): {len(merged)}")

    if args.strip_sku:
        merged, modified, deleted = strip_sku_from_rows(merged, args.strip_sku)
        print(
            f"After removing SKU '{args.strip_sku}': {len(merged)} rows "
            f"({modified} modified, {deleted} dropped)"
        )

    if args.dry_run:
        print("\nDRY-RUN — sample rows:")
        for row in merged[:5]:
            sku_preview = row["SKU"][:80] + ("..." if len(row["SKU"]) > 80 else "")
            print(
                f"  {row['Merk']} | {row['Model']} | {row['Modelcode']} | "
                f"{row['Uitvoering']} | {sku_preview}"
            )
        print(f"\nWould write {len(merged)} rows to CSV and tab '{args.worksheet_name}'")
        return 0

    write_csv(args.output_csv, merged)
    print(f"\nWrote local CSV: {args.output_csv} ({len(merged)} rows)")

    if not args.no_upload:
        print(f"Uploading to Google Sheet tab '{args.worksheet_name}'...")
        url = upload_to_worksheet(client, args.worksheet_name, merged)
        print(f"Sheet URL: {url}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
