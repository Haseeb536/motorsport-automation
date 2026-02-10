#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test CSF brand only: scrape distribution (cost + retail) and motorsport (availability
+ retail fallback) for every CSF row in the upload sheet — same logic as
update_prices_and_sheet.py (including distribution search pagination when the SKU
is not on page 1).

By default DRY-RUN (preview only — sheet and Shopify are NOT updated).

To update the Google upload sheet AND Shopify for all CSF rows:
  python test_csf_pricing.py --apply

One SKU only (preview):
  python test_csf_pricing.py --sku "#8179"

One SKU + write sheet + Shopify:
  python test_csf_pricing.py --apply --sku "#8179"

Motorsport fallback price uses ``#old_price_display`` first, then ``#our_price_display``.

Retail formula: distribution **retail list** raw (e.g. 339,00) × 1.21 + 15 → charm .99 (e.g. 424,99).
Motorsport fallback: scraped PDP price + 15 only (no ×1.21).

Other options:
  python test_csf_pricing.py --limit 5
  python test_csf_pricing.py --apply --output csf_results.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

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
from brand_price_config import normalize_brand_key
from distribution_cost_scrape import (
    DISTRIBUTION_EMAIL,
    DISTRIBUTION_PASSWORD,
    login_distribution_site,
    scrape_distribution_cost_and_retail,
)
from sku_price_overrides import (
    apply_vwr_pack_multiplier_to_raw,
    distribution_scrape_plans,
    vwr_ignition_coil_pack_multiplier,
)
from motorsport_pricing import calculation_steps, motorsport_fallback_steps, parse_scraped_price
from motorsport_site_scrape import english_pdp_label_from_upload_value
DEFAULT_BRAND = "CSF"


def _load_brand_variants(brand: str) -> List[Dict[str, Any]]:
    variants, *_ = updater.load_uploaded_data()
    key = normalize_brand_key(brand)
    out: List[Dict[str, Any]] = []
    for var in variants:
        if normalize_brand_key(str(var.get("brand") or "")) != key:
            continue
        row = dict(var)
        row["update_prices"] = True
        out.append(row)
    out.sort(key=lambda v: int(v.get("row_idx") or 0))
    return out


def _price_float(s: str) -> Optional[float]:
    return parse_scraped_price(s)


def _fmt_diff(old_s: str, new_s: Optional[str]) -> str:
    if not new_s:
        return "(no change)"
    o = _price_float(old_s)
    n = _price_float(new_s)
    if o is not None and n is not None:
        d = n - o
        if abs(d) < 0.005:
            return f"{new_s} (same)"
        return f"{old_s or '(empty)'} → {new_s} ({d:+.2f})"
    return f"{old_s or '(empty)'} → {new_s}"


