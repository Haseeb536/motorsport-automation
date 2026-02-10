#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rename product brand Alpha → Alpha Competition in Google Sheets and Shopify."""

import os
import random
import re
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

import gspread
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OLD_BRAND = "Alpha"
NEW_BRAND = "Alpha Competition"
OLD_BRAND_KEY = OLD_BRAND.casefold()
NEW_TAGS_BRAND = "alpha competition"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")

SPREADSHEET_IDS = [
    os.environ.get("GOOGLE_SPREADSHEET_ID", ""),
    os.environ.get("GOOGLE_UPDATED_SPREADSHEET_ID", ""),
    os.environ.get("GOOGLE_UPLOADED_SPREADSHEET_ID", ""),
]

PRODUCT_BRAND_HEADERS = {
    "brand1",
    "brand",
    "manufacturer",
    "vendor",
    "merk",
    "tags_brand",
}

TEXT_FIELD_HEADERS = {
    "title",
    "shopify_title",
    "meta_title",
    "meta_description",
    "description",
    "html_description",
    "body_html",
}

ALPHA_BRAND_RE = re.compile(r"\bAlpha\b(?! Competition)")

SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-01"
UPLOADED_SPREADSHEET_ID = os.environ.get("GOOGLE_UPLOADED_SPREADSHEET_ID", "")


