#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copy the `url` column (and optional `url_status`) from the Motorsports products
Google Sheet to the Shopify upload products sheet.

Row match (strict):
  • product_id (normalized)
  • Reference: strip leading `#`, lowercase, strip trailing ``-N`` / ``_N`` (digits)
    repeatedly so e.g. ``#SKU-1`` / ``SKU_1`` align with ``sku``.
  • All ``att_*`` columns that exist on **both** sheets: values must match after
    resolving upload values through ``att_value_translation_lookup.json`` (same
    idea as ``match_targets_for_sheet_value`` in motorsport_site_scrape.py):
    Dutch / translated cell → English key from JSON when present, then compare
    to the motor row (English) case-insensitively.

Does not change other columns. Inserts ``url`` (and ``url_status`` if absent) at
column A when missing.

Usage:
  python sync_url_motor_to_upload_sheet.py
  python sync_url_motor_to_upload_sheet.py --dry-run
  python sync_url_motor_to_upload_sheet.py --pid-ref-only   # ignore att_* match
  python sync_url_motor_to_upload_sheet.py --att-json path/to/lookup.json

Requires credentials.json next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ATT_JSON = os.path.join(SCRIPT_DIR, "att_value_translation_lookup.json")

# Motorsports sheet (URLs from add_product_urls_to_sheet.py)
SOURCE_SPREADSHEET_ID = os.environ.get("GOOGLE_SOURCE_SPREADSHEET_ID", "")
# Shopify upload sheet
DEST_SPREADSHEET_ID = os.environ.get("GOOGLE_UPLOADED_SPREADSHEET_ID", "")
SHEET_NAME_PRODUCTS = "products"

URL_COLUMN = "url"
STATUS_COLUMN = "url_status"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SERVICE_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
SHEET_API_DELAY = 0.85

_att_bundle_cache: Dict[str, Dict[str, Any]] = {}


def _load_att_translation_bundle(path: str) -> Dict[str, Any]:
    global _att_bundle_cache
    key = os.path.abspath(path or "")
    if key in _att_bundle_cache:
        return _att_bundle_cache[key]
    try:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                _att_bundle_cache[key] = json.load(f)
        else:
            _att_bundle_cache[key] = {}
    except Exception:
        _att_bundle_cache[key] = {}
    data = _att_bundle_cache[key]
    if not isinstance(data, dict):
        data = {}
        _att_bundle_cache[key] = data
    data.setdefault("by_column", {})
    data.setdefault("global", {})
    return data


def match_targets_for_sheet_value(
    att_key_lower: str, raw: str, bundle: Dict[str, Any]
) -> List[str]:
    """Same behaviour as motorsport_site_scrape.match_targets_for_sheet_value."""
    raw = str(raw or "").strip()
    if not raw:
        return []
    out: List[str] = []
    seen = set()

    def add(s: str) -> None:
        s = str(s).strip()
        if not s:
            return
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)

    add(raw)
    lk = raw.lower()
    att_key_lower = (att_key_lower or "").strip().lower()

    by_col = bundle.get("by_column") or {}
    if att_key_lower and isinstance(by_col.get(att_key_lower), dict):
        eng = by_col[att_key_lower].get(lk)
        if eng:
            add(eng)

    gl = bundle.get("global") or {}
    if isinstance(gl, dict):
        eng2 = gl.get(lk)
        if eng2:
            add(eng2)

    return out


def normalize_ref_strip_hash(value: Optional[str]) -> str:
    s = str(value or "").strip()
    while s.startswith("#"):
        s = s[1:].strip()
    return s.strip()


def strip_trailing_index_suffix(ref: str) -> str:
    """Strip one trailing variant index (``-1``, ``_2``, …) from the end only — not mid-string ``-``/``_``."""
    from motorsport_site_scrape import strip_duplicate_suffix

    return strip_duplicate_suffix(ref)


def normalize_reference_for_match(reference: Optional[str]) -> str:
    """Reference key: no leading #, lowercase, no trailing -1 / _2 style suffixes (repeat)."""
    s = normalize_ref_strip_hash(reference).lower().strip()
    for _ in range(12):
        nxt = strip_trailing_index_suffix(s).lower().strip()
        if nxt == s:
            return s
        s = nxt
    return s


def partial_row_key(pid: str, ref: str) -> Tuple[str, str]:
    return (_norm_pid(pid), normalize_reference_for_match(ref))