def _process_one(
    driver,
    dist_tab: str,
    motor_tab: str,
    dist_login_ok: bool,
    var: Dict[str, Any],
    seq: int,
    total: int,
) -> Dict[str, Any]:
    sku = var["sku"]
    row_num = int(var.get("row_idx") or 0)
    src_pid = var.get("source_product_id") or ""
    product_url = var.get("product_url") or ""
    attributes = var.get("attributes") or {}
    old_price = var.get("current_price") or ""
    old_cost = var.get("current_cost_price") or ""
    old_avail = var.get("current_availability") or ""
    old_avail1 = var.get("current_availability_1") or ""

    print(f"\n{'—' * 70}")
    print(
        f"[{seq}/{total}] row {row_num} | {sku} | product_id={src_pid} | brand={var.get('brand')}"
    )
    print(
        f"  Sheet now → price={old_price or '(empty)'}  cost={old_cost or '(empty)'}  "
        f"avail={old_avail or '(empty)'}  avail_1={old_avail1 or '(empty)'}"
    )
    if attributes:
        for k, v in sorted(attributes.items()):
            if not str(v).strip():
                continue
            en = english_pdp_label_from_upload_value(k, v)
            print(
                f"  att {k}={v}"
                + (f" → PDP '{en}'" if en else " (⚠ missing in att_value_translation_lookup.json)")
            )
    print(
        f"  distribution plans={distribution_scrape_plans(sku, attributes)}"
    )

    record: Dict[str, Any] = {
        "sku": sku,
        "row_idx": row_num,
        "product_id": src_pid,
        "sheet_price": old_price,
        "sheet_cost": old_cost,
        "sheet_availability": old_avail,
        "sheet_availability_1": old_avail1,
        "dist_status": "",
        "motor_status": "",
        "cost_raw": "",
        "retail_raw": "",
        "motor_retail_raw": "",
        "retail_source": "",
        "new_cost": "",
        "new_price": "",
        "new_availability": "",
        "new_availability_1": "",
    }

    new_cost_price = None
    dist_retail_raw = None
    dist_status = ""

    if dist_login_ok:
        driver.switch_to.window(dist_tab)
        raw_cost, raw_retail, dist_status = scrape_distribution_cost_and_retail(
            driver, sku, attributes=attributes, log_fn=lambda s, m: print(f"  [dist] {m}"),
        )
        record["dist_status"] = dist_status
        pack_mult = vwr_ignition_coil_pack_multiplier(sku)
        if pack_mult > 1:
            print(
                f"  VWR ignition pack ×{pack_mult} "
                f"(per-coil raw → pack total before +15 / ×1.21+15)"
            )
            if raw_cost:
                raw_cost = apply_vwr_pack_multiplier_to_raw(sku, raw_cost)
            if raw_retail:
                raw_retail = apply_vwr_pack_multiplier_to_raw(sku, raw_retail)
        record["cost_raw"] = raw_cost or ""
        record["retail_raw"] = raw_retail or ""
        print(
            f"  distribution: {dist_status}  cost_raw={raw_cost or '(none)'}  "
            f"retail_raw={raw_retail or '(none)'}"
        )
        if raw_cost and not raw_retail:
            print("  note: distribution cost only — retail must come from retail xpath or motorsport")
        if raw_cost:
            new_cost_price = updater.calculate_cost_price(sku, raw_cost)
            print(f"  new cost (+15): {new_cost_price or 'calc failed'}")
            record["new_cost"] = new_cost_price or ""
        if raw_retail:
            steps = calculation_steps(raw_retail)
            if steps:
                scraped, markup, before, final = steps
                print(
                    f"  retail formula: {scraped:.2f} ×1.21={markup:.2f} "
                    f"+15={before:.2f} → {final}"
                )
            dist_retail_raw = raw_retail
    else:
        record["dist_status"] = "distribution_login_failed"
        print("  distribution: skipped (login failed)")

    driver.switch_to.window(motor_tab)
    motorsport_retail_raw = None
    motor_price, avail_raw, motor_status = updater.scrape_motorsport_for_upload_row(
        driver,
        1,
        sku,
        src_pid,
        product_url or "",
        attributes,
    )
    record["motor_status"] = motor_status
    print(f"  motorsport: {motor_status}  price_raw={motor_price or '(none)'}  avail={avail_raw or '(none)'}")
    motorsport_ok = updater._motorsport_ok_status(motor_status)
    if motor_price:
        motorsport_retail_raw = apply_vwr_pack_multiplier_to_raw(sku, motor_price)
        record["motor_retail_raw"] = motorsport_retail_raw or motor_price

    retail_raw = dist_retail_raw
    retail_source = "distribution"
    if not retail_raw and motorsport_retail_raw and motorsport_ok:
        retail_raw = motorsport_retail_raw
        retail_source = "motorsport"
        steps = motorsport_fallback_steps(motorsport_retail_raw)
        if steps:
            scraped, after_add, final = steps
            print(
                f"  distribution unavailable ({dist_status or 'no retail'}) — "
                f"motorsport raw={scraped:.2f} +15 → {final}"
            )
        else:
            print(
                f"  distribution unavailable — motorsport raw={motorsport_retail_raw} +15"
            )
    record["retail_source"] = retail_source

    if (
        motorsport_retail_raw
        and motorsport_ok
        and retail_source == "distribution"
    ):
        asm_steps = motorsport_fallback_steps(motorsport_retail_raw)
        if asm_steps:
            asm_raw, asm_after, asm_final = asm_steps
            print(
                f"  ASM PDP (not used — distribution has retail): "
                f"raw={asm_raw:.2f} +15 → {asm_final} "
                f"(would apply if no dist retail)"
            )

    new_price = None
    if retail_raw:
        new_price = updater.calculate_retail_price(
            sku, retail_raw, retail_source=retail_source
        )
        src_note = (
            "distribution ×1.21+15"
            if retail_source == "distribution"
            else "ASM fallback +15"
        )
        print(f"  new shop price ({src_note}): {new_price or 'calc failed'}")
        record["new_price"] = new_price or ""

    new_avail = new_avail1 = None
    if avail_raw:
        new_avail = updater.clean_availability(avail_raw)
        new_avail1 = updater.clean_availability_1(avail_raw)
        print(f"  new availability: {new_avail} / {new_avail1}")
        record["new_availability"] = new_avail
        record["new_availability_1"] = new_avail1

    if new_price or new_cost_price:
        print(
            f"  vs sheet → price {_fmt_diff(old_price, new_price)}  "
            f"cost {_fmt_diff(old_cost, new_cost_price)}"
        )
    if new_avail:
        if str(new_avail) != str(old_avail):
            print(f"  vs sheet → avail {old_avail or '(empty)'} → {new_avail}")
        else:
            print(f"  vs sheet → avail unchanged ({new_avail})")

    record["_new_cost_price"] = new_cost_price
    record["_new_price"] = new_price
    record["_new_avail"] = new_avail
    record["_new_avail1"] = new_avail1
    return record


