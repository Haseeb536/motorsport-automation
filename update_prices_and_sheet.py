#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upload-sheet updater: cost + retail (distribution) + availability (motorsport).

Distribution (all-stars-distribution.com):
  • Strip ``#``; strip trailing ``_1``/``-2`` (one digit at end); borderline overrides.
  • Exact ``card_sku ==`` clean Reference; cost/retail xpaths on matched ``li`` card.
  • Search results paginated: if SKU not on page 1, later ``#pagination_bottom`` pages are checked.
  • Cost xpath ``…/div[3]/span`` → sheet cost (+15). Retail xpath ``…/div[2]/div/span[1]/span`` → retail (×1.21+15).

Motorsport (``motorsport_site_scrape``):
  • ``product_id`` + Reference + ``att_*`` / translation JSON → availability.
  • Retail fallback when distribution has no price: motorsport PDP price + 15 (no ×1.21).

Default **6 workers**: one Chrome per worker for the whole run (login once per thread).
Each worker processes its rows in sheet order; each row → immediate sheet + Shopify.
``--workers 1`` = single browser, fully sequential.

Distribution login credentials are in ``distribution_cost_scrape.py``.
"""

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import argparse
import os
import time
import random
import re
import itertools
import atexit
import signal
import threading
import gspread
import requests
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, NoSuchElementException, TimeoutException
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, List, Dict, Optional, Tuple

from brand_price_config import (
    apply_update_prices_by_brand,
    resolve_brand_config_path,
    enabled_brand_display_names,
    find_brand_column_index,
    load_brand_config,
    merge_discovered_brands,
    save_brand_config,
)

# ============================================
# GLOBAL DRIVER REGISTRY – ensures cleanup
# ============================================

_drivers_lock = threading.Lock()
_active_drivers: List = []

def _register_driver(driver) -> None:
    with _drivers_lock:
        _active_drivers.append(driver)

def _unregister_driver(driver) -> None:
    with _drivers_lock:
        try:
            _active_drivers.remove(driver)
        except ValueError:
            pass

def _quit_all_drivers() -> None:
    with _drivers_lock:
        for driver in list(_active_drivers):
            try:
                driver.quit()
            except Exception:
                pass
        _active_drivers.clear()

atexit.register(_quit_all_drivers)

def _signal_handler(sig, frame):
    _quit_all_drivers()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _signal_handler)
try:
    signal.signal(signal.SIGBREAK, _signal_handler)
except AttributeError:
    pass

# ============================================
# CONFIGURATION
# ============================================

SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-01"

UPLOADED_SPREADSHEET_ID = os.environ.get("GOOGLE_UPLOADED_SPREADSHEET_ID", "")
SHEET_NAME_PRODUCTS = "products"

MOTORSPORT_SITE_BASE = "https://www.all-stars-motorsport.com/en/"
# Legacy availability helper (same storefront as main motorsport URL).
AVAILABILITY_SITE_BASE = MOTORSPORT_SITE_BASE

COST_PRICE_COLUMN = "cost price"

from motorsport_pricing import calculate_cost_price as _calc_cost_price
from motorsport_pricing import calculate_final_price as _calc_final_price
from motorsport_pricing import calculate_motorsport_fallback_price as _calc_motor_fallback_price
from motorsport_pricing import calculation_steps
from motorsport_pricing import motorsport_fallback_steps
from motorsport_pricing import parse_scraped_price
from motorsport_site_scrape import (
    english_pdp_label_from_upload_value,
    scrape_price_and_availability,
    sku_matches_on_page,
)
from distribution_cost_scrape import (
    DISTRIBUTION_EMAIL,
    DISTRIBUTION_PASSWORD,
    login_distribution_site,
    scrape_distribution_cost_and_retail,
)
from sku_price_overrides import (
    apply_vwr_pack_multiplier_to_raw,
    distribution_card_match_sku,
    distribution_search_sku,
    strip_single_digit_variant_suffix,
    vwr_ignition_coil_pack_multiplier,
)

MAX_WORKERS = 6
SHEET_UPDATE_DELAY = 1.2

_sheet_write_lock = threading.Lock()
_console_lock = threading.Lock()
_shopify_cost_lock = threading.Lock()
_shopify_cost_api_enabled = True

WRITE_INVENTORY_SCOPE = "write_inventory"
SHOPIFY_COST_SCOPE_HELP = """
⚠️  Shopify Cost cannot be updated: API token is missing «write_inventory» scope.

   The Google Sheet «cost price» column is still updated. To fix Shopify Cost:

   1. Shopify Admin → Settings → Apps and sales channels → Develop apps
   2. Open your custom app (the one that created the Admin API token)
   3. Configuration → Admin API integration → Edit scopes
   4. Enable: read_inventory + write_inventory (merchant may need to approve)
   5. Save → Install app / Reinstall to apply scopes
   6. Copy the new Admin API access token into SHOPIFY_ACCESS_TOKEN in update_prices_and_sheet.py
   7. Re-run this script
""".strip()


def normalize_shopify_id(value: Any) -> str:
    """Sheets often store numeric IDs as ``12345678901234.0`` — strip for API calls."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def shopify_id_int(value: Any) -> Optional[int]:
    s = normalize_shopify_id(value)
    if not s.isdigit():
        return None
    try:
        return int(s)
    except ValueError:
        return None


def wprint(worker_id: int, message: str) -> None:
    """Thread-safe console line prefixed with worker id."""
    with _console_lock:
        print(f"[W{worker_id}] {message}", flush=True)


def emit_row_progress(
    worker_id: int, row_num: int, done: int, total: int
) -> None:
    """Structured line for Streamlit dashboard progress (no worker prefix)."""
    with _console_lock:
        print(
            f"[PROGRESS] worker={worker_id} row={row_num} done={done} total={total}",
            flush=True,
        )

# Log files
DETAILED_LOG_FILE = "scraper_detailed.log"
ERROR_LOG_FILE = "scraper_errors.log"

SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

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
                    pass
            msg = str(e).lower()
            retryable = status in (429, 500, 503) or ("quota" in msg) or ("rate" in msg and "limit" in msg)
            if not retryable or attempt >= max_retries - 1:
                raise
            sleep_s = (base_delay * (2**attempt)) + random.uniform(0.0, 0.6)
            time.sleep(sleep_s)
        except Exception as e:
            msg = str(e).lower()
            if ("quota" in msg) or ("429" in msg) or ("rate" in msg and "limit" in msg):
                if attempt >= max_retries - 1:
                    raise
                sleep_s = (base_delay * (2**attempt)) + random.uniform(0.0, 0.6)
                time.sleep(sleep_s)
                continue
            raise

# ============================================
# LOGGING UTILITIES (thread-safe)
# ============================================

_log_lock = threading.Lock()