def safe_gs_call(func, *args, max_retries=6, base_delay=1.25, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (HttpError, APIError) as e:
            status = None
            try:
                status = getattr(getattr(e, "resp", None), "status", None)
            except Exception:
                status = None
            if status is None:
                try:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                except Exception:
                    status = None
            msg = str(e).lower()
            retryable = status in (429, 500, 503) or ("quota" in msg) or ("rate" in msg and "limit" in msg)
            if not retryable or attempt >= max_retries - 1:
                raise
            sleep_s = (base_delay * (2**attempt)) + random.uniform(0.0, 0.6)
            print(f"[WARN] Google API rate limit. Retrying in {sleep_s:.1f}s...")
            time.sleep(sleep_s)


def _header_key(header: str) -> str:
    return str(header or "").strip().lower()


def _is_product_brand_column(header: str) -> bool:
    return _header_key(header) in PRODUCT_BRAND_HEADERS


def replace_alpha_brand_in_text(text: str) -> Optional[str]:
    val = str(text or "")
    if not val or "Alpha Competition" in val:
        return None
    new_val = ALPHA_BRAND_RE.sub(NEW_BRAND, val)
    return new_val if new_val != val else None


def _replacement_value(header: str, current: str) -> Optional[str]:
    key = _header_key(header)
    val = str(current or "").strip()
    if not val:
        return None
    if key == "tags_brand":
        if val.casefold() == OLD_BRAND_KEY:
            return NEW_TAGS_BRAND
        return None
    if val.casefold() == OLD_BRAND_KEY:
        return NEW_BRAND
    return None


def fix_worksheet(sheet) -> int:
    """Update product-brand columns in one worksheet. Skips options tab."""
    title = sheet.title
    if title.strip().lower() == "options":
        print(f"  [SKIP] {title} (vehicle compatibility Brand column)")
        return 0

    all_values = safe_gs_call(sheet.get_all_values)
    if not all_values:
        print(f"  [SKIP] {title} (empty)")
        return 0

    headers = all_values[0]
    brand_cols = [i for i, h in enumerate(headers) if _is_product_brand_column(h)]
    text_cols = [i for i, h in enumerate(headers) if _header_key(h) in TEXT_FIELD_HEADERS]
    if not brand_cols and not text_cols:
        print(f"  [SKIP] {title} (no brand/text columns)")
        return 0

    brand1_col = next((i for i, h in enumerate(headers) if _header_key(h) == "brand1"), -1)

    updates: List[Tuple[int, int, str]] = []
    for row_idx, row in enumerate(all_values[1:], start=2):
        row_brand = ""
        if brand1_col >= 0 and brand1_col < len(row):
            row_brand = str(row[brand1_col] or "").strip().casefold()
        is_alpha_row = row_brand in {OLD_BRAND_KEY, NEW_BRAND.casefold()}

        for col_idx in brand_cols:
            if col_idx >= len(row):
                continue
            new_val = _replacement_value(headers[col_idx], row[col_idx])
            if new_val is not None:
                updates.append((row_idx, col_idx + 1, new_val))

        if is_alpha_row:
            for col_idx in text_cols:
                if col_idx >= len(row):
                    continue
                new_val = replace_alpha_brand_in_text(row[col_idx])
                if new_val is not None:
                    updates.append((row_idx, col_idx + 1, new_val))

    if not updates:
        print(f"  [OK] {title}: no Alpha rows to update")
        return 0

    cells = []
    for row_num, col_num, value in updates:
        cells.append(gspread.Cell(row_num, col_num, value))

    safe_gs_call(sheet.update_cells, cells, value_input_option="USER_ENTERED")
    print(f"  [OK] {title}: updated {len(updates)} cell(s)")
    return len(updates)


def fix_all_spreadsheets(client) -> int:
    total = 0
    for sid in SPREADSHEET_IDS:
        print(f"\nSpreadsheet: {sid}")
        spreadsheet = safe_gs_call(client.open_by_key, sid)
        for ws in spreadsheet.worksheets():
            total += fix_worksheet(ws)
    return total


def collect_shopify_product_ids(client) -> Set[str]:
    spreadsheet = safe_gs_call(client.open_by_key, UPLOADED_SPREADSHEET_ID)
    sheet = safe_gs_call(spreadsheet.worksheet, "products")
    all_values = safe_gs_call(sheet.get_all_values)
    if not all_values:
        return set()

    headers = all_values[0]
    brand_cols = [i for i, h in enumerate(headers) if _header_key(h) in {"brand1", "brand", "vendor", "merk"}]
    pid_col = next((i for i, h in enumerate(headers) if _header_key(h) == "shopify_product_id"), -1)
    if pid_col < 0:
        print("[WARN] No Shopify_Product_ID column on upload sheet")
        return set()

    alpha_brands = {OLD_BRAND_KEY, NEW_BRAND.casefold()}
    ids: Set[str] = set()
    for row in all_values[1:]:
        is_alpha = False
        for col in brand_cols:
            if col < len(row) and str(row[col] or "").strip().casefold() in alpha_brands:
                is_alpha = True
                break
        if not is_alpha:
            continue
        if pid_col < len(row):
            pid = str(row[pid_col] or "").strip()
            if pid:
                ids.add(pid)
    return ids


def _shopify_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    }


def _shopify_request(method: str, url: str, json_payload: Optional[dict] = None) -> requests.Response:
    for attempt in range(6):
        try:
            r = requests.request(method, url, headers=_shopify_headers(), json=json_payload, timeout=45)
            if r.status_code in (429, 500, 502, 503):
                time.sleep((1.5 * (2**attempt)) + random.uniform(0.0, 0.5))
                continue
            return r
        except Exception:
            if attempt >= 5:
                raise
            time.sleep((1.5 * (2**attempt)) + random.uniform(0.0, 0.5))
    raise RuntimeError("Shopify request failed after retries")


def shopify_get_product(product_id: str) -> Optional[dict]:
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    r = _shopify_request("GET", url)
    if r.status_code == 200:
        return (r.json() or {}).get("product")
    return None


def shopify_get_product_metafields(product_id: str) -> List[dict]:
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}/metafields.json?limit=250"
    r = _shopify_request("GET", url)
    if r.status_code == 200:
        return (r.json() or {}).get("metafields", [])
    return []