def canonical_att_for_fingerprint(att_key_lower: str, cell: str, bundle: Dict[str, Any]) -> str:
    """
    Single lowercase token so upload (Dutch) and motor (English) agree when JSON maps them.
    """
    raw_l = str(cell or "").strip().lower()
    if not raw_l:
        return ""
    targets = match_targets_for_sheet_value(att_key_lower, str(cell), bundle)
    for cand in targets[1:]:
        cl = str(cand).strip().lower()
        if cl and cl != raw_l:
            return cl
    return raw_l


def att_column_names(headers: List[str]) -> List[str]:
    names = []
    for h in headers:
        hs = str(h or "").strip().lower()
        if hs.startswith("att_"):
            names.append(hs)
    return sorted(set(names))


def row_att_fingerprint(
    row: List[str],
    headers: List[str],
    common_att_cols: List[str],
    bundle: Dict[str, Any],
) -> Tuple[Tuple[str, str], ...]:
    col_to_i = {str(h or "").strip().lower(): i for i, h in enumerate(headers)}
    pairs: List[Tuple[str, str]] = []
    for att in common_att_cols:
        idx = col_to_i.get(att, -1)
        cell = row[idx] if 0 <= idx < len(row) else ""
        pairs.append((att, canonical_att_for_fingerprint(att, cell, bundle)))
    return tuple(pairs)