def log_detailed(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with _log_lock:
        try:
            with open(DETAILED_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            print(f"⚠️ Failed to write detailed log: {e}")

def log_error(sku: str, reason: str, site: str = "general") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _log_lock:
        try:
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] SITE={site} SKU={sku} REASON={reason}\n")
        except Exception as e:
            print(f"⚠️ Failed to write error log: {e}")

# ============================================
# AUTHENTICATION
# ============================================

def shopify_store_url() -> str:
    return os.environ.get("SHOPIFY_STORE_URL", SHOPIFY_STORE_URL).strip()


def shopify_api_version() -> str:
    return os.environ.get("SHOPIFY_API_VERSION", SHOPIFY_API_VERSION).strip()


def get_access_token() -> str:
    return os.environ.get("SHOPIFY_ACCESS_TOKEN", SHOPIFY_ACCESS_TOKEN).strip()


def verify_shopify_api(access_token: Optional[str] = None) -> Tuple[bool, str]:
    """Return (ok, message). Checks Admin API can reach the configured shop."""
    token = (access_token or get_access_token()).strip()
    store = shopify_store_url()
    if not store or not token:
        return False, "SHOPIFY_STORE_URL or SHOPIFY_ACCESS_TOKEN is empty"
    url = f"https://{store}/admin/api/{shopify_api_version()}/shop.json"
    try:
        r = requests.get(
            url,
            headers={"X-Shopify-Access-Token": token},
            timeout=20,
        )
    except Exception as e:
        return False, f"Shopify request failed: {e}"
    if r.status_code == 200:
        name = r.json().get("shop", {}).get("name") or store
        return True, f"Shopify OK — «{name}» ({store})"
    if r.status_code == 401:
        return False, (
            "Shopify token rejected (401). Regenerate Admin API token and set "
            "SHOPIFY_ACCESS_TOKEN (or env var)."
        )
    if r.status_code == 404:
        return False, (
            f"Shopify store not found: {store} (404). Shop may be closed/renamed — "
            f"update SHOPIFY_STORE_URL in update_prices_and_sheet.py or set env var."
        )
    return False, f"Shopify API {r.status_code}: {(r.text or '')[:200]}"


try:
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    print("[OK] Connected to Google Sheets")
    log_detailed("Google Sheets connected successfully")
except Exception as e:
    print(f"[ERR] Google Sheets auth error: {e}")
    exit(1)

# ============================================
# LOAD UPLOADED DATA
# ============================================

def load_uploaded_data():
    spreadsheet = safe_gs_call(client.open_by_key, UPLOADED_SPREADSHEET_ID)
    sheet = safe_gs_call(spreadsheet.worksheet, SHEET_NAME_PRODUCTS)
    all_data = safe_gs_call(sheet.get_all_values)

    total_rows = len(all_data)
    print(f"📊 Sheet has {total_rows} rows (including header).")
    log_detailed(f"Loaded sheet with {total_rows} rows total")

    if total_rows < 2:
        print("⚠️ Uploaded sheet is empty.")
        return [], {}, {}, -1, -1, -1, -1, -1

    headers = [str(h or "").strip() for h in all_data[0]]
    data_rows = all_data[1:]

    src_pid_col = None
    shopify_pid_col = None
    shopify_vid_col = None
    sku_col = None
    price_col = None
    cost_price_col = None
    avail_col = None
    avail1_col = None

    column_map = {
        'product_id': ['product_id', 'source product id'],
        'shopify_product_id': ['shopify_product_id', 'shopify product id'],
        'shopify_variant_id': ['shopify_variant_id', 'shopify variant id'],
        'reference': ['reference', 'sku', 'shopify_sku'],
        'price': ['price'],
        'cost_price': ['cost price', 'cost_price', 'costprice'],
        'availability': ['availability'],
        'availability_1': ['availability_1'],
        'url': ['url', 'product_url', 'product url'],
    }

    url_col = None
    brand_col = find_brand_column_index(headers)
    att_col_indices: List[Tuple[int, str]] = []

    for i, h in enumerate(headers):
        h_low = h.lower().strip()
        if h_low in column_map['product_id']:
            src_pid_col = i
        elif h_low in column_map['shopify_product_id']:
            shopify_pid_col = i
        elif h_low in column_map['shopify_variant_id']:
            shopify_vid_col = i
        elif h_low in column_map['reference']:
            sku_col = i
        elif h_low in column_map['price']:
            price_col = i
        elif h_low in column_map['cost_price']:
            cost_price_col = i
        elif h_low in column_map['availability']:
            avail_col = i
        elif h_low in column_map['availability_1']:
            avail1_col = i
        elif h_low in column_map['url']:
            url_col = i
        elif h_low.startswith('att_'):
            att_col_indices.append((i, h))

    required = {
        'src_pid_col': src_pid_col,
        'shopify_pid_col': shopify_pid_col,
        'shopify_vid_col': shopify_vid_col,
        'sku_col': sku_col,
        'price_col': price_col,
        'avail_col': avail_col,
        'avail1_col': avail1_col
    }
    missing = [name for name, idx in required.items() if idx is None]
    if missing:
        print(f"❌ Missing required columns in uploaded sheet: {', '.join(missing)}")
        log_detailed(f"Missing columns: {missing}")
        return [], {}, {}, -1, -1, -1, -1, -1

    variants = []
    sku_to_row = {}
    product_to_skus = {}
    skipped_empty_sku = 0
    skipped_short_row = 0

    for row_idx, row in enumerate(data_rows, start=2):
        if len(row) <= max(src_pid_col, shopify_pid_col, shopify_vid_col, sku_col, price_col, avail_col, avail1_col):
            skipped_short_row += 1
            continue
        original_sku = row[sku_col].strip()
        if not original_sku:
            skipped_empty_sku += 1
            continue
        src_pid = row[src_pid_col].strip()
        attributes = {}
        for att_i, att_name in att_col_indices:
            if att_i < len(row) and str(row[att_i] or "").strip():
                att_key = str(att_name).strip().lower().replace("-", "_")
                attributes[att_key] = str(row[att_i]).strip()

        brand_val = ""
        if brand_col >= 0 and brand_col < len(row):
            brand_val = str(row[brand_col] or "").strip()

        variants.append({
            'row_idx': row_idx,
            'sku': original_sku,
            'brand': brand_val,
            'update_prices': True,
            'clean_sku': clean_sku(original_sku),
            'shopify_variant_id': normalize_shopify_id(row[shopify_vid_col]),
            'shopify_product_id': normalize_shopify_id(row[shopify_pid_col]),
            'source_product_id': src_pid,
            'product_url': row[url_col].strip() if url_col is not None and url_col < len(row) else "",
            'attributes': attributes,
            'current_price': row[price_col].strip() if price_col < len(row) else "",
            'current_cost_price': (
                row[cost_price_col].strip()
                if cost_price_col is not None and cost_price_col < len(row)
                else ""
            ),
            'current_availability': row[avail_col].strip() if avail_col < len(row) else "",
            'current_availability_1': row[avail1_col].strip() if avail1_col < len(row) else ""
        })
        sku_to_row[original_sku] = row_idx
        if src_pid not in product_to_skus:
            product_to_skus[src_pid] = []
        product_to_skus[src_pid].append(original_sku)

    product_variant_count = {pid: len(skus) for pid, skus in product_to_skus.items()}

    print(f"✅ Loaded {len(variants)} variants from uploaded sheet.")
    print(f"   Skipped: {skipped_empty_sku} rows with empty SKU, {skipped_short_row} rows with insufficient columns.")
    if brand_col < 0:
        print("   ⚠️ No brand column (Brand/Brand1/Vendor/Merk) — all rows will update prices if enabled in config.")
    log_detailed(f"Loaded {len(variants)} variants, {len(product_variant_count)} products")
    return variants, product_variant_count, sku_to_row, price_col, cost_price_col, avail_col, avail1_col


def ensure_cost_price_column(sheet, headers: List[str]) -> Tuple[List[str], int]:
    """Ensure ``cost price`` column exists; append if missing."""
    for i, h in enumerate(headers):
        if str(h or "").strip().lower() in ("cost price", "cost_price", "costprice"):
            return headers, i
    safe_gs_call(sheet.update_cell, 1, len(headers) + 1, COST_PRICE_COLUMN)
    new_headers = headers + [COST_PRICE_COLUMN]
    return new_headers, len(headers)

def clean_sku(sku: str) -> str:
    """Strip leading ``#`` only (legacy distribution search / card match)."""
    if not sku:
        return ""
    return str(sku).strip().lstrip("#").strip()


def sheet_col_letter(index_zero_based: int) -> str:
    """A, B, … Z, AA, AB… for gspread range updates."""
    n = index_zero_based + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

# ============================================
# PRICE CALCULATION & AVAILABILITY CLEANING
# ============================================

def calculate_final_price(
    sku: str,
    scraped_price_str: str,
    *,
    line_log: Optional[Callable[[str, str], None]] = None,
) -> Optional[str]:
    def _log(s: str, msg: str) -> None:
        if line_log:
            line_log(s, msg)
        else:
            print(f"   [SKU {s}] {msg}")
        log_detailed(f"SKU={s} {msg}")

    result = _calc_final_price(scraped_price_str, sku, log_fn=_log)
    if result is None and scraped_price_str:
        log_error(sku, "Price calculation failed", site="price")
    return result


def calculate_motorsport_fallback_price(
    sku: str,
    scraped_price_str: str,
    *,
    line_log: Optional[Callable[[str, str], None]] = None,
) -> Optional[str]:
    def _log(s: str, msg: str) -> None:
        if line_log:
            line_log(s, msg)
        else:
            print(f"   [SKU {s}] {msg}")
        log_detailed(f"SKU={s} {msg}")

    result = _calc_motor_fallback_price(scraped_price_str, sku, log_fn=_log)
    if result is None and scraped_price_str:
        log_error(sku, "Motorsport fallback price calculation failed", site="price")
    return result


def calculate_retail_price(
    sku: str,
    scraped_price_str: str,
    *,
    retail_source: str = "distribution",
    line_log: Optional[Callable[[str, str], None]] = None,
) -> Optional[str]:
    """Distribution retail uses ×1.21+15; motorsport fallback uses +15 only."""
    if retail_source == "motorsport":
        return calculate_motorsport_fallback_price(
            sku, scraped_price_str, line_log=line_log
        )
    return calculate_final_price(sku, scraped_price_str, line_log=line_log)


def calculate_cost_price(
    sku: str,
    scraped_price_str: str,
    *,
    line_log: Optional[Callable[[str, str], None]] = None,
) -> Optional[str]:
    def _log(s: str, msg: str) -> None:
        if line_log:
            line_log(s, msg)
        else:
            print(f"   [SKU {s}] {msg}")
        log_detailed(f"SKU={s} {msg}")

    result = _calc_cost_price(scraped_price_str, sku, log_fn=_log)
    if result is None and scraped_price_str:
        log_error(sku, "Cost price calculation failed", site="distribution")
    return result

def clean_availability(value: str) -> str:
    if not value:
        return ""
    v = value.strip().lower()
    if "in stock" in v or "en stock" in v:
        return "3"
    if "out of stock" in v:
        return "20"
    if "60 days" in v:
        return "20"
    if "10-14 days" in v:
        return "10"
    return "10"

def clean_availability_1(value: str) -> str:
    return clean_availability(value)

# ============================================
# AVAILABILITY SITE – EXACT VARIANT MATCH (legacy helper)
# ============================================

def select_option_by_value(driver, wait, fieldset_index, value):
    select_xpath = f'//*[@id="attributes"]/fieldset[{fieldset_index}]//select'
    try:
        select = wait.until(EC.element_to_be_clickable((By.XPATH, select_xpath)))
    except:
        return False
    try:
        select.click()
    except:
        driver.execute_script("arguments[0].click();", select)
    time.sleep(0.2)
    option_elem = None
    try:
        options = select.find_elements(By.TAG_NAME, "option")
        for o in options:
            if o.get_attribute("value") == value:
                option_elem = o
                break
    except:
        pass
    if not option_elem:
        return False
    try:
        option_elem.click()
    except:
        driver.execute_script("arguments[0].click();", option_elem)
    try:
        driver.execute_script("arguments[0].selected = true;", option_elem)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", select)
    except:
        pass
    time.sleep(0.5)
    return True

def wait_for_availability_text(driver, sku, max_wait=20):
    availability_xpaths = [
        '//*[@id="center_column"]/div/div[1]/div/div/div[2]/div[2]/div[2]/div[1]/p',
        '//p[contains(@class, "availability")]',
        '//span[@id="availability_value"]',
        '//div[@id="availability_statut"]//span',
    ]
    start = time.time()
    while time.time() - start < max_wait:
        for xpath in availability_xpaths:
            try:
                elem = driver.find_element(By.XPATH, xpath)
                if elem.is_displayed():
                    text = elem.text.strip()
                    if text:
                        return text
            except:
                pass
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            for line in body.split('\n'):
                lower = line.lower()
                if any(k in lower for k in ["in stock", "en stock", "out of stock", "days"]):
                    return line.strip()
        except:
            pass
        time.sleep(1)
    print(f"   [SKU {sku}] ⚠️ Availability text not found after {max_wait}s")
    log_detailed(f"SKU={sku} availability text not found")
    log_error(sku, "Availability text not found", site="availability")
    return ""

def get_price_and_availability_from_availability_site(driver, sku: str) -> Tuple[Optional[str], Optional[str]]:
    driver.get(AVAILABILITY_SITE_BASE)
    wait = WebDriverWait(driver, 10)

    try:
        search = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="search_query_top"]')))
        search.clear()
        search.send_keys(sku)
        search.send_keys(Keys.ENTER)
        time.sleep(2)
    except Exception as e:
        msg = f"Search error: {e}"
        print(f"   [SKU {sku}] {msg}")
        log_detailed(f"SKU={sku} availability search error: {e}")
        log_error(sku, f"Search error: {e}", site="availability")
        return None, None

    product_links = []
    items = driver.find_elements(By.XPATH, '//li[contains(@class, "ajax_block_product")]//a[contains(@class, "product_img_link")]')
    if not items:
        items = driver.find_elements(By.XPATH, '//*[@id="center_column"]//a[contains(@href, "/en/") and contains(@href, ".html")]')
    for it in items:
        href = it.get_attribute("href")
        if href and href not in product_links:
            product_links.append(href)
    if not product_links and "product" in driver.current_url:
        product_links = [driver.current_url]

    print(f"   [SKU {sku}] Found {len(product_links)} product links.")
    log_detailed(f"SKU={sku} availability found {len(product_links)} product links")
    if not product_links:
        log_error(sku, "No product links found", site="availability")
        return None, None

    original_window = driver.current_window_handle
    for idx, url in enumerate(product_links, 1):
        print(f"   [SKU {sku}] Checking product {idx}: {url[:80]}...")
        log_detailed(f"SKU={sku} availability checking product {idx}: {url[:80]}")
        driver.execute_script("window.open(arguments[0]);", url)
        driver.switch_to.window(driver.window_handles[-1])
        time.sleep(2)

        variations_meta = []
        try:
            fieldsets = driver.find_elements(By.XPATH, '//*[@id="attributes"]/fieldset')
            for f_idx, fs in enumerate(fieldsets, start=1):
                label = fs.find_element(By.TAG_NAME, "label").text.strip()
                select = fs.find_element(By.TAG_NAME, "select")
                options = select.find_elements(By.TAG_NAME, "option")
                valid = []
                for opt in options:
                    val = opt.get_attribute("value")
                    text = opt.text.strip()
                    if val == "" or "select" in text.lower():
                        continue
                    valid.append({"value": val, "text": text})
                if label and valid:
                    variations_meta.append((label, f_idx, valid))
        except:
            pass

        matched = False
        if variations_meta:
            all_option_lists = [v[2] for v in variations_meta]
            for combo in itertools.product(*all_option_lists):
                for (label, field_idx, _), chosen in zip(variations_meta, combo):
                    select_option_by_value(driver, wait, field_idx, chosen["value"])
                time.sleep(1.5)
                try:
                    sku_elem = driver.find_element(By.XPATH, '//*[@id="product_reference"]/span')
                    if sku_matches_on_page(sku_elem.text.strip(), sku):
                        matched = True
                        break
                except:
                    continue
        else:
            try:
                sku_elem = driver.find_element(By.XPATH, '//*[@id="product_reference"]/span')
                if sku_matches_on_page(sku_elem.text.strip(), sku):
                    matched = True
            except:
                pass

        if matched:
            print(f"   [SKU {sku}] ✅ MATCH FOUND with SKU {sku}!")
            log_detailed(f"SKU={sku} availability variant matched")
            price_raw = None
            for sel in [
                '//*[@id="old_price_display"]',
                '//*[@id="our_price_display"]',
                '//span[@itemprop="price"]',
                '//span[@class="price"]',
            ]:
                try:
                    pe = driver.find_element(By.XPATH, sel)
                    if pe.is_displayed():
                        price_raw = pe.text.strip()
                        break
                except:
                    pass
            avail_raw = wait_for_availability_text(driver, sku)
            driver.close()
            driver.switch_to.window(original_window)
            return price_raw, avail_raw
        else:
            driver.close()
            driver.switch_to.window(original_window)

    print(f"   [SKU {sku}] ❌ No matching product/variant found on availability site.")
    log_detailed(f"SKU={sku} availability no matching variant")
    log_error(sku, "No matching variant found", site="availability")
    return None, None