def shopify_update_metafield(metafield_id: int, value: str, value_type: str = "single_line_text_field") -> bool:
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/metafields/{metafield_id}.json"
    r = _shopify_request(
        "PUT",
        url,
        json_payload={"metafield": {"id": metafield_id, "value": value, "type": value_type}},
    )
    return r.status_code == 200


def shopify_update_product_full(product_id: str, dry_run: bool = False) -> Tuple[bool, str]:
    product = shopify_get_product(product_id)
    if not product:
        return False, "product not found"

    title = str(product.get("title") or "")
    body_html = str(product.get("body_html") or "")
    tags = str(product.get("tags") or "")
    vendor = str(product.get("vendor") or "")

    new_title = replace_alpha_brand_in_text(title) or title
    new_body = replace_alpha_brand_in_text(body_html) or body_html
    new_tags = replace_alpha_brand_in_text(tags) or tags
    new_vendor = NEW_BRAND if vendor.casefold() in {OLD_BRAND_KEY, NEW_BRAND.casefold()} else vendor

    metafields = shopify_get_product_metafields(product_id)
    mf_updates: List[Tuple[int, str, str]] = []
    for mf in metafields:
        ns = str(mf.get("namespace") or "")
        key = str(mf.get("key") or "")
        val = str(mf.get("value") or "")
        mf_type = str(mf.get("type") or "single_line_text_field")
        mf_id = mf.get("id")
        if ns != "custom" or not mf_id:
            continue
        if key == "brand_name" and val.casefold() == OLD_BRAND_KEY:
            mf_updates.append((mf_id, NEW_BRAND, mf_type))
        elif key in {"seo_meta_title", "seo_meta_description"}:
            new_val = replace_alpha_brand_in_text(val)
            if new_val is not None:
                mf_updates.append((mf_id, new_val, mf_type))

    changed = (
        new_title != title
        or new_body != body_html
        or new_tags != tags
        or new_vendor != vendor
        or bool(mf_updates)
    )
    if not changed:
        return True, "already correct"

    if dry_run:
        return True, f"would update ({len(mf_updates)} metafield(s))"

    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    r = _shopify_request(
        "PUT",
        url,
        json_payload={
            "product": {
                "id": int(product_id),
                "title": new_title,
                "body_html": new_body,
                "vendor": new_vendor,
                "tags": new_tags,
            }
        },
    )
    if r.status_code != 200:
        return False, f"product HTTP {r.status_code}: {(r.text or '')[:200]}"

    for mf_id, new_val, mf_type in mf_updates:
        if not shopify_update_metafield(mf_id, new_val, mf_type):
            return False, f"metafield {mf_id} update failed"

    return True, f"updated ({len(mf_updates)} metafield(s))"


def fix_shopify(product_ids: Set[str], dry_run: bool = False) -> Tuple[int, int]:
    ok = 0
    fail = 0
    print(f"\nShopify: updating vendor/title/metafields on {len(product_ids)} product(s)...")
    for pid in sorted(product_ids, key=lambda x: int(x) if x.isdigit() else x):
        success, msg = shopify_update_product_full(pid, dry_run=dry_run)
        if success:
            ok += 1
            print(f"  [OK] product {pid} ({msg})")
        else:
            fail += 1
            print(f"  [FAIL] product {pid}: {msg}")
        time.sleep(0.4)
    return ok, fail


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — no writes")

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)

    product_ids = collect_shopify_product_ids(client)
    print(f"Found {len(product_ids)} Alpha Competition Shopify product ID(s) on upload sheet")

    if not dry_run:
        sheet_updates = fix_all_spreadsheets(client)
        print(f"\nGoogle Sheets: {sheet_updates} cell(s) updated total")
    else:
        print("Skipping sheet writes in dry-run")

    if product_ids:
        ok, fail = fix_shopify(product_ids, dry_run=dry_run)
        print(f"\nShopify: {ok} updated, {fail} failed")
    else:
        print("No Shopify products to update")


if __name__ == "__main__":
    main()