def safe_gs_call(func, *args, max_retries: int = 6, base_delay: float = 1.25, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (HttpError, APIError) as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status is None:
                status = getattr(getattr(e, "response", None), "status_code", None)
            msg = str(e).lower()
            retryable = (
                status in (429, 500, 503)
                or ("quota" in msg)
                or ("rate" in msg and "limit" in msg)
            )
            if not retryable or attempt >= max_retries - 1:
                raise
            time.sleep((base_delay * (2**attempt)) + random.uniform(0.0, 0.6))
        except Exception as e:
            msg = str(e).lower()
            if ("quota" in msg) or ("429" in msg) or ("rate" in msg and "limit" in msg):
                if attempt >= max_retries - 1:
                    raise
                time.sleep((base_delay * (2**attempt)) + random.uniform(0.0, 0.6))
                continue
            raise


def _norm_pid(value: Optional[str]) -> str:
    s = str(value or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _find_col(headers: List[str], aliases: List[str]) -> int:
    want = {a.lower().strip() for a in aliases}
    for i, h in enumerate(headers):
        if str(h or "").strip().lower() in want:
            return i
    return -1


def col_letter(index_0_based: int) -> str:
    n = index_0_based + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def connect_worksheet(spreadsheet_id: str) -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = safe_gs_call(client.open_by_key, spreadsheet_id)
    return spreadsheet.worksheet(SHEET_NAME_PRODUCTS)


def ensure_url_columns(
    worksheet: gspread.Worksheet, headers: List[str]
) -> Tuple[List[str], int, int]:
    """Same as add_product_urls_to_sheet.ensure_url_columns."""
    url_idx = _find_col(headers, [URL_COLUMN])
    status_idx = _find_col(headers, [STATUS_COLUMN])

    if url_idx == 0:
        if status_idx == -1:
            return headers, 0, -1
        return headers, 0, status_idx

    inserts = 1 if url_idx == -1 else 0
    if status_idx == -1 and inserts:
        inserts = 2

    if url_idx == -1:
        if inserts == 2:
            safe_gs_call(worksheet.insert_cols, [[URL_COLUMN], [STATUS_COLUMN]], col=1)
            headers = [URL_COLUMN, STATUS_COLUMN] + headers
            return headers, 0, 1
        safe_gs_call(worksheet.insert_cols, [[URL_COLUMN]], col=1)
        headers = [URL_COLUMN] + headers
        url_idx = 0

    status_idx = _find_col(headers, [STATUS_COLUMN])
    return headers, url_idx if url_idx != -1 else 0, status_idx


class MotorCandidate:
    __slots__ = ("fingerprint", "url", "status")

    def __init__(
        self,
        *,
        fingerprint: Tuple[Tuple[str, str], ...],
        url: str,
        status: str,
    ) -> None:
        self.fingerprint = fingerprint
        self.url = url
        self.status = status


def load_motor_index(
    rows: List[List[str]],
    headers: List[str],
    common_att_cols: List[str],
    bundle: Dict[str, Any],
    use_att_fingerprint: bool,
) -> Dict[Tuple[str, str], List[MotorCandidate]]:
    pid_i = _find_col(headers, ["product_id", "Product_ID"])
    ref_i = _find_col(headers, ["Reference", "reference", "SKU", "sku"])
    url_i = _find_col(headers, [URL_COLUMN, "product_url"])
    st_i = _find_col(headers, [STATUS_COLUMN])

    if pid_i == -1 or ref_i == -1:
        raise RuntimeError("Motor sheet: need product_id and Reference (or SKU)")
    if url_i == -1:
        raise RuntimeError("Motor sheet: need url column")

    index: Dict[Tuple[str, str], List[MotorCandidate]] = {}

    for row in rows:
        while len(row) < len(headers):
            row.append("")
        pid = row[pid_i] if pid_i < len(row) else ""
        ref = row[ref_i] if ref_i < len(row) else ""
        pk = partial_row_key(pid, ref)
        if not pk[0] or not pk[1]:
            continue
        url = str(row[url_i] if url_i < len(row) else "").strip()
        status = ""
        if st_i != -1 and st_i < len(row):
            status = str(row[st_i] or "").strip()

        if use_att_fingerprint and common_att_cols:
            fp = row_att_fingerprint(row, headers, common_att_cols, bundle)
        else:
            fp = ()

        cand = MotorCandidate(fingerprint=fp, url=url, status=status)
        index.setdefault(pk, []).append(cand)

    def sort_key(c: MotorCandidate) -> Tuple[int, int]:
        return (0 if c.url else 1, 0)

    for lst in index.values():
        lst.sort(key=sort_key)

    return index


def pick_motor_candidate(
    candidates: List[MotorCandidate], upload_fp: Tuple[Tuple[str, str], ...]
) -> Optional[MotorCandidate]:
    matches = [c for c in candidates if c.fingerprint == upload_fp]
    if not matches:
        return None
    return matches[0]


def batch_update_column(
    worksheet: gspread.Worksheet,
    *,
    col_letter_str: str,
    start_row: int,
    values: List[List[str]],
) -> None:
    if not values:
        return
    end_row = start_row + len(values) - 1
    rng = f"{col_letter_str}{start_row}:{col_letter_str}{end_row}"
    safe_gs_call(
        worksheet.update,
        values,
        range_name=rng,
        value_input_option="RAW",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy url (and url_status) from Motorsports sheet to upload sheet."
    )
    parser.add_argument(
        "--source-id",
        default=SOURCE_SPREADSHEET_ID,
        help="Motor / source spreadsheet id",
    )
    parser.add_argument(
        "--dest-id",
        default=DEST_SPREADSHEET_ID,
        help="Upload / destination spreadsheet id",
    )
    parser.add_argument(
        "--att-json",
        default="",
        help=f"att_value_translation_lookup.json path (default: {DEFAULT_ATT_JSON})",
    )
    parser.add_argument(
        "--pid-ref-only",
        action="store_true",
        help="Match only product_id + normalized Reference (ignore att_* columns)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts only; do not modify the upload sheet",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace non-empty upload URLs when motor has a URL",
    )
    parser.add_argument(
        "--clear-when-motor-empty",
        action="store_true",
        help="Clear upload URL when motor has no URL for that SKU (risky)",
    )
    args = parser.parse_args()

    if not os.path.isfile(SERVICE_ACCOUNT_FILE):
        print(f"[ERROR] Missing {SERVICE_ACCOUNT_FILE}")
        return 1

    att_path = (args.att_json or "").strip() or os.environ.get("ATT_TRANSLATION_JSON", "") or DEFAULT_ATT_JSON
    bundle = _load_att_translation_bundle(att_path)

    print("[INFO] Reading motor (source) sheet…")
    ws_src = connect_worksheet(args.source_id)
    src_all = safe_gs_call(ws_src.get_all_values)
    if len(src_all) < 2:
        print("[ERROR] Source sheet empty")
        return 1
    src_headers = [str(h or "").strip() for h in src_all[0]]

    print("[INFO] Reading upload (destination) sheet…")
    ws_dst = connect_worksheet(args.dest_id)
    dst_all = safe_gs_call(ws_dst.get_all_values)
    if len(dst_all) < 2:
        print("[ERROR] Destination sheet empty")
        return 1

    dst_headers_work = [str(h or "").strip() for h in dst_all[0]]
    if not args.dry_run:
        ensure_url_columns(ws_dst, dst_headers_work)
        dst_all = safe_gs_call(ws_dst.get_all_values)
        dst_headers_work = [str(h or "").strip() for h in dst_all[0]]

    motor_att = att_column_names(src_headers)
    upload_att = att_column_names(dst_headers_work)
    common_att = sorted(set(motor_att) & set(upload_att))
    pid_ref_only = bool(args.pid_ref_only)

    if pid_ref_only:
        print("[INFO] Mode: product_id + Reference only (att_* match disabled).")
    elif not common_att:
        print("[WARN] No att_* columns in common; matching on product_id + Reference only.")
    else:
        print(
            f"[INFO] Common att_* columns on both sheets ({len(common_att)}): "
            + (", ".join(common_att[:12]) + (" …" if len(common_att) > 12 else ""))
        )

    use_att_fp = bool(not pid_ref_only and common_att)

    if use_att_fp and not os.path.isfile(att_path):
        print(
            f"[WARN] No att JSON at {att_path}; Dutch→English aliases are skipped (raw text only per column)."
        )

    motor_index = load_motor_index(
        src_all[1:],
        src_headers,
        common_att,
        bundle,
        use_att_fingerprint=use_att_fp,
    )

    print(f"       Motor distinct (product_id + ref) partial keys: {len(motor_index)}")

    url_col = _find_col(dst_headers_work, [URL_COLUMN, "product_url"])
    status_col = _find_col(dst_headers_work, [STATUS_COLUMN])
    if url_col == -1:
        print(
            "[ERROR] Destination has no 'url' column. Run once without --dry-run to insert columns, "
            "or add a url column manually."
        )
        return 1

    pid_i = _find_col(dst_headers_work, ["product_id", "Product_ID"])
    ref_i = _find_col(dst_headers_work, ["Reference", "reference", "SKU", "sku"])
    if pid_i == -1 or ref_i == -1:
        print("[ERROR] Destination needs product_id and Reference (or SKU)")
        return 1

    data_rows = dst_all[1:]
    for row in data_rows:
        while len(row) < len(dst_headers_work):
            row.append("")

    n_matched = 0
    n_fill_empty = 0
    n_overwritten = 0
    n_skip_already_had_url = 0
    n_no_motor_row = 0
    n_ambiguous_partial = 0
    n_cleared = 0
    n_status_copied = 0

    url_out: List[List[str]] = []
    status_out: List[List[str]] = []

    for row in data_rows:
        pid = row[pid_i] if pid_i < len(row) else ""
        ref = row[ref_i] if ref_i < len(row) else ""
        pk = partial_row_key(pid, ref)

        cur_url = str(row[url_col] if url_col < len(row) else "").strip()
        cur_status = ""
        if status_col != -1 and status_col < len(row):
            cur_status = str(row[status_col] or "").strip()

        if not use_att_fp:
            upl_fp = ()
        else:
            upl_fp = row_att_fingerprint(row, dst_headers_work, common_att, bundle)

        cands = motor_index.get(pk)
        src = pick_motor_candidate(cands or [], upl_fp) if cands else None

        if cands and not src:
            n_ambiguous_partial += 1
        if not src:
            n_no_motor_row += 1
            url_out.append([cur_url])
            status_out.append([cur_status])
            continue

        n_matched += 1
        motor_url = str(src.url or "").strip()
        motor_status = str(src.status or "").strip()

        new_url = cur_url
        new_status = cur_status

        if motor_url:
            if not cur_url:
                new_url = motor_url
                n_fill_empty += 1
                if motor_status and status_col != -1:
                    new_status = motor_status
                    n_status_copied += 1
            elif args.overwrite:
                if motor_url != cur_url:
                    n_overwritten += 1
                new_url = motor_url
                if motor_status and status_col != -1:
                    new_status = motor_status
                    n_status_copied += 1
            else:
                n_skip_already_had_url += 1
        else:
            if args.clear_when_motor_empty and cur_url:
                new_url = ""
                n_cleared += 1
                if status_col != -1:
                    new_status = ""

        url_out.append([new_url])
        status_out.append([new_status])

    print(
        "[SUMMARY] "
        f"upload_data_rows={len(data_rows)} motor_partial_keys={len(motor_index)} "
        f"matched={n_matched} no_match={n_no_motor_row} "
        f"partial_key_but_att_mismatch≈{n_ambiguous_partial}"
    )
    print(
        f"          fill_empty={n_fill_empty} overwritten={n_overwritten} "
        f"skip_had_url={n_skip_already_had_url} cleared={n_cleared} "
        f"status_cells_set={n_status_copied}"
    )

    if args.dry_run:
        print("[DRY-RUN] No sheet writes.")
        return 0

    letter_url = col_letter(url_col)
    batch_update_column(ws_dst, col_letter_str=letter_url, start_row=2, values=url_out)
    time.sleep(SHEET_API_DELAY)
    if status_col != -1:
        letter_st = col_letter(status_col)
        batch_update_column(ws_dst, col_letter_str=letter_st, start_row=2, values=status_out)
        time.sleep(SHEET_API_DELAY)

    print("[OK] Upload sheet updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
