#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import os
import re
import sys
import time
import argparse
from datetime import datetime
from typing import List, Tuple, Optional, Dict

import os

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


SRC_SHEET_URL = os.environ.get("GOOGLE_SRC_SHEET_URL", "")
DST_SHEET_URL = os.environ.get("GOOGLE_DST_SHEET_URL", "")


def _extract_sheet_id_and_gid(url: str) -> Tuple[str, int]:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError(f"Could not extract spreadsheet id from url: {url}")
    sheet_id = m.group(1)

    mg = re.search(r"[?#&]gid=(\d+)", url)
    if not mg:
        raise ValueError(f"Could not extract gid from url: {url}")
    gid = int(mg.group(1))

    return sheet_id, gid


def _get_client(project_dir: str) -> gspread.Client:
    credentials_path = os.path.join(project_dir, "credentials.json")
    if not os.path.exists(credentials_path):
        raise FileNotFoundError(f"credentials.json not found at: {credentials_path}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    return gspread.authorize(creds)


def _get_drive_service(project_dir: str):
    credentials_path = os.path.join(project_dir, "credentials.json")
    scopes = [
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _open_worksheet_by_gid(client: gspread.Client, spreadsheet_id: str, gid: int) -> gspread.Worksheet:
    ss = client.open_by_key(spreadsheet_id)
    for ws in ss.worksheets():
        # gspread Worksheet has both id and _properties['sheetId'] depending on version.
        ws_id = getattr(ws, "id", None)
        if ws_id is None:
            try:
                ws_id = ws._properties.get("sheetId")
            except Exception:
                ws_id = None
        if int(ws_id) == int(gid):
            return ws
    raise ValueError(f"No worksheet with gid={gid} found in spreadsheet {spreadsheet_id}")


def _normalize_row(row: List[str]) -> Tuple[str, ...]:
    # Compare complete rows, but ignore trailing empty cells that are usually formatting noise.
    trimmed = list(row)
    while trimmed and str(trimmed[-1]).strip() == "":
        trimmed.pop()
    return tuple(str(c).strip() for c in trimmed)


def _index_by_header(header: List[str]) -> Dict[str, int]:
    return {str(name).strip(): i for i, name in enumerate(header)}


def _project_row_to_headers(row: List[str], header_index: Dict[str, int], headers: List[str]) -> List[str]:
    out: List[str] = []
    for h in headers:
        idx = header_index.get(h)
        out.append(row[idx] if (idx is not None and idx < len(row)) else "")
    return out


def _find_header_index(header: List[str], name: str) -> Optional[int]:
    target = str(name).strip().lower()
    for i, h in enumerate(header):
        if str(h).strip().lower() == target:
            return i
    return None


def _find_header_index_any(header: List[str], candidates: List[str]) -> Optional[int]:
    for c in candidates:
        idx = _find_header_index(header, c)
        if idx is not None:
            return idx
    return None


def _read_all_rows(ws: gspread.Worksheet) -> List[List[str]]:
    values = ws.get_all_values()
    return values if values else []


def _write_csv(output_path: str, header: List[str], rows: List[List[str]]) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if header:
            w.writerow(header)
        for r in rows:
            w.writerow(r)


def _upload_to_same_drive_folder_as_sheet(project_dir: str, destination_spreadsheet_id: str, local_csv_path: str) -> Optional[str]:
    """Upload CSV to the same Google Drive folder that contains the destination spreadsheet.

    Returns the uploaded file id, or None if upload fails.
    """
    drive = _get_drive_service(project_dir)

    meta = (
        drive.files()
        .get(fileId=destination_spreadsheet_id, fields="parents", supportsAllDrives=True)
        .execute()
    )
    parents = meta.get("parents") or []
    if not parents:
        return None

    parent_folder_id = parents[0]

    file_name = os.path.basename(local_csv_path)
    file_metadata = {
        "name": file_name,
        "parents": [parent_folder_id],
        "mimeType": "text/csv",
    }

    # googleapiclient.http.MediaFileUpload imported lazily to keep imports minimal
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(local_csv_path, mimetype="text/csv", resumable=False)
    created = (
        drive.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return created.get("id")


def main() -> int:
    project_dir = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(description="Compare complete rows between two Google Sheets tabs and export missing rows to CSV")
    parser.add_argument("--src", default=SRC_SHEET_URL, help="Source sheet URL (must include gid=...)")
    parser.add_argument("--dst", default=DST_SHEET_URL, help="Destination sheet URL (must include gid=...)")
    parser.add_argument(
        "--common-columns",
        action="store_true",
        help="If headers differ, compare rows using only the intersection of column headers (still compares full rows across those columns).",
    )
    parser.add_argument(
        "--compare-duplicates",
        action="store_true",
        help="Compare by a key column (e.g. SKU) and export extra source rows for keys where source has more occurrences than destination.",
    )
    parser.add_argument(
        "--src-only-duplicates",
        action="store_true",
        help="Only scan the source sheet and export all rows whose key (SKU) appears more than once.",
    )
    parser.add_argument(
        "--key-column",
        default="Reference",
        help="Column name to use as the key when using --compare-duplicates (default: Reference).",
    )
    args = parser.parse_args()

    # If no mode flags were specified, default to source-only duplicate export.
    if not (args.common_columns or args.compare_duplicates or args.src_only_duplicates):
        args.src_only_duplicates = True

    src_sheet_id, src_gid = _extract_sheet_id_and_gid(args.src)
    dst_sheet_id, dst_gid = _extract_sheet_id_and_gid(args.dst)

    client = _get_client(project_dir)

    print(f"[INFO] Source spreadsheet: {src_sheet_id} (gid={src_gid})")
    src_ws = _open_worksheet_by_gid(client, src_sheet_id, src_gid)
    print(f"[INFO] Source tab: {src_ws.title}")
    src_values = _read_all_rows(src_ws)

    dst_values: List[List[str]] = []
    dst_sheet_id, dst_gid = _extract_sheet_id_and_gid(args.dst)
    if args.compare_duplicates or args.common_columns:
        print(f"[INFO] Destination spreadsheet: {dst_sheet_id} (gid={dst_gid})")
        dst_ws = _open_worksheet_by_gid(client, dst_sheet_id, dst_gid)
        print(f"[INFO] Destination tab: {dst_ws.title}")
        dst_values = _read_all_rows(dst_ws)

    if not src_values:
        print("[WARN] Source worksheet is empty")
        return 0

    src_header = src_values[0]
    src_rows = src_values[1:]

    dst_header = dst_values[0] if dst_values else []
    dst_rows = dst_values[1:] if dst_values else []

    # Mode: only scan source and export all duplicate key rows.
    if args.src_only_duplicates:
        key_idx = _find_header_index(src_header, args.key_column)
        if key_idx is None:
            key_idx = _find_header_index_any(src_header, ["sku", "reference", "ref", "shopify_sku", "variant_sku"])
        if key_idx is None:
            print(f"[ERROR] Could not find key column in source header. Tried '{args.key_column}' plus common SKU column names.")
            return 2

        def _key_from_row(row: List[str]) -> str:
            if key_idx >= len(row):
                return ""
            return str(row[key_idx]).strip().lstrip("#").strip().lower()

        counts: Dict[str, int] = {}
        for r in src_rows:
            k = _key_from_row(r)
            if not k:
                continue
            counts[k] = counts.get(k, 0) + 1

        duplicate_keys = {k for k, c in counts.items() if c > 1}
        duplicate_rows: List[List[str]] = []
        for r in src_rows:
            k = _key_from_row(r)
            if k and k in duplicate_keys:
                duplicate_rows.append(r)

        print(f"[RESULT] Source data rows: {len(src_rows)}")
        print(f"[RESULT] Duplicate keys found: {len(duplicate_keys)}")
        print(f"[RESULT] Rows with duplicate key: {len(duplicate_rows)}")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = f"source_duplicate_{args.key_column.lower()}_rows_{ts}.csv"
        out_path = os.path.join(project_dir, out_name)
        _write_csv(out_path, src_header, duplicate_rows)
        print(f"[OK] CSV written: {out_path}")
        return 0

    # Mode: destination dropped duplicate SKUs (or other key) so compare counts per key.
    if args.compare_duplicates:
        src_key_idx = _find_header_index(src_header, args.key_column)
        dst_key_idx = _find_header_index(dst_header, args.key_column) if dst_header else None

        if src_key_idx is None:
            print(f"[ERROR] Key column '{args.key_column}' not found in SOURCE header.")
            return 2
        if dst_key_idx is None:
            print(f"[ERROR] Key column '{args.key_column}' not found in DESTINATION header.")
            return 2

        def _key_from_row(row: List[str], idx: int) -> str:
            if idx >= len(row):
                return ""
            return str(row[idx]).strip().lstrip("#").strip().lower()

        dst_counts: Dict[str, int] = {}
        for r in dst_rows:
            k = _key_from_row(r, dst_key_idx)
            if not k:
                continue
            dst_counts[k] = dst_counts.get(k, 0) + 1

        emitted_counts: Dict[str, int] = {}
        missing_rows: List[List[str]] = []
        for r in src_rows:
            k = _key_from_row(r, src_key_idx)
            if not k:
                continue
            emitted_counts[k] = emitted_counts.get(k, 0)
            allowed = dst_counts.get(k, 0)
            # If destination contains N rows for this key, allow the first N occurrences from source.
            # Any additional occurrences are considered "missing" (likely skipped as duplicates).
            if emitted_counts[k] >= allowed:
                missing_rows.append(r)
            emitted_counts[k] += 1

        print(f"[RESULT] Source data rows: {len(src_rows)}")
        print(f"[RESULT] Destination data rows: {len(dst_rows)}")
        print(f"[RESULT] Rows in source but not in destination (by duplicates on '{args.key_column}'): {len(missing_rows)}")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = f"rows_missing_in_destination_{ts}.csv"
        out_path = os.path.join(project_dir, out_name)

        _write_csv(out_path, src_header, missing_rows)
        print(f"[OK] CSV written: {out_path}")

        try:
            uploaded_id = _upload_to_same_drive_folder_as_sheet(project_dir, dst_sheet_id, out_path)
            if uploaded_id:
                print(f"[OK] Uploaded CSV to destination sheet Drive folder. File id: {uploaded_id}")
            else:
                print("[WARN] Could not determine destination Drive folder (no parents). Uploaded CSV skipped.")
        except Exception as e:
            print(f"[WARN] Upload to Drive folder failed: {e}")

        return 0

    if src_header != dst_header:
        if not args.common_columns:
            print("[ERROR] Source and destination headers do not match. Comparing complete rows would mark everything as missing.")
            print(f"[ERROR] Source header columns: {len(src_header)}")
            print(f"[ERROR] Destination header columns: {len(dst_header)}")
            print("[HINT] Re-check the destination gid/tab, or re-run with --common-columns.")
            return 2

        src_idx = _index_by_header(src_header)
        dst_idx = _index_by_header(dst_header)
        common_headers = [h for h in src_header if h in dst_idx]
        if not common_headers:
            print("[ERROR] No common headers found between source and destination. Cannot compare.")
            return 2
        print(f"[WARN] Headers differ. Comparing using common columns only: {len(common_headers)}")

        dst_set = {
            _normalize_row(_project_row_to_headers(r, dst_idx, common_headers))
            for r in dst_rows
            if any(str(c).strip() for c in r)
        }

        missing_rows = []
        for r in src_rows:
            if not any(str(c).strip() for c in r):
                continue
            projected = _project_row_to_headers(r, src_idx, common_headers)
            if _normalize_row(projected) not in dst_set:
                missing_rows.append(projected)

        src_header = common_headers
    else:
        dst_set = {_normalize_row(r) for r in dst_rows if any(str(c).strip() for c in r)}
        missing_rows = []
        for r in src_rows:
            if not any(str(c).strip() for c in r):
                continue
            if _normalize_row(r) not in dst_set:
                missing_rows.append(r)

    print(f"[RESULT] Source data rows: {len(src_rows)}")
    print(f"[RESULT] Destination data rows: {len(dst_rows)}")
    print(f"[RESULT] Rows in source but not in destination: {len(missing_rows)}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"rows_missing_in_destination_{ts}.csv"
    out_path = os.path.join(project_dir, out_name)

    _write_csv(out_path, src_header, missing_rows)
    print(f"[OK] CSV written: {out_path}")

    try:
        uploaded_id = _upload_to_same_drive_folder_as_sheet(project_dir, dst_sheet_id, out_path)
        if uploaded_id:
            print(f"[OK] Uploaded CSV to destination sheet Drive folder. File id: {uploaded_id}")
        else:
            print("[WARN] Could not determine destination Drive folder (no parents). Uploaded CSV skipped.")
    except Exception as e:
        print(f"[WARN] Upload to Drive folder failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