_MOTOR_PRODUCT_ID_FAIL = frozenset(
    {"product_not_found", "url_not_found", "sku_mismatch"}
)


def _motorsport_ok_status(status: str) -> bool:
    if status in ("ok", "ok_legacy_sku_search"):
        return True
    return str(status).startswith("ok_page_ref_")


def _motorsport_search_key(reference: str) -> str:
    base = strip_single_digit_variant_suffix(reference) or str(reference).strip()
    return clean_sku(base) if base else ""


def scrape_motorsport_for_upload_row(
    driver,
    worker_id: int,
    original_sku: str,
    src_pid,
    product_url: str,
    attributes: Optional[Dict[str, str]],
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Motorsport PDP scrape (Product_ID + URL + att_*), then legacy site search if that fails.
    """
    motor_price, avail_raw, scrape_status = scrape_price_and_availability(
        driver,
        product_id=src_pid,
        reference=original_sku,
        url=product_url or "",
        attributes=attributes,
    )
    need_legacy = (not motor_price and not avail_raw) or scrape_status in _MOTOR_PRODUCT_ID_FAIL
    if need_legacy:
        search_key = _motorsport_search_key(original_sku)
        if search_key:
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Motorsport Product_ID/URL failed ({scrape_status}); "
                f"legacy SKU search «{search_key}»…",
            )
            legacy_price, legacy_avail = get_price_and_availability_from_availability_site(
                driver, search_key
            )
            if legacy_price and not motor_price:
                motor_price = legacy_price
            if legacy_avail and not avail_raw:
                avail_raw = legacy_avail
            if legacy_price or legacy_avail:
                scrape_status = "ok_legacy_sku_search"
    return motor_price, avail_raw, scrape_status

# ============================================
# SHOPIFY UPDATE FUNCTIONS
# ============================================

def shopify_cost_api_enabled() -> bool:
    return _shopify_cost_api_enabled


def shopify_list_access_scopes(access_token: str) -> List[str]:
    """Return scope handles granted to the current Admin API token."""
    url = f"https://{shopify_store_url()}/admin/oauth/access_scopes.json"
    headers = {"X-Shopify-Access-Token": access_token}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            log_detailed(f"access_scopes GET {r.status_code}: {(r.text or '')[:200]}")
            return []
        return [
            str(s.get("handle") or "").strip()
            for s in r.json().get("access_scopes", [])
            if s.get("handle")
        ]
    except Exception as e:
        log_detailed(f"access_scopes exception: {e}")
        return []


def configure_shopify_cost_api(access_token: str) -> bool:
    """
    Check token scopes once at startup. Cost uses inventory_items API → write_inventory.
    """
    global _shopify_cost_api_enabled
    scopes = shopify_list_access_scopes(access_token)
    if scopes and WRITE_INVENTORY_SCOPE not in scopes:
        _shopify_cost_api_enabled = False
        print(SHOPIFY_COST_SCOPE_HELP)
        print(f"\n   Token scopes now: {', '.join(scopes)}\n")
        log_error(
            "global",
            f"missing {WRITE_INVENTORY_SCOPE}; cost → sheet only",
            site="shopify",
        )
        return False
    if scopes:
        log_detailed(f"Shopify scopes OK for cost ({WRITE_INVENTORY_SCOPE} present)")
    return _shopify_cost_api_enabled


def _disable_shopify_cost_api(reason: str) -> None:
    global _shopify_cost_api_enabled
    with _shopify_cost_lock:
        if _shopify_cost_api_enabled:
            _shopify_cost_api_enabled = False
            print(SHOPIFY_COST_SCOPE_HELP)
            log_error("global", reason, site="shopify")


def _price_for_shopify_api(formatted_price: str) -> str:
    """Sheet/UI uses '109,00'; Shopify REST API expects '109.00'."""
    val = parse_scraped_price(formatted_price)
    if val is not None:
        return f"{val:.2f}"
    return str(formatted_price or "").replace(",", ".").strip()


def update_variant_price(
    variant_id: str,
    new_price: str,
    access_token: str,
    *,
    sku: str = "",
) -> bool:
    vid = shopify_id_int(variant_id)
    if vid is None:
        log_error(sku or str(variant_id), f"invalid shopify_variant_id={variant_id!r}", site="shopify")
        return False
    url = f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/variants/{vid}.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": access_token}
    price_for_shopify = _price_for_shopify_api(new_price)
    data = {"variant": {"id": vid, "price": price_for_shopify}}
    try:
        r = requests.put(url, headers=headers, json=data, timeout=30)
        if r.status_code != 200:
            log_error(
                sku,
                f"variant price PUT {r.status_code}: {(r.text or '')[:300]}",
                site="shopify",
            )
        return r.status_code == 200
    except Exception as e:
        log_error(sku, f"variant price exception: {e}", site="shopify")
        return False


def update_variant_cost(
    variant_id: str,
    cost_price: str,
    access_token: str,
    *,
    sku: str = "",
) -> bool:
    """
    Set Shopify admin Pricing → Cost (inventory item cost per unit).
    Requires Admin API scope ``write_inventory`` on the access token.
    """
    if not shopify_cost_api_enabled():
        return False
    if not str(cost_price or "").strip():
        return False
    vid = shopify_id_int(variant_id)
    if vid is None:
        log_error(sku or str(variant_id), f"invalid shopify_variant_id={variant_id!r}", site="shopify")
        return False
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": access_token}
    cost_api = _price_for_shopify_api(cost_price)
    try:
        v_url = (
            f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/"
            f"variants/{vid}.json"
        )
        r = requests.get(v_url, headers=headers, timeout=30)
        if r.status_code != 200:
            log_error(
                sku,
                f"variant GET {r.status_code}: {(r.text or '')[:300]}",
                site="shopify",
            )
            return False
        inv_id = r.json().get("variant", {}).get("inventory_item_id")
        if not inv_id:
            log_error(sku, "variant has no inventory_item_id", site="shopify")
            return False
        inv_id_int = shopify_id_int(inv_id) or int(inv_id)
        inv_url = (
            f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/"
            f"inventory_items/{inv_id_int}.json"
        )
        data = {"inventory_item": {"id": inv_id_int, "cost": cost_api}}
        r2 = requests.put(inv_url, headers=headers, json=data, timeout=30)
        if r2.status_code != 200:
            body = (r2.text or "")[:300]
            if r2.status_code == 403 and "write_inventory" in body:
                _disable_shopify_cost_api(
                    "inventory_items PUT 403: write_inventory not approved"
                )
            else:
                log_error(
                    sku,
                    f"inventory_items PUT {r2.status_code}: {body}",
                    site="shopify",
                )
        return r2.status_code == 200
    except Exception as e:
        log_error(sku, f"inventory cost exception: {e}", site="shopify")
        return False

def update_variant_metafield(variant_id, namespace, key, value, access_token):
    url = f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/variants/{variant_id}/metafields.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": access_token}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            for mf in r.json().get('metafields', []):
                if mf.get('namespace') == namespace and mf.get('key') == key:
                    update_url = f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/metafields/{mf['id']}.json"
                    update_data = {"metafield": {"id": mf['id'], "value": value, "type": "single_line_text_field"}}
                    r2 = requests.put(update_url, headers=headers, json=update_data, timeout=30)
                    return r2.status_code == 200
    except:
        pass
    create_data = {"metafield": {"namespace": namespace, "key": key, "value": value, "type": "single_line_text_field"}}
    try:
        r = requests.post(url, headers=headers, json=create_data, timeout=30)
        return r.status_code == 201
    except:
        return False

def update_product_metafield(product_id, namespace, key, value, access_token):
    url = f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/products/{product_id}/metafields.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": access_token}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            for mf in r.json().get('metafields', []):
                if mf.get('namespace') == namespace and mf.get('key') == key:
                    update_url = f"https://{shopify_store_url()}/admin/api/{shopify_api_version()}/metafields/{mf['id']}.json"
                    update_data = {"metafield": {"id": mf['id'], "value": value, "type": "single_line_text_field"}}
                    r2 = requests.put(update_url, headers=headers, json=update_data, timeout=30)
                    return r2.status_code == 200
    except:
        pass
    create_data = {"metafield": {"namespace": namespace, "key": key, "value": value, "type": "single_line_text_field"}}
    try:
        r = requests.post(url, headers=headers, json=create_data, timeout=30)
        return r.status_code == 201
    except:
        return False

# ============================================
# ROW PROCESSING – sequential, immediate sheet + Shopify per row
# ============================================

def update_sheet_cell(sheet, row_idx, col_idx, new_value, delay=SHEET_UPDATE_DELAY):
    try:
        col_letter = sheet_col_letter(col_idx)
        with _sheet_write_lock:
            safe_gs_call(sheet.update, values=[[new_value]], range_name=f"{col_letter}{row_idx}")
            time.sleep(delay)
        return True
    except Exception as e:
        print(f"   ⚠️ Failed to update sheet: {e}")
        return False


def _apply_row_updates_immediately(
    *,
    original_sku: str,
    row_num: int,
    vid: str,
    pid: str,
    src_pid: str,
    access_token: str,
    product_variant_count: Dict[str, int],
    write_sheet,
    price_col: int,
    cost_price_col: int,
    avail_col: int,
    avail1_col: int,
    old_price: str,
    old_cost: str,
    old_avail: str,
    old_avail1: str,
    new_cost_price: Optional[str],
    new_price: Optional[str],
    new_avail: Optional[str],
    new_avail1: Optional[str],
    worker_id: int = 0,
    force_write: bool = False,
) -> Dict:
    """Push cost + retail + availability to Shopify and Google Sheet right after scrapes."""

    def _same_price(a: str, b: str) -> bool:
        va = parse_scraped_price(a)
        vb = parse_scraped_price(b)
        if va is not None and vb is not None:
            return abs(va - vb) < 0.005
        return str(a or "").strip() == str(b or "").strip()

    cost_changed = new_cost_price is not None and (
        force_write or not _same_price(new_cost_price, old_cost)
    )
    price_changed = new_price is not None and (
        force_write or not _same_price(new_price, old_price)
    )
    avail_changed = new_avail is not None and new_avail != old_avail
    avail1_changed = new_avail1 is not None and new_avail1 != old_avail1
    shopify_ok = True

    if new_cost_price is not None:
        label = "unchanged" if not cost_changed else "updated"
        dest = "sheet + Shopify Cost" if shopify_cost_api_enabled() else "sheet only (no write_inventory)"
        wprint(worker_id, f"   [SKU {original_sku}] Writing cost → {dest} ({label})…")
        if cost_price_col != -1:
            update_sheet_cell(write_sheet, row_num, cost_price_col, new_cost_price)
        if vid:
            if not shopify_cost_api_enabled():
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] ✅ sheet cost = {new_cost_price} "
                    f"(Shopify Cost skipped — enable write_inventory on app token)",
                )
                log_detailed(f"SKU={original_sku} cost={new_cost_price} sheet_only")
            elif update_variant_cost(vid, new_cost_price, access_token, sku=original_sku):
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] ✅ sheet cost + Shopify Cost = {new_cost_price}",
                )
                log_detailed(f"SKU={original_sku} cost={new_cost_price}")
            else:
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] ✅ sheet cost = {new_cost_price} "
                    f"(Shopify Cost failed — see {ERROR_LOG_FILE})",
                )
                log_detailed(f"SKU={original_sku} cost={new_cost_price} sheet_only")
                shopify_ok = False
        else:
            wprint(worker_id, f"   [SKU {original_sku}] ⚠️ No shopify_variant_id — sheet only")

    if new_price is not None and (price_changed or force_write):
        if force_write and not price_changed:
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Writing price (forced) → sheet + Shopify…",
            )
        else:
            wprint(worker_id, f"   [SKU {original_sku}] Writing price → sheet + Shopify…")
        if price_col != -1:
            update_sheet_cell(write_sheet, row_num, price_col, new_price)
        if vid and update_variant_price(vid, new_price, access_token, sku=original_sku):
            wprint(worker_id, f"   [SKU {original_sku}] ✅ sheet price + Shopify Price")
            log_detailed(f"SKU={original_sku} price={new_price}")
        elif vid:
            wprint(
                worker_id,
                f"   [SKU {original_sku}] ✅ sheet price = {new_price} "
                f"(Shopify Price failed — see {ERROR_LOG_FILE})",
            )
            log_detailed(f"SKU={original_sku} price={new_price} sheet_only")
            shopify_ok = False
        else:
            wprint(worker_id, f"   [SKU {original_sku}] ✅ sheet price = {new_price} (no shopify_variant_id)")
            log_detailed(f"SKU={original_sku} price={new_price} sheet_only")

    is_single = product_variant_count.get(src_pid, 1) == 1
    if is_single:
        if avail_changed:
            if update_product_metafield(pid, "custom", "shipping_time", new_avail, access_token):
                update_sheet_cell(write_sheet, row_num, avail_col, new_avail)
            else:
                shopify_ok = False
        if avail1_changed:
            if update_product_metafield(pid, "custom", "shipping_time_margin", new_avail1, access_token):
                update_sheet_cell(write_sheet, row_num, avail1_col, new_avail1)
            else:
                shopify_ok = False
    else:
        if avail_changed:
            if update_variant_metafield(vid, "custom", "shipping_time", new_avail, access_token):
                update_sheet_cell(write_sheet, row_num, avail_col, new_avail)
            else:
                shopify_ok = False
        if avail1_changed:
            if update_variant_metafield(vid, "custom", "shipping_time_margin", new_avail1, access_token):
                update_sheet_cell(write_sheet, row_num, avail1_col, new_avail1)
            else:
                shopify_ok = False

    if not (cost_changed or price_changed or avail_changed or avail1_changed):
        if new_cost_price is None and new_price is None and new_avail is None:
            return {
                "sku": original_sku,
                "cost_changed": False,
                "price_changed": False,
                "avail_changed": False,
                "avail1_changed": False,
                "shopify_ok": False,
                "error": "No data found",
            }
        return {
            "sku": original_sku,
            "cost_changed": False,
            "price_changed": False,
            "avail_changed": False,
            "avail1_changed": False,
            "shopify_ok": True,
            "error": None,
        }

    return {
        "sku": original_sku,
        "new_cost": new_cost_price if cost_changed else None,
        "new_price": new_price if price_changed else None,
        "new_avail": new_avail if avail_changed else None,
        "new_avail1": new_avail1 if avail1_changed else None,
        "cost_applied": new_cost_price is not None,
        "cost_changed": cost_changed,
        "price_changed": price_changed,
        "avail_changed": avail_changed,
        "avail1_changed": avail1_changed,
        "shopify_ok": shopify_ok,
        "error": None if shopify_ok else "Shopify update failed",
    }


def process_variants_chunk(
    variants_chunk: List[Dict],
    access_token: str,
    product_variant_count: Dict[str, int],
    sku_to_row: Dict[str, int],
    price_col: int,
    cost_price_col: int,
    avail_col: int,
    avail1_col: int,
    write_sheet,
    worker_id: int = 0,
) -> List[Dict]:
    driver = Driver(headless=False)
    _register_driver(driver)
    driver.maximize_window()
    wprint(
        worker_id,
        f"Opening Chrome for {len(variants_chunk)} row(s) "
        f"(login once, browser closes when chunk is done)",
    )

    def row_log(s: str, msg: str) -> None:
        wprint(worker_id, f"   [SKU {s}] {msg}")

    driver.execute_script("window.open('');")
    tabs = driver.window_handles
    dist_tab = tabs[0]
    motor_tab = tabs[1]

    dist_login_ok = False
    driver.switch_to.window(dist_tab)
    if DISTRIBUTION_EMAIL and DISTRIBUTION_PASSWORD:
        wprint(worker_id, "Logging into All-Stars Distribution…")
        dist_login_ok = login_distribution_site(
            driver, DISTRIBUTION_EMAIL, DISTRIBUTION_PASSWORD
        )
        if dist_login_ok:
            wprint(worker_id, "Distribution login OK")
            log_detailed(f"Worker {worker_id} distribution login OK")
        else:
            wprint(worker_id, "Distribution login failed — cost price skipped")
            log_error("global", "Distribution login failed", site="distribution")
    else:
        wprint(worker_id, "Distribution credentials missing — cost price skipped")

    driver.switch_to.window(motor_tab)
    driver.get(MOTORSPORT_SITE_BASE)
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="search_query_top"]'))
        )
    except Exception:
        pass
    log_detailed(f"Worker {worker_id} motorsport site ready")

    def _dist_log(sku: str, msg: str) -> None:
        log_detailed(f"SKU={sku} {msg}")

    rows_sorted = sorted(
        variants_chunk,
        key=lambda v: int(v.get("row_idx") or sku_to_row.get(v.get("sku", ""), 999999)),
    )
    results = []
    total = len(rows_sorted)
    for seq, var in enumerate(rows_sorted, start=1):
        original_sku = var['sku']
        clean_sku_val = var['clean_sku']
        vid = var['shopify_variant_id']
        pid = var['shopify_product_id']
        src_pid = var['source_product_id']
        product_url = var.get('product_url', '')
        attributes = var.get('attributes', {})
        old_price = var['current_price'] or ""
        old_cost = var.get('current_cost_price') or ""
        old_avail = var['current_availability'] or ""
        old_avail1 = var['current_availability_1'] or ""
        row_num = int(var.get("row_idx") or sku_to_row.get(original_sku, 0))
        update_prices = bool(var.get("update_prices", True))

        wprint(
            worker_id,
            f"\n[Row {seq}/{total}] sheet row {row_num} | SKU: {original_sku} "
            f"(clean: {clean_sku_val}) | product_id: {src_pid} | "
            f"prices={'ON' if update_prices else 'OFF'}",
        )
        wprint(
            worker_id,
            f"   Current → Price={old_price or '(empty)'}, Cost={old_cost or '(empty)'}, "
            f"Avail={old_avail or '(empty)'}",
        )

        # --- Scrape cost + retail (All-Stars Distribution) for this row ---
        new_cost_price = None
        dist_retail_raw = None
        dist_status = None
        if dist_login_ok and update_prices:
            driver.switch_to.window(dist_tab)
            dist_search = distribution_search_sku(original_sku, attributes)
            dist_match = distribution_card_match_sku(original_sku, attributes)
            if dist_search != dist_match or dist_search.upper() != str(
                original_sku
            ).strip().lstrip("#").upper():
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] Distribution search={dist_search} "
                    f"card_match={dist_match}",
                )
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Scraping distribution (cost + retail xpaths)…",
            )
            raw_cost, raw_retail, dist_status = scrape_distribution_cost_and_retail(
                driver,
                original_sku,
                attributes=attributes,
                log_fn=_dist_log,
            )
            pack_mult = vwr_ignition_coil_pack_multiplier(original_sku)
            if pack_mult > 1:
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] VWR ignition pack ×{pack_mult} "
                    f"(per-coil scraped raw → pack total before +15 / ×1.21+15)",
                )
                if raw_cost:
                    raw_cost = apply_vwr_pack_multiplier_to_raw(original_sku, raw_cost)
                if raw_retail:
                    raw_retail = apply_vwr_pack_multiplier_to_raw(original_sku, raw_retail)
            if raw_cost:
                new_cost_price = calculate_cost_price(
                    original_sku, raw_cost, line_log=row_log
                )
            if raw_retail:
                dist_retail_raw = raw_retail
            if raw_cost or raw_retail:
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] Distribution OK ({dist_status}): "
                    f"cost_raw={raw_cost or '(none)'} → cost+15={new_cost_price or '—'} | "
                    f"retail_raw={raw_retail or '(none)'}",
                )
                if raw_cost and not raw_retail:
                    wprint(
                        worker_id,
                        f"   [SKU {original_sku}] Distribution: cost xpath only — "
                        f"retail must come from distribution retail xpath or motorsport +15",
                    )
            else:
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] Distribution scrape failed: {dist_status}",
                )
                log_error(original_sku, dist_status, site="distribution")
        elif not update_prices:
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Distribution scrape skipped — price updates disabled for brand",
            )
        else:
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Distribution scrape skipped — no distribution login",
            )

        # --- Motorsport: availability; retail fallback if distribution had no price ---
        driver.switch_to.window(motor_tab)
        new_avail = new_avail1 = None
        motorsport_retail_raw = None
        if attributes:
            att_parts = []
            for k, v in sorted(attributes.items()):
                if not str(v).strip():
                    continue
                key = str(k).strip().lower().replace("-", "_")
                en = english_pdp_label_from_upload_value(key, v)
                if en:
                    att_parts.append(f"{k}={v} → PDP '{en}'")
                else:
                    att_parts.append(
                        f"{k}={v} (no entry in att_value_translation_lookup.json)"
                    )
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Motorsport variant (upload → JSON → PDP): "
                + "; ".join(att_parts),
            )
        motor_price, avail_raw, scrape_status = scrape_motorsport_for_upload_row(
            driver,
            worker_id,
            original_sku,
            src_pid,
            product_url or "",
            attributes,
        )
        log_detailed(f"SKU={original_sku} motorsport status={scrape_status}")
        motorsport_ok = _motorsport_ok_status(scrape_status)
        if motor_price:
            motorsport_retail_raw = apply_vwr_pack_multiplier_to_raw(
                original_sku, motor_price
            )
        if not motorsport_ok:
            hint = ""
            if scrape_status == "variant_not_matched" and attributes:
                hint = (
                    " (upload att_* must map via att_value_translation_lookup.json "
                    "to English PDP options — run build_att_translation_json.py if missing)"
                )
            wprint(worker_id, f"   [SKU {original_sku}] Motorsport: {scrape_status}{hint}")
            if scrape_status not in ("price_not_found",):
                log_error(original_sku, scrape_status, site="motorsport")
        if avail_raw:
            new_avail = clean_availability(avail_raw)
            new_avail1 = clean_availability_1(avail_raw)
            wprint(worker_id, f"   [SKU {original_sku}] Availability: {new_avail}")

        retail_raw = dist_retail_raw
        retail_source = "distribution"
        if not retail_raw and motorsport_retail_raw and update_prices and motorsport_ok:
            retail_raw = motorsport_retail_raw
            retail_source = "motorsport"
            steps = motorsport_fallback_steps(motorsport_retail_raw)
            if steps:
                scraped, after_add, final_str = steps
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] Retail: motorsport fallback "
                    f"({dist_status or 'no_dist_retail'}) | raw={scraped} | "
                    f"+15→{after_add:.2f} → {final_str}",
                )
            else:
                wprint(
                    worker_id,
                    f"   [SKU {original_sku}] Distribution had no retail — "
                    f"motorsport fallback raw={retail_raw}",
                )
            log_detailed(f"SKU={original_sku} retail fallback motorsport raw={retail_raw}")
        elif update_prices and not retail_raw and motorsport_retail_raw and not motorsport_ok:
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Motorsport price ignored ({scrape_status}) — "
                f"no trusted retail",
            )

        new_price = None
        if retail_raw and update_prices:
            new_price = calculate_retail_price(
                original_sku,
                retail_raw,
                retail_source=retail_source,
                line_log=row_log,
            )
            formula = "+15" if retail_source == "motorsport" else "×1.21+15"
            wprint(
                worker_id,
                f"   [SKU {original_sku}] Retail from {retail_source} raw={retail_raw} → "
                f"price ({formula})={new_price or 'calc failed'}",
            )

        # --- Immediate sheet + Shopify for this row (cost, then price, then avail) ---
        result = _apply_row_updates_immediately(
            original_sku=original_sku,
            row_num=row_num,
            vid=vid,
            pid=pid,
            src_pid=src_pid,
            access_token=access_token,
            product_variant_count=product_variant_count,
            write_sheet=write_sheet,
            price_col=price_col,
            cost_price_col=cost_price_col,
            avail_col=avail_col,
            avail1_col=avail1_col,
            old_price=old_price,
            old_cost=old_cost,
            old_avail=old_avail,
            old_avail1=old_avail1,
            new_cost_price=new_cost_price,
            new_price=new_price,
            new_avail=new_avail,
            new_avail1=new_avail1,
            worker_id=worker_id,
        )
        if result.get("error") == "No data found":
            wprint(worker_id, f"   [SKU {original_sku}] ⚠️ No data found")
        elif not (
            result.get("cost_applied")
            or result.get("price_changed")
            or result.get("avail_changed")
            or result.get("avail1_changed")
        ):
            wprint(worker_id, f"   [SKU {original_sku}] ✅ Already up-to-date")
        else:
            parts = []
            if result.get("cost_applied"):
                parts.append("cost")
            if result.get("price_changed"):
                parts.append("price")
            if result.get("avail_changed") or result.get("avail1_changed"):
                parts.append("availability")
            wprint(worker_id, f"   [SKU {original_sku}] ✅ Row saved: {', '.join(parts)}")
        results.append(result)

        wprint(worker_id, f"[DONE row {row_num}] {seq}/{total}")
        emit_row_progress(worker_id, row_num, seq, total)
        time.sleep(0.5)

    _unregister_driver(driver)
    driver.quit()
    return results

# ============================================
# MAIN LOOP
# ============================================

def split_variants_across_workers(
    variants: List[Dict], workers: int
) -> List[List[Dict]]:
    """
    Round-robin split so worker 1 gets rows 1,7,13… worker 2 gets 2,8,14…
    (same interleaving as old “batch of N” parallelism, but one browser per worker).
    """
    n = max(1, int(workers))
    chunks: List[List[Dict]] = [[] for _ in range(n)]
    for i, var in enumerate(variants):
        chunks[i % n].append(var)
    return [c for c in chunks if c]


def update_all(workers: int = MAX_WORKERS, brand_config_path: Optional[str] = None) -> None:
    workers = max(1, int(workers))
    print("=" * 70)
    print("💰🚚 UPDATER (distribution cost+retail, motorsport availability, per-row sheet+Shopify)")
    print("=" * 70)

    # Clear log files at start
    try:
        with open(DETAILED_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Detailed log started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        with open(ERROR_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Error log started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        print(f"📝 Detailed log: {DETAILED_LOG_FILE}")
        print(f"📝 Error log: {ERROR_LOG_FILE}")
    except Exception as e:
        print(f"⚠️ Could not create log files: {e}")

    access_token = get_access_token()
    configure_shopify_cost_api(access_token)
    variants, product_variant_count, sku_to_row, price_col, cost_price_col, avail_col, avail1_col = (
        load_uploaded_data()
    )
    if not variants:
        return

    cfg_path = resolve_brand_config_path(brand_config_path)
    brand_config = load_brand_config(cfg_path)
    discovered = sorted({str(v.get("brand") or "").strip() for v in variants if str(v.get("brand") or "").strip()})
    if discovered:
        brand_config = merge_discovered_brands(brand_config, discovered, default_for_new=False)
        save_brand_config(brand_config, cfg_path)
    price_on, price_off = apply_update_prices_by_brand(variants, brand_config)
    enabled_names = enabled_brand_display_names(brand_config)
    print(
        f"🏷️ Brand price toggles ({os.path.basename(cfg_path)}): "
        f"{price_on} rows update cost+retail | {price_off} rows availability only"
    )
    if enabled_names:
        print(f"   Prices ON for: {', '.join(enabled_names)}")
    else:
        print("   Prices ON for: (none — availability only for all branded rows)")
    print(f"[BRAND_PRICE] price_updates=ON:{price_on} availability_only={price_off}")

    write_spreadsheet = safe_gs_call(client.open_by_key, UPLOADED_SPREADSHEET_ID)
    write_sheet = safe_gs_call(write_spreadsheet.worksheet, SHEET_NAME_PRODUCTS)

    headers = safe_gs_call(write_sheet.row_values, 1)
    headers, cost_price_col = ensure_cost_price_column(write_sheet, headers)
    if cost_price_col == -1:
        cost_price_col = len(headers) - 1
    print(f"📋 Sheet column «{COST_PRICE_COLUMN}» at index {cost_price_col}")

    variants = sorted(
        variants,
        key=lambda v: int(v.get("row_idx") or sku_to_row.get(v.get("sku", ""), 999999)),
    )

    total = len(variants)
    worker_chunks = split_variants_across_workers(variants, workers)
    print(f"[PROGRESS] phase=start total={total} workers={workers}", flush=True)
    print(f"🔄 Processing {total} rows, workers={workers}", flush=True)
    if workers == 1:
        print("   Mode: one Chrome, login once, all rows in sheet order.")
    else:
        print(
            f"   Mode: {len(worker_chunks)} Chrome instance(s) — each stays open for "
            f"its full chunk (~{max(len(c) for c in worker_chunks)} rows max per worker)."
        )
    cost_dest = (
        "sheet «cost price» + Shopify Cost"
        if shopify_cost_api_enabled()
        else "sheet «cost price» only (Shopify Cost needs write_inventory scope)"
    )
    print(
        f"   Per row: (1) distribution → cost (raw+15) + retail (raw×1.21+15) when brand price ON; "
        f"(2) motorsport → availability for all; retail fallback (+15 only) when price ON."
    )
    print(
        "   Borderline distribution SKUs: "
        "SVWS059→SVWS059C, SVWS059_1→SVWS059, SVWS052C_1→SVW052C, SVW065_1→SVW066\n"
    )

    all_results: List[Dict] = []
    common_args = (
        access_token,
        product_variant_count,
        sku_to_row,
        price_col,
        cost_price_col,
        avail_col,
        avail1_col,
        write_sheet,
    )

    if len(worker_chunks) == 1:
        chunk = worker_chunks[0]
        first_row = int(chunk[0].get("row_idx") or 0)
        last_row = int(chunk[-1].get("row_idx") or 0)
        print(
            f"\n{'=' * 60}\n"
            f"WORKER 1: sheet rows {first_row}–{last_row} ({len(chunk)} rows)\n"
            f"{'=' * 60}"
        )
        all_results.extend(
            process_variants_chunk(chunk, *common_args, worker_id=1)
        )
    else:
        with ThreadPoolExecutor(max_workers=len(worker_chunks)) as executor:
            futures = {}
            for slot, chunk in enumerate(worker_chunks, start=1):
                first_row = int(chunk[0].get("row_idx") or 0)
                last_row = int(chunk[-1].get("row_idx") or 0)
                print(
                    f"\n{'=' * 60}\n"
                    f"WORKER {slot}: {len(chunk)} rows "
                    f"(sheet rows {first_row}–{last_row}, interleaved)\n"
                    f"{'=' * 60}"
                )
                futures[
                    executor.submit(
                        process_variants_chunk,
                        chunk,
                        *common_args,
                        slot,
                    )
                ] = slot
            for future in as_completed(futures):
                slot = futures[future]
                try:
                    all_results.extend(future.result())
                except Exception as e:
                    print(f"❌ Worker {slot} failed: {e}")
                    log_detailed(f"Worker {slot} exception: {e}")

    updated = unchanged = failed = skipped = 0
    for result in all_results:
        if result.get('error') == "No data found":
            skipped += 1
        elif (
            result.get('cost_applied')
            or result.get('cost_changed')
            or result.get('price_changed')
            or result.get('avail_changed')
            or result.get('avail1_changed')
        ):
            if result.get('shopify_ok'):
                updated += 1
            else:
                failed += 1
        else:
            unchanged += 1

    print("\n" + "=" * 70)
    print("📊 UPDATE SUMMARY")
    print(f"✅ Updated (Shopify + sheet): {updated}")
    print(f"🔄 Already up‑to‑date: {unchanged}")
    print(f"❌ Failed Shopify updates: {failed}")
    print(f"⏭️  Skipped (data not found): {skipped}")
    print(f"📄 Check {DETAILED_LOG_FILE} for detailed logs and {ERROR_LOG_FILE} for errors")
    print("=" * 70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update upload sheet + Shopify row-by-row.")
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=(
            "Chrome instances for the full run (default 6). Each logs in once and "
            "processes its rows before closing. Use 1 for one browser, all rows."
        ),
    )
    parser.add_argument(
        "--brand-config",
        default=None,
        help=(
            "Optional override for brand price JSON (dashboard saves "
            "brand_price_update_config.json automatically)."
        ),
    )
    args = parser.parse_args()
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"❌ Service account file not found: {SERVICE_ACCOUNT_FILE}")
        exit(1)
    brand_cfg = resolve_brand_config_path(args.brand_config)
    print(f"🏷️ Brand config: {brand_cfg}")
    print("[CYCLE START]", flush=True)
    update_all(workers=args.workers, brand_config_path=brand_cfg)
    print("[CYCLE END]", flush=True)