#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forge brand pricing test — all Forge rows on the upload sheet.

Scrape/apply logic matches test_csf_pricing.py and update_prices_and_sheet.py:
  • Distribution cost + retail (paginated search, SKU overrides)
  • FMKC022 / FMBH18T: With Clamps → ``-HC`` on distribution; ``_1`` or Zonder klemmen → no ``-HC``
  • Motorsport availability + retail fallback (raw + 15)
  • Bypasses brand_price_update_config (update_prices=True on every row)

By default DRY-RUN (sheet and Shopify are NOT updated).

Preview all Forge rows:
  python test_forge_pricing.py

Only rows missing «cost price»:
  python test_forge_pricing.py --missing-cost

Apply to Google Sheet + Shopify:
  python test_forge_pricing.py --apply

One SKU (# optional; ``_1`` aliases resolve to Zonder klemmen rows):
  python test_forge_pricing.py --sku FMKC022-BLA_1
  python test_forge_pricing.py --apply --sku "#FMBH18T-RED"

Limit (smoke test):
  python test_forge_pricing.py --limit 5

Retail: distribution raw × 1.21 + 15  |  Motorsport fallback: raw + 15  |  Cost: raw + 15
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import Driver

import update_prices_and_sheet as updater
from distribution_cost_scrape import (
    DISTRIBUTION_EMAIL,
    DISTRIBUTION_PASSWORD,
    login_distribution_site,
)
from sku_price_overrides import row_matches_sheet_sku_alias
from test_csf_pricing import (
    _load_brand_variants,
    _price_float,
    _process_one,
    _write_csv,
)

DEFAULT_BRAND = "Forge"


def _missing_cost(var: Dict[str, Any]) -> bool:
    return not str(var.get("current_cost_price") or "").strip()


def _filter_by_sku(variants: List[Dict[str, Any]], sku_args: List[str]) -> List[Dict[str, Any]]:
    """Match Reference with or without ``#``; ``_1`` → base + Zonder klemmen via aliases."""
    out: List[Dict[str, Any]] = []
    for var in variants:
        if any(row_matches_sheet_sku_alias(var, w) for w in sku_args):
            out.append(var)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Forge brand pricing test — scrape and optionally update all Forge rows.",
    )
    parser.add_argument(
        "--brand",
        default=DEFAULT_BRAND,
        help=f"Brand filter (default: {DEFAULT_BRAND})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write cost, price, and availability to Google Sheet + Shopify (force_write)",
    )
    parser.add_argument(
        "--missing-cost",
        action="store_true",
        help="Only rows where «cost price» is empty on the sheet",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only first N rows after filters (0 = all)",
    )
    parser.add_argument(
        "--sku",
        action="append",
        default=[],
        help="Only these References (# optional; repeatable)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="CSV summary (default: forge_pricing_test_YYYYMMDD_HHMMSS.csv)",
    )
    args = parser.parse_args()
    do_apply = args.apply

    print("=" * 70)
    print(f"Forge brand pricing test — {args.brand} (upload sheet)")
    print("=" * 70)
    if do_apply:
        print("MODE: APPLY — writes Google Sheet + Shopify (force_write=True)")
    else:
        print("MODE: DRY-RUN — add --apply to update sheet and store")
    print(
        "Scrape: distribution cost+retail (paginated), motorsport availability + "
        "retail fallback; FMKC022/FMBH18T -HC vs Zonder klemmen/_1"
    )
    print("Retail: distribution raw × 1.21 + 15  |  Motorsport fallback: raw + 15  |  Cost: raw + 15")
    print()

    all_brand = _load_brand_variants(args.brand)
    if not all_brand:
        print(f"No rows with brand «{args.brand}» in upload sheet.")
        return 1

    missing_cost_n = sum(1 for v in all_brand if _missing_cost(v))
    print(f"Sheet: {len(all_brand)} {args.brand} row(s), {missing_cost_n} with empty cost price.")

    variants = list(all_brand)
    if args.sku:
        variants = _filter_by_sku(variants, args.sku)
        print(f"Filter: --sku → {len(variants)} row(s)")
        if not variants:
            print("No rows matched --sku (try with/without #, or _1 alias).")
            return 1
    elif args.missing_cost:
        variants = [v for v in variants if _missing_cost(v)]
        print(f"Filter: --missing-cost → {len(variants)} row(s)")
    if args.limit > 0:
        variants = variants[: args.limit]

    if not variants:
        print("No rows to process.")
        return 1

    print(f"\nProcessing {len(variants)} row(s).\n")

    access_token = updater.get_access_token()
    updater.configure_shopify_cost_api(access_token)

    if do_apply:
        shop_ok, shop_msg = updater.verify_shopify_api(access_token)
        if shop_ok:
            print(shop_msg)
        else:
            print(f"⚠ {shop_msg}")
            print("   Sheet will still update; Shopify may fail until store/token are fixed.\n")

    write_sheet = None
    product_variant_count: Dict[str, int] = {}
    price_col = cost_price_col = avail_col = avail1_col = -1

    if do_apply:
        write_spreadsheet = updater.safe_gs_call(
            updater.client.open_by_key, updater.UPLOADED_SPREADSHEET_ID
        )
        write_sheet = updater.safe_gs_call(
            write_spreadsheet.worksheet, updater.SHEET_NAME_PRODUCTS
        )
        headers = updater.safe_gs_call(write_sheet.row_values, 1)
        headers, cost_price_col = updater.ensure_cost_price_column(write_sheet, headers)
        if cost_price_col == -1:
            cost_price_col = len(headers) - 1
        _, product_variant_count, _, price_col, cost_price_col, avail_col, avail1_col = (
            updater.load_uploaded_data()
        )

    driver = Driver(headless=False)
    driver.maximize_window()

    driver.execute_script("window.open('');")
    tabs = driver.window_handles
    dist_tab = tabs[0]
    motor_tab = tabs[1]

    dist_login_ok = False
    driver.switch_to.window(dist_tab)
    if DISTRIBUTION_EMAIL and DISTRIBUTION_PASSWORD:
        print("Logging into All-Stars Distribution…")
        dist_login_ok = login_distribution_site(
            driver, DISTRIBUTION_EMAIL, DISTRIBUTION_PASSWORD
        )
        print(f"Distribution login: {'OK' if dist_login_ok else 'FAILED'}\n")
    else:
        print("Distribution login: skipped (no credentials)\n")

    driver.switch_to.window(motor_tab)
    driver.get(updater.MOTORSPORT_SITE_BASE)
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="search_query_top"]'))
        )
    except Exception:
        pass
    print("Motorsport site ready.\n")

    results: List[Dict[str, Any]] = []
    total = len(variants)
    try:
        for seq, var in enumerate(variants, start=1):
            rec = _process_one(
                driver, dist_tab, motor_tab, dist_login_ok, var, seq, total
            )
            if do_apply and write_sheet is not None:
                print("  Applying to sheet + Shopify…")
                apply_result = updater._apply_row_updates_immediately(
                    original_sku=var["sku"],
                    row_num=int(var["row_idx"]),
                    vid=var["shopify_variant_id"],
                    pid=var["shopify_product_id"],
                    src_pid=var["source_product_id"],
                    access_token=access_token,
                    product_variant_count=product_variant_count,
                    write_sheet=write_sheet,
                    price_col=price_col,
                    cost_price_col=cost_price_col,
                    avail_col=avail_col,
                    avail1_col=avail1_col,
                    old_price=var.get("current_price") or "",
                    old_cost=var.get("current_cost_price") or "",
                    old_avail=var.get("current_availability") or "",
                    old_avail1=var.get("current_availability_1") or "",
                    new_cost_price=rec.get("_new_cost_price"),
                    new_price=rec.get("_new_price"),
                    new_avail=rec.get("_new_avail"),
                    new_avail1=rec.get("_new_avail1"),
                    worker_id=1,
                    force_write=True,
                )
                ok = apply_result.get("shopify_ok", False)
                print(
                    f"  APPLY → Shopify={'OK' if ok else 'FAILED'}  "
                    f"price→{rec.get('_new_price') or '(unchanged)'}  "
                    f"cost→{rec.get('_new_cost_price') or '(unchanged)'}  "
                    f"avail→{rec.get('_new_avail') or '(unchanged)'}"
                )
            elif not do_apply and (
                rec.get("_new_price") or rec.get("_new_cost_price") or rec.get("_new_avail")
            ):
                print("  (DRY-RUN — run with --apply to push price/cost/availability)")
            results.append(rec)
    finally:
        driver.quit()

    out_path = args.output.strip()
    if not out_path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(SCRIPT_DIR, f"forge_pricing_test_{stamp}.csv")
    _write_csv(out_path, results)

    price_changes = sum(
        1
        for r in results
        if r.get("new_price")
        and _price_float(str(r.get("sheet_price") or "")) is not None
        and _price_float(str(r.get("new_price") or "")) is not None
        and abs(
            _price_float(str(r.get("sheet_price") or ""))  # type: ignore
            - _price_float(str(r.get("new_price") or ""))  # type: ignore
        )
        >= 0.005
    )
    cost_filled = sum(
        1
        for r in results
        if r.get("new_cost")
        and not str(r.get("sheet_cost") or "").strip()
    )
    no_dist_cost = sum(1 for r in results if not r.get("cost_raw"))

    print("\n" + "=" * 70)
    print(f"Finished {len(results)} {args.brand} SKU(s).")
    print(f"  Rows with scraped price ≠ sheet price (approx): {price_changes}")
    print(f"  Rows where cost was empty and scrape found cost: {cost_filled}")
    if no_dist_cost:
        print(f"  Rows with no distribution cost found: {no_dist_cost}")
    if do_apply:
        print("  Sheet updated where scraped; Shopify depends on API.")
    else:
        print("  Re-run with: python test_forge_pricing.py --apply")
        print("  Missing cost only: python test_forge_pricing.py --apply --missing-cost")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
