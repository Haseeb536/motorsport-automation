#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brand toggles for update_prices_and_sheet.py and the Streamlit dashboard.

Checked brands → cost + retail updated on distribution/motorsport.
Unchecked brands → availability only (still scraped on motorsport).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRAND_CONFIG_PATH = os.path.join(SCRIPT_DIR, "brand_price_update_config.json")
BRAND_CONFIG_ENV_VAR = "MOTORSPORT_BRAND_CONFIG_PATH"


def resolve_brand_config_path(explicit: Optional[str] = None) -> str:
    """
    Path to brand price JSON (dashboard + update_prices_and_sheet.py).

    Priority: explicit CLI/path argument → env MOTORSPORT_BRAND_CONFIG_PATH →
    ``brand_price_update_config.json`` next to this module.
    """
    if explicit and str(explicit).strip():
        return os.path.abspath(str(explicit).strip())
    env = os.environ.get(BRAND_CONFIG_ENV_VAR, "").strip()
    if env:
        return os.path.abspath(env)
    return DEFAULT_BRAND_CONFIG_PATH

# Upload sheet brand columns (first match wins)
BRAND_COLUMN_NAMES = (
    "brand1",
    "brand",
    "vendor",
    "merk",
    "brand 1",
)

UPLOAD_SPREADSHEET_ID = os.environ.get("GOOGLE_UPLOADED_SPREADSHEET_ID", "")
SHEET_NAME_PRODUCTS = "products"


def normalize_brand_key(name: str) -> str:
    return str(name or "").strip().casefold()


def find_brand_column_index(headers: List[str]) -> int:
    for i, h in enumerate(headers):
        h_low = str(h or "").strip().lower()
        if h_low in BRAND_COLUMN_NAMES:
            return i
    return -1


def discover_brands_from_headers_rows(
    headers: List[str], data_rows: List[List[str]]
) -> List[str]:
    """Unique non-empty brand values from sheet rows (sorted, original casing kept)."""
    idx = find_brand_column_index(headers)
    if idx < 0:
        return []
    seen: Dict[str, str] = {}
    for row in data_rows:
        if idx >= len(row):
            continue
        raw = str(row[idx] or "").strip()
        if not raw:
            continue
        key = normalize_brand_key(raw)
        if key not in seen:
            seen[key] = raw
    return sorted(seen.values(), key=lambda s: s.lower())


def load_brand_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or DEFAULT_BRAND_CONFIG_PATH
    if not os.path.isfile(path):
        return {"version": 1, "brands": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("brands", {})
            return data
    except Exception:
        pass
    return {"version": 1, "brands": {}}


def save_brand_config(
    config: Dict[str, Any],
    path: Optional[str] = None,
) -> str:
    path = path or DEFAULT_BRAND_CONFIG_PATH
    config = dict(config)
    config.setdefault("version", 1)
    config.setdefault("brands", {})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return path


def merge_discovered_brands(
    config: Dict[str, Any],
    discovered: List[str],
    *,
    default_for_new: bool = True,
) -> Dict[str, Any]:
    """Add sheet brands missing from config; keep existing on/off flags."""
    brands = dict(config.get("brands") or {})
    for name in discovered:
        key = normalize_brand_key(name)
        if not key:
            continue
        if key not in brands:
            brands[key] = {
                "display_name": name,
                "update_prices": default_for_new,
            }
        else:
            entry = brands[key]
            if not entry.get("display_name"):
                entry["display_name"] = name
    config["brands"] = brands
    return config


def brand_entry_display_name(entry: Dict[str, Any], key: str) -> str:
    return str(entry.get("display_name") or key or "").strip()


def is_brand_price_update_enabled(brand: str, config: Dict[str, Any]) -> bool:
    """
    If config file has no brand entries, default True (backward compatible).
    If brand column empty, default True (do not block rows without brand).
    """
    brands = config.get("brands") or {}
    if not brands:
        return True
    key = normalize_brand_key(brand)
    if not key:
        return True
    entry = brands.get(key)
    if entry is None:
        return bool(config.get("default_update_prices", False))
    return bool(entry.get("update_prices", False))


def apply_update_prices_by_brand(
    variants: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[int, int]:
    """Set ``update_prices`` on each variant. Returns (price_on_count, price_off_count)."""
    on = off = 0
    for var in variants:
        brand = str(var.get("brand") or "").strip()
        enabled = is_brand_price_update_enabled(brand, config)
        var["update_prices"] = enabled
        if enabled:
            on += 1
        else:
            off += 1
    return on, off


def enabled_brand_display_names(config: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key, entry in sorted((config.get("brands") or {}).items()):
        if entry.get("update_prices"):
            out.append(brand_entry_display_name(entry, key))
    return out


def discover_brands_from_upload_sheet(
    *,
    credentials_path: Optional[str] = None,
    spreadsheet_id: str = UPLOAD_SPREADSHEET_ID,
) -> List[str]:
    """Read upload sheet and return sorted unique brand names."""
    cred_path = credentials_path or os.path.join(SCRIPT_DIR, "credentials.json")
    try:
        from google.oauth2.service_account import Credentials
        import gspread

        creds = Credentials.from_service_account_file(
            cred_path,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(spreadsheet_id).worksheet(SHEET_NAME_PRODUCTS)
        all_data = sheet.get_all_values()
        if len(all_data) < 2:
            return []
        headers = [str(h or "").strip() for h in all_data[0]]
        return discover_brands_from_headers_rows(headers, all_data[1:])
    except Exception:
        return []


def build_config_from_checkbox_state(
    discovered: List[str],
    checked: Set[str],
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """checked = set of display names (or keys) with checkbox on."""
    config = dict(existing or load_brand_config())
    checked_keys = {normalize_brand_key(c) for c in checked}
    brands: Dict[str, Any] = {}
    for name in discovered:
        key = normalize_brand_key(name)
        if not key:
            continue
        brands[key] = {
            "display_name": name,
            "update_prices": key in checked_keys
            or normalize_brand_key(name) in checked_keys,
        }
    config["brands"] = brands
    return config