def _write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    fields = [
        "sku",
        "row_idx",
        "product_id",
        "dist_status",
        "motor_status",
        "cost_raw",
        "retail_raw",
        "retail_source",
        "motor_retail_raw",
        "new_cost",
        "new_price",
        "new_availability",
        "new_availability_1",
        "sheet_price",
        "sheet_cost",
        "sheet_availability",
        "sheet_availability_1",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"\n📄 Wrote {len(rows)} row(s) to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CSF-only price + availability test (same scrape logic as main updater).",
    )
    parser.add_argument(
        "--brand",
        default=DEFAULT_BRAND,
        help=f"Brand name filter (default: {DEFAULT_BRAND})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write results to Google Sheet + Shopify (force_write — updates even if values match)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only first N CSF rows (0 = all)",
    )
    parser.add_argument(
        "--sku",
        action="append",
        default=[],
        help="Only these References (e.g. --sku #8179), repeatable",
    )
    parser.add_argument(
        "--output",
        default="",
        help="CSV path for scrape summary (default: csf_pricing_test_YYYYMMDD_HHMMSS.csv)",
    )
    args = parser.parse_args()
    do_apply = args.apply

    print("=" * 70)
    print(f"CSF brand test — {args.brand} (upload sheet)")
    print("=" * 70)
    if do_apply:
        print("MODE: APPLY — writes Google Sheet + Shopify for every CSF row (force_write=True)")
    else:
        print("MODE: DRY-RUN — Shopify and sheet are NOT changed (add --apply to update store)")
    print(
        "Scrape: distribution cost+retail (paginated search), "
        "motorsport (#old_price_display then fallbacks), availability + legacy SKU search"
    )
    print("Retail: distribution raw × 1.21 + 15  |  Motorsport fallback: raw + 15  |  Cost: raw + 15")
    print()

    variants = _load_brand_variants(args.brand)
    if not variants:
        print(f"No rows with brand «{args.brand}» in upload sheet.")
        return 1

    if args.sku:
        want = {s.strip().upper() for s in args.sku}
        variants = [v for v in variants if str(v.get("sku") or "").strip().upper() in want]
    if args.limit > 0:
        variants = variants[: args.limit]

    print(f"Found {len(variants)} {args.brand} variant(s) to process.\n")

    access_token = updater.get_access_token()
    updater.configure_shopify_cost_api(access_token)

    write_sheet = None
    product_variant_count: Dict[str, int] = {}
    sku_to_row: Dict[str, int] = {}
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
        _, product_variant_count, sku_to_row, price_col, cost_price_col, avail_col, avail1_col = (
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
        print("Distribution login: skipped (no credentials in distribution_cost_scrape.py)\n")

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
                sku = var["sku"]
                print("  Applying to sheet + Shopify…")
                apply_result = updater._apply_row_updates_immediately(
                    original_sku=sku,
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
                rec.get("_new_price") or rec.get("_new_avail")
            ):
                print(
                    "  (DRY-RUN — Shopify unchanged; run with --apply to push price/availability)"
                )
            results.append(rec)
    finally:
        driver.quit()

    out_path = args.output.strip()
    if not out_path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(SCRIPT_DIR, f"csf_pricing_test_{stamp}.csv")
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
    print("\n" + "=" * 70)
    print(f"Finished {len(results)} {args.brand} SKU(s).")
    print(f"  Rows with scraped retail ≠ sheet price (approx): {price_changes}")
    if do_apply:
        print("  Sheet + Shopify updated where values changed.")
    else:
        print("  Re-run with --apply to push changes to sheet + Shopify.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
