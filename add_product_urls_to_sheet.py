#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Search all-stars-motorsport.com by SKU/Reference for each row in the MotorSports
Google Sheet, match the motorsport product_id from the page URL, and add a `url`
column as the first column (column A).

Sheet: configure GOOGLE_SPREADSHEET_ID in your .env file.
Tab: products (gid=0)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import Driver

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "url_lookup_output")

SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
SHEET_NAME_PRODUCTS = "products"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "credentials.json")

SITE_BASE = "https://www.all-stars-motorsport.com/en/"
URL_COLUMN = "url"
STATUS_COLUMN = "url_status"

CHECKPOINT_FILE = os.path.join(OUTPUT_DIR, "url_lookup_checkpoint.json")

# Small pause after a Sheet batch write to ease API rate limits.
SHEET_API_DELAY_AFTER_BATCH = 0.85

# Per-thread Selenium drivers (ThreadPoolExecutor worker threads are reused).
_thread_drivers_lock = threading.Lock()
_thread_drivers: Dict[int, Any] = {}


def safe_gs_call(func, *args, max_retries=6, base_delay=1.25, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (HttpError, APIError) as e:
            status = None
            try:
                status = getattr(getattr(e, "resp", None), "status", None)
            except Exception:
                pass
            if status is None:
                try:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                except Exception:
                    pass
            msg = str(e).lower()
            retryable = status in (429, 500, 503) or ("quota" in msg) or ("rate" in msg and "limit" in msg)
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


def _norm_id(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _normalize_ref(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    while s.startswith("#"):
        s = s[1:]
    return s.strip()


def _strip_duplicate_suffix(ref: str) -> str:
    from motorsport_site_scrape import strip_duplicate_suffix

    return strip_duplicate_suffix(ref)


def _search_keys(reference: str) -> List[str]:
    """Search terms to try on the site (base SKU first, trailing -1/_2 removed from end only)."""
    base = _strip_duplicate_suffix(_normalize_ref(reference))
    keys: List[str] = []
    if base:
        keys.append(base)
    raw = str(reference or "").strip().lstrip("#").strip()
    if raw and raw not in keys:
        keys.append(raw)
    return keys


def extract_product_id_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        if "/en/" in url:
            part = url.split("/en/")[1].split("/")[1].split("-")[0]
            if part.isdigit():
                return part
    except Exception:
        pass
    m = re.search(r"[?&]id_product=(\d+)", url, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/(\d+)(?:[-_]|\.|$)", url)
    if m:
        return m.group(1)
    return ""


def _safe_navigate(driver: Any, url: str, *, retries: int = 4, base_delay: float = 1.5) -> bool:
    """Recover from ConnectionResetError / remote host closed connection."""
    last_err = None
    for attempt in range(retries):
        try:
            driver.get(url)
            return True
        except Exception as e:
            last_err = e
            time.sleep(base_delay + attempt * 0.75)
    return False


def _search_product(driver: Driver, reference_number: str) -> bool:
    try:
        if "all-stars-motorsport.com" not in (driver.current_url or ""):
            if not _safe_navigate(driver, SITE_BASE):
                return False
            time.sleep(1.5)
        search_field = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="search_query_top"]'))
        )
        search_field.clear()
        time.sleep(0.2)
        search_field.send_keys(reference_number)
        search_field.send_keys(Keys.ENTER)
        time.sleep(1.2)
        return True
    except Exception:
        return False


def _collect_result_links(driver: Driver, max_links: int = 12) -> List[str]:
    links: List[str] = []
    xpaths = [
        '//ul[contains(@class,"product_list")]//a[contains(@class,"product-name")]',
        '//ul[contains(@class,"product_list")]//a[contains(@class,"product_img_link")]',
        '//a[contains(@class,"product-name")]',
        '//a[contains(@class,"product_img_link")]',
    ]
    for xp in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                href = (el.get_attribute("href") or "").strip()
                if href and href not in links:
                    links.append(href)
                    if len(links) >= max_links:
                        return links
        except Exception:
            continue
    return links


def _try_match_by_opening_links(
    driver: Driver, expected_product_id: str
) -> Tuple[Optional[str], str]:
    expected = _norm_id(expected_product_id)
    if not expected:
        return None, "empty_product_id"

    current = driver.current_url or ""
    pid_here = extract_product_id_from_url(current)
    if pid_here and _norm_id(pid_here) == expected:
        return current, "matched_product_id_direct"

    links = _collect_result_links(driver)
    if not links:
        return None, "no_results_links"

    for href in links:
        try:
            if not _safe_navigate(driver, href):
                continue
            time.sleep(1.0)
            pid = extract_product_id_from_url(driver.current_url or "")
            if pid and _norm_id(pid) == expected:
                return driver.current_url, "matched_product_id"
        except Exception:
            continue
    return None, "no_matching_product_id_in_results"


def find_product_url(
    driver: Driver, reference: str, product_id: str
) -> Tuple[Optional[str], str]:
    pid_expected = _norm_id(product_id)
    if not pid_expected:
        return None, "empty_product_id"

    keys = _search_keys(reference)
    if not keys:
        return None, "empty_reference"

    last_status = "search_failed"
    for key in keys:
        if not _search_product(driver, key):
            last_status = "search_failed"
            continue
        url, status = _try_match_by_opening_links(driver, expected_product_id=pid_expected)
        if url:
            return url, status
        last_status = status

    return None, last_status


def _find_col(headers: List[str], names: List[str]) -> int:
    norm = {n.lower().strip() for n in names}
    for i, h in enumerate(headers):
        if str(h or "").strip().lower() in norm:
            return i
    return -1


def connect_sheet():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(SHEET_NAME_PRODUCTS)
    return worksheet


def ensure_url_columns(worksheet, headers: List[str]) -> Tuple[List[str], int, int]:
    """
    Ensure `url` is column A and optional `url_status` is column B.
    Returns (updated_headers, url_col_idx, status_col_idx).
    """
    url_idx = _find_col(headers, [URL_COLUMN])
    status_idx = _find_col(headers, [STATUS_COLUMN])

    if url_idx == 0:
        if status_idx == -1:
            return headers, 0, -1
        return headers, 0, status_idx

    # Insert url (and status) at front via API
    inserts = 1 if url_idx == -1 else 0
    if status_idx == -1 and inserts:
        inserts = 2  # url + url_status

    if url_idx == -1:
        if inserts == 2:
            safe_gs_call(worksheet.insert_cols, [[URL_COLUMN], [STATUS_COLUMN]], col=1)
            headers = [URL_COLUMN, STATUS_COLUMN] + headers
            return headers, 0, 1
        safe_gs_call(worksheet.insert_cols, [[URL_COLUMN]], col=1)
        headers = [URL_COLUMN] + headers
        url_idx = 0
    elif url_idx > 0:
        # url exists but not first — user asked for front; move data in memory only
        pass

    status_idx = _find_col(headers, [STATUS_COLUMN])
    return headers, url_idx if url_idx != -1 else 0, status_idx


def load_checkpoint() -> Dict[str, Dict[str, str]]:
    if not os.path.isfile(CHECKPOINT_FILE):
        return {}
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        out: Dict[str, Dict[str, str]] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                out[str(k)] = {"url": str(v.get("url", "")), "status": str(v.get("status", ""))}
            else:
                out[str(k)] = {"url": str(v or ""), "status": "checkpoint"}
        return out
    except Exception:
        return {}


def save_checkpoint(cache: Dict[str, Dict[str, str]]) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def checkpoint_key(product_id: str, reference: str) -> str:
    return f"{_norm_id(product_id)}|{str(reference or '').strip()}"


def write_csv_backup(path: str, headers: List[str], rows: List[List[str]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def batch_update_column(
    worksheet,
    col_letter: str,
    start_row: int,
    values: List[List[str]],
) -> None:
    if not values:
        return
    end_row = start_row + len(values) - 1
    rng = f"{col_letter}{start_row}:{col_letter}{end_row}"
    safe_gs_call(
        worksheet.update,
        values,
        range_name=rng,
        value_input_option="RAW",
    )


def _quit_thread_driver(tid: int) -> None:
    with _thread_drivers_lock:
        d = _thread_drivers.pop(tid, None)
    if d is not None:
        try:
            d.quit()
        except Exception:
            pass


def get_thread_driver(headless: bool) -> Any:
    """One Chrome instance per worker thread."""
    tid = threading.get_ident()
    with _thread_drivers_lock:
        if tid not in _thread_drivers:
            d = Driver(browser="chrome", headless=headless)
            try:
                if not _safe_navigate(d, SITE_BASE):
                    raise RuntimeError("Could not load motorsport home")
                time.sleep(1.2)
            except Exception:
                try:
                    d.quit()
                except Exception:
                    pass
                raise
            _thread_drivers[tid] = d
        return _thread_drivers[tid]


def teardown_all_thread_drivers() -> None:
    with _thread_drivers_lock:
        tids = list(_thread_drivers.keys())
    for tid in tids:
        _quit_thread_driver(tid)


def process_url_task(
    args_tuple: Tuple[int, str, str, bool, Dict[str, Dict[str, str]]],
) -> Tuple[int, str, str, str, str]:
    """
    (sheet_row, pid, reference, headless, cache_ref) ->
        (sheet_row, url, status, pid, ref_copy)

    If the shared checkpoint dict already has a URL for this row, skip the browser.
    """
    sheet_row, pid, reference, headless_bool, cache_ref = args_tuple
    ref_copy = str(reference or "").strip()

    ck = checkpoint_key(pid, reference)
    hit = cache_ref.get(ck)
    if hit and str(hit.get("url", "") or "").strip():
        return (
            sheet_row,
            str(hit["url"]).strip(),
            str(hit.get("status") or "checkpoint"),
            pid,
            ref_copy,
        )

    try:
        driver = get_thread_driver(headless_bool)
    except Exception as e:
        return sheet_row, "", f"browser_error:{e}", pid, ref_copy

    for attempt in range(3):
        try:
            url, status = find_product_url(driver, reference, pid)
            return sheet_row, url or "", status, pid, ref_copy
        except Exception as e:
            msg = str(e).lower()
            tid = threading.get_ident()
            _quit_thread_driver(tid)
            transient = (
                "10054" in msg
                or "connection" in msg
                or "reset" in msg
                or "refused" in msg
                or "timeout" in msg
            )
            if transient and attempt < 2:
                time.sleep(2.0 + attempt)
                continue
            return sheet_row, "", f"error:{e}", pid, ref_copy

    return sheet_row, "", "error:exhausted_retries", pid, ref_copy


def flush_pending_writes(
    worksheet,
    *,
    url_col: int,
    status_col: int,
    pending_urls: List[Tuple[int, str]],
    pending_status: List[Tuple[int, str]],
) -> None:
    if not pending_urls:
        return
    by_row = sorted(pending_urls, key=lambda x: x[0])
    vals = [[u] for _, u in by_row]
    batch_update_column(worksheet, col_letter(url_col), by_row[0][0], vals)
    if status_col != -1 and pending_status:
        by_stat = sorted(pending_status, key=lambda x: x[0])
        stat_vals = [[s] for _, s in by_stat]
        batch_update_column(worksheet, col_letter(status_col), by_stat[0][0], stat_vals)


def col_letter(index_0_based: int) -> str:
    n = index_0_based + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Add motorsport product URLs to the Google Sheet products tab."
    )
    parser.add_argument("--limit", type=int, default=0, help="Max scrape tasks (0 = all empty rows)")
    parser.add_argument(
        "--start-row",
        type=int,
        default=2,
        help="Minimum sheet row (1-based); rows above are ignored",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Google Sheet")
    parser.add_argument("--no-sheet", action="store_true", help="CSV backup only, no sheet updates")
    parser.add_argument(
        "--include-filled",
        action="store_true",
        help="Also re-search rows that already have a URL (default: only rows with empty url)",
    )
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel browser threads (default: 4). Use 1 to disable parallelism.",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Write Google Sheet + checkpoint every N completed lookups (default: 1 = each row). "
            "Higher N reduces API calls and is faster but delays visible updates."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Prefill empty url cells from url_lookup_checkpoint.json before scraping",
    )
    args = parser.parse_args(argv)

    only_empty_url = not args.include_filled
    workers = max(1, int(args.workers))
    flush_every = max(1, int(args.flush_every))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("[Sheet] Connecting...")
    worksheet = connect_sheet()
    all_values = safe_gs_call(worksheet.get_all_values)
    if not all_values:
        print("ERROR: Sheet is empty")
        return 1

    headers = [str(h or "").strip() for h in all_values[0]]
    if not args.dry_run and not args.no_sheet:
        headers, url_col, status_col = ensure_url_columns(worksheet, headers)
        all_values = safe_gs_call(worksheet.get_all_values)
        headers = [str(h or "").strip() for h in all_values[0]]
    else:
        url_col = _find_col(headers, [URL_COLUMN])
        status_col = _find_col(headers, [STATUS_COLUMN])
        if url_col == -1:
            headers = [URL_COLUMN] + headers
            url_col = 0
            padded_rows = []
            for row in all_values[1:]:
                padded_rows.append([""] + row)
            all_values = [headers] + padded_rows

    url_col = _find_col(headers, [URL_COLUMN])
    status_col = _find_col(headers, [STATUS_COLUMN])
    col_pid = _find_col(headers, ["product_id", "Product_ID"])
    col_ref = _find_col(headers, ["Reference", "reference", "SKU"])

    if col_pid == -1 or col_ref == -1:
        print("ERROR: Sheet must have product_id and Reference columns")
        return 1
    if url_col == -1:
        print("ERROR: Could not resolve url column")
        return 1

    cache = load_checkpoint()
    flush_lock = threading.RLock()
    pending_urls: List[Tuple[int, str]] = []
    pending_status: List[Tuple[int, str]] = []

    start_row = max(2, args.start_row)
    data_rows = all_values[1:]

    for row in data_rows:
        while len(row) < len(headers):
            row.append("")

    matched = 0
    skipped_already_filled = 0
    restored_from_ckpt = 0

    # (--resume): fill empty sheet cells from checkpoint without opening the browser.
    if args.resume:
        for offset, row in enumerate(data_rows):
            sheet_row = offset + 2
            if sheet_row < start_row:
                continue
            pid = row[col_pid] if col_pid < len(row) else ""
            ref = row[col_ref] if col_ref < len(row) else ""
            if not _norm_id(pid) or not str(ref or "").strip():
                continue
            existing_url = row[url_col] if url_col < len(row) else ""
            if str(existing_url or "").strip():
                continue
            ck = checkpoint_key(pid, ref)
            hit = cache.get(ck)
            if hit and str(hit.get("url", "") or "").strip():
                row[url_col] = str(hit["url"]).strip()
                if status_col != -1:
                    while len(row) <= status_col:
                        row.append("")
                    row[status_col] = str(hit.get("status") or "checkpoint")
                matched += 1
                restored_from_ckpt += 1
                if not args.dry_run and not args.no_sheet:
                    pending_urls.append((sheet_row, row[url_col]))
                    if status_col != -1:
                        pending_status.append((sheet_row, row[status_col]))
        if restored_from_ckpt:
            print(
                f"[Resume] Applied {restored_from_ckpt} URLs from checkpoint to empty sheet cells"
            )

    scrape_tasks: List[Tuple[int, str, str, bool, Dict[str, Dict[str, str]]]] = []
    for offset, row in enumerate(data_rows):
        sheet_row = offset + 2
        if sheet_row < start_row:
            continue
        pid = row[col_pid] if col_pid < len(row) else ""
        ref = row[col_ref] if col_ref < len(row) else ""
        if not _norm_id(pid) or not str(ref or "").strip():
            continue

        existing_url = row[url_col] if url_col < len(row) else ""
        if only_empty_url and str(existing_url or "").strip():
            skipped_already_filled += 1
            continue

        scrape_tasks.append((sheet_row, str(pid).strip(), str(ref).strip(), args.headless, cache))

    if args.limit:
        scrape_tasks = scrape_tasks[: args.limit]

    print(
        f"[Queue] {len(scrape_tasks)} rows to scrape, "
        f"{skipped_already_filled} skipped (already have URL), "
        f"workers={workers}, only_empty_url={only_empty_url}"
    )

    processed = restored_from_ckpt if args.resume else 0

    def flush_batch_if_needed(force: bool) -> None:
        """Caller must hold flush_lock."""
        if not force and len(pending_urls) < flush_every:
            return
        if not pending_urls:
            return
        save_checkpoint(cache)
        if not args.dry_run and not args.no_sheet:
            flush_pending_writes(
                worksheet,
                url_col=url_col,
                status_col=status_col,
                pending_urls=pending_urls,
                pending_status=pending_status,
            )
            time.sleep(SHEET_API_DELAY_AFTER_BATCH)
        pending_urls.clear()
        pending_status.clear()

    if pending_urls and not args.dry_run and not args.no_sheet:
        flush_pending_writes(
            worksheet,
            url_col=url_col,
            status_col=status_col,
            pending_urls=list(pending_urls),
            pending_status=list(pending_status) if status_col != -1 else [],
        )
        time.sleep(SHEET_API_DELAY_AFTER_BATCH)
        pending_urls.clear()
        pending_status.clear()

    def apply_result(sr: int, url: str, status: str, pid: str, ref: str) -> None:
        nonlocal matched, processed
        with flush_lock:
            ck = checkpoint_key(pid, ref)
            cache[ck] = {"url": url or "", "status": status}
            offset = sr - 2
            if 0 <= offset < len(data_rows):
                row = data_rows[offset]
                while len(row) < len(headers):
                    row.append("")
                row[url_col] = url or ""
                if status_col != -1:
                    while len(row) <= status_col:
                        row.append("")
                    row[status_col] = status
            if url:
                matched += 1
            pending_urls.append((sr, url or ""))
            if status_col != -1:
                pending_status.append((sr, status))
            processed += 1
            if processed % max(10, workers * 5) == 0:
                print(f"[Progress] completed={processed} matched_urls≈{matched}")
            flush_batch_if_needed(force=False)

    try:
        if scrape_tasks:
            max_workers_effective = workers if scrape_tasks else 1
            with ThreadPoolExecutor(max_workers=max_workers_effective) as executor:
                futures = [executor.submit(process_url_task, t) for t in scrape_tasks]
                for fut in as_completed(futures):
                    try:
                        sheet_row, url, status, pid, ref_copy = fut.result()
                    except Exception as e:
                        print(f"[WARN] Task failed: {e}")
                        continue
                    apply_result(sheet_row, url, status, pid, ref_copy)

        with flush_lock:
            flush_batch_if_needed(force=True)

    finally:
        teardown_all_thread_drivers()

    save_checkpoint(cache)

    with flush_lock:
        if pending_urls and not args.dry_run and not args.no_sheet:
            flush_pending_writes(
                worksheet,
                url_col=url_col,
                status_col=status_col,
                pending_urls=list(pending_urls),
                pending_status=list(pending_status) if status_col != -1 else [],
            )
            pending_urls.clear()
            pending_status.clear()

    all_values_out = safe_gs_call(worksheet.get_all_values) if not args.no_sheet else [headers] + data_rows
    backup_path = os.path.join(OUTPUT_DIR, f"products_with_urls_{ts}.csv")
    if all_values_out:
        write_csv_backup(backup_path, all_values_out[0], all_values_out[1:])

    print(
        f"[Done] processed≈{processed} matched_urls≈{matched} "
        f"skipped_already_filled={skipped_already_filled} restored_ckpt={restored_from_ckpt}"
    )
    print(f"[Backup] {backup_path}")
    print(f"[Checkpoint] {CHECKPOINT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
