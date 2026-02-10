#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrape prices (and optional availability) from all-stars-motorsport.com by matching
product_id, SKU/Reference, and sheet att_* attribute values.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SITE_BASE = "https://www.all-stars-motorsport.com/en/"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ATT_JSON = os.path.join(SCRIPT_DIR, "att_value_translation_lookup.json")

_att_translation_bundle: Optional[Dict[str, Any]] = None


def _normalize_lookup_key(raw: str) -> str:
    """Lowercase + collapse whitespace so upload sheet text matches JSON lookup_key."""
    s = str(raw or "").strip().lower()
    return re.sub(r"\s+", " ", s)


def _load_att_translation_bundle() -> Dict[str, Any]:
    """
    Loads att_value_translation_lookup.json (built by build_att_translation_json.py).

    Upload sheet att_* cells are Dutch/translated; motorsport.com PDP options are English.
    by_column / global / entries map translated text → english_exact_for_website_pdp.
    """
    global _att_translation_bundle
    if _att_translation_bundle is not None:
        return _att_translation_bundle
    path = os.environ.get("ATT_TRANSLATION_JSON", _DEFAULT_ATT_JSON)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                _att_translation_bundle = json.load(f)
        else:
            _att_translation_bundle = {}
    except Exception:
        _att_translation_bundle = {}
    if not isinstance(_att_translation_bundle, dict):
        _att_translation_bundle = {}
    _att_translation_bundle.setdefault("by_column", {})
    _att_translation_bundle.setdefault("global", {})
    _att_translation_bundle.setdefault("entries", [])
    # Fast path: entries[] audit list → (att_column, lookup_key) → English PDP phrase
    entries_by_col: Dict[str, Dict[str, str]] = {}
    for entry in _att_translation_bundle.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        col = str(entry.get("att_column") or "").strip().lower()
        lk = str(entry.get("lookup_key") or "").strip().lower()
        en = str(entry.get("english_exact_for_website_pdp") or "").strip()
        if col and lk and en and lk not in entries_by_col.setdefault(col, {}):
            entries_by_col[col][lk] = en
    _att_translation_bundle["_entries_by_column"] = entries_by_col
    return _att_translation_bundle


def english_pdp_label_from_upload_value(att_key_lower: str, raw: str) -> Optional[str]:
    """
    Resolve one upload-sheet att_* cell to the English phrase used on the motorsport PDP.
    Returns None when the value is not in att_value_translation_lookup.json.
    """
    raw = str(raw or "").strip()
    if not raw:
        return None
    lk = _normalize_lookup_key(raw)
    att_key_lower = (att_key_lower or "").strip().lower()
    data = _load_att_translation_bundle()

    by_col = data.get("by_column") or {}
    if att_key_lower and isinstance(by_col.get(att_key_lower), dict):
        eng = by_col[att_key_lower].get(lk)
        if eng:
            return eng

    entries_by_col = data.get("_entries_by_column") or {}
    if att_key_lower and isinstance(entries_by_col.get(att_key_lower), dict):
        eng = entries_by_col[att_key_lower].get(lk)
        if eng:
            return eng

    gl = data.get("global") or {}
    if isinstance(gl, dict):
        eng2 = gl.get(lk)
        if eng2 and eng2 != "__ambiguous__":
            return eng2

    return None


def match_targets_for_sheet_value(att_key_lower: str, raw: str) -> List[str]:
    """
    Values to match against English PDP <option> labels.

    Upload sheet cells are translated (Dutch); motorsport.com is English. We always try
    the verbatim sheet cell first, then english_exact_for_website_pdp from
    att_value_translation_lookup.json (by_column, entries, then global).
    """
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
    eng = english_pdp_label_from_upload_value(att_key_lower, raw)
    if eng:
        add(eng)

    return out


MOTORSPORT_OLD_PRICE_XPATH = '//*[@id="old_price_display"]'

PRICE_SELECTORS = [
    '//*[@id="our_price_display"]',
    '//span[@itemprop="price"]',
    '//span[@class="price"]',
    '//div[contains(@class,"content_prices")]//span[contains(@class,"price")]',
]

AVAILABILITY_XPATHS = [
    '//*[@id="center_column"]/motion/div[1]/div/div/div[2]/motion[2]/div[2]/div[1]/p',
    '//*[@id="center_column"]/motion/div[1]/div/div/div[2]/div[2]/motion[2]/motion[2]/div[2]/div[1]/p',
    '//*[@id="center_column"]/div/div[1]/motion/div/div/div[2]/motion[2]/div[2]/div[1]/p',
    '//*[@id="center_column"]/motion/div[1]/div/div/div[2]/div[2]/div[2]/div[1]/p',
    '//p[contains(@class, "availability")]',
    '//span[@id="availability_value"]',
    '//motion//p[contains(@class, "availability")]',
]


def normalize_ref(value: Any) -> str:
    s = str(value or "").strip()
    while s.startswith("#"):
        s = s[1:]
    return s.strip()


# Trailing variant index only (end of string): _1, -2, -12 — not ``-``/``_`` in the middle.
_TRAILING_VARIANT_INDEX_RE = re.compile(r"[-_]\d+$")


def strip_duplicate_suffix(ref: str) -> str:
    """
    Remove one trailing variant index suffix (``_1``, ``-2``, ``-1``, …) from the **end only**.

    Hyphens/underscores inside the reference are kept, e.g. ``SAU-093CF``, ``MY-PART-NAME``.
    """
    if not ref:
        return ""
    s = str(ref).strip()
    m = _TRAILING_VARIANT_INDEX_RE.search(s)
    if not m:
        return s
    base = s[: m.start()].rstrip()
    return base or s


def strip_duplicate_suffix_repeated(ref: str) -> str:
    """Strip trailing ``-N`` / ``_N`` at the end only, repeatedly (e.g. ``sku-1-2`` → ``sku``)."""
    s = str(ref or "").strip()
    if not s:
        return ""
    for _ in range(24):
        n = strip_duplicate_suffix(s)
        if n == s:
            return s
        s = n
    return s


def search_keys(reference: str) -> List[str]:
    """Ordered search terms: raw, normalized, then progressively shorter base SKUs."""
    keys: List[str] = []
    seen = set()

    def add(s: str) -> None:
        s = str(s or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        keys.append(s)

    raw = str(reference or "").strip().lstrip("#").strip()
    add(raw)
    norm = normalize_ref(reference)
    add(norm)
    cur = norm
    for _ in range(24):
        n = strip_duplicate_suffix(cur)
        if n == cur:
            break
        cur = n
        add(cur)
    return keys


def normalize_id(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


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


def sku_matches_exact(page_sku: str, target_sku: str) -> bool:
    """Exact Reference match (``#`` and case ignored). Used after att_* variant selection."""
    a = normalize_ref(page_sku).lower()
    b = normalize_ref(target_sku).lower()
    return bool(a and b and a == b)


def sku_matches_on_page(page_sku: str, target_sku: str) -> bool:
    """
    Loose match for distribution/search only: also treats trailing ``-N`` / ``_N`` as equivalent.

    Do **not** use for motorsport variant selection — ``SVWS059`` must not match ``SVWS059_1``.
    """
    a = normalize_ref(page_sku).lower()
    b = normalize_ref(target_sku).lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return strip_duplicate_suffix_repeated(a) == strip_duplicate_suffix_repeated(b)


def _label_to_att_key(label: str) -> str:
    l = label.lower().strip()
    if not l or l == "title":
        return ""
    if "color" in l or "kleur" in l:
        return "att_color"
    if "option" in l or "optie" in l:
        return "att_option"
    if "thickness" in l or "dikte" in l:
        return "att_thickness"
    if "wastegate" in l or l == "version":
        return "att_wastegate"
    if "tailpipe" in l or "uitlaatpijp" in l:
        return "att_tailpipes"
    if "valve" in l or "klep" in l:
        return "att_valves"
    if "gearbox" in l:
        return "att_gearbox"
    if "tip" in l:
        return "att_tips"
    if "finish" in l or "afwerking" in l:
        return "att_finish"
    if "diameter" in l:
        return "att_diameter"
    if "design" in l:
        return "att_design"
    if "can" in l and "size" in l:
        return "att_can_size"
    if "year" in l or "jaar" in l:
        return "att_year"
    if "size" in l or "maat" in l:
        return "att_size"
    if "thread" in l:
        return "att_thread"
    if "bore" in l:
        return "att_bore"
    if "flow" in l:
        return "att_flow"
    if "type" in l:
        return "att_type"
    clean = re.sub(r"[^a-z0-9]+", "_", l).strip("_")
    return f"att_{clean}" if clean else ""


def _normalize_att_map(attributes: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (attributes or {}).items():
        key = str(k or "").strip().lower().replace("-", "_")
        val = str(v or "").strip()
        if key and val:
            out[key] = val
    return out


def _option_matches_exact(option_text: str, targets: List[str]) -> bool:
    """Option label must equal a sheet value or a translation target (case-insensitive)."""
    a = str(option_text or "").strip().lower()
    if not a or not targets:
        return False
    for target in targets:
        b = str(target or "").strip().lower()
        if a and b and a == b:
            return True
    return False


def _option_matches_any_target(option_text: str, targets: List[str]) -> bool:
    """Looser match used only when brute-forcing combos with no sheet att_* values."""
    a = str(option_text or "").strip().lower()
    if not a or not targets:
        return False
    for target in targets:
        b = str(target or "").strip().lower()
        if not b:
            continue
        if a == b or b in a or a in b:
            return True
    return False


def select_option_by_value(driver, wait, fieldset_index: int, value: str) -> bool:
    select_xpath = f'//*[@id="attributes"]/fieldset[{fieldset_index}]//select'
    try:
        select = wait.until(EC.element_to_be_clickable((By.XPATH, select_xpath)))
    except Exception:
        return False
    try:
        select.click()
    except Exception:
        driver.execute_script("arguments[0].click();", select)
    time.sleep(0.2)
    option_elem = None
    for o in select.find_elements(By.TAG_NAME, "option"):
        if o.get_attribute("value") == value:
            option_elem = o
            break
    if not option_elem:
        return False
    try:
        option_elem.click()
    except Exception:
        driver.execute_script("arguments[0].click();", option_elem)
    try:
        driver.execute_script("arguments[0].selected = true;", option_elem)
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", select
        )
    except Exception:
        pass
    time.sleep(0.5)
    return True


def _collect_variations_meta(driver) -> List[Tuple[str, int, List[Dict[str, str]]]]:
    variations: List[Tuple[str, int, List[Dict[str, str]]]] = []
    try:
        fieldsets = driver.find_elements(By.XPATH, '//*[@id="attributes"]/fieldset')
        for f_idx, fs in enumerate(fieldsets, start=1):
            try:
                label = fs.find_element(By.TAG_NAME, "label").text.strip()
                select = fs.find_element(By.TAG_NAME, "select")
                valid = []
                for opt in select.find_elements(By.TAG_NAME, "option"):
                    val = opt.get_attribute("value")
                    text = opt.text.strip()
                    if not val or "select" in text.lower():
                        continue
                    valid.append({"value": val, "text": text})
                if valid:
                    variations.append((label, f_idx, valid))
            except Exception:
                continue
    except Exception:
        pass
    return variations


def _select_variant_by_exact_attributes(
    driver,
    wait,
    target_sku: str,
    att_map: Dict[str, str],
    variations: List[Tuple[str, int, List[Dict[str, str]]]],
) -> bool:
    """
    Every non-empty sheet att_* (Dutch on upload sheet) must map to a PDP fieldset.
    Option text is matched via att_value_translation_lookup.json → English PDP label,
    then product_reference must match the row SKU.
    """
    required_keys = set(att_map.keys())
    matched_keys: set[str] = set()
    selections: List[Tuple[int, str]] = []

    for label, field_idx, options in variations:
        att_key = _label_to_att_key(label)
        if att_key not in att_map:
            continue
        targets = match_targets_for_sheet_value(att_key, att_map[att_key])
        chosen = None
        for opt in options:
            if _option_matches_exact(opt["text"], targets):
                chosen = opt["value"]
                break
        if not chosen:
            return False
        matched_keys.add(att_key)
        selections.append((field_idx, chosen))

    if matched_keys != required_keys:
        return False

    for field_idx, val in selections:
        if not select_option_by_value(driver, wait, field_idx, val):
            return False
    time.sleep(1.5)
    # Trust att_* selection (upload Dutch → JSON → English PDP). Motorsport often shows a
    # base reference (e.g. SVWS059) while the upload sheet uses SVWS059_1 for Shopify.
    return True


def select_variant_by_attributes(
    driver,
    wait,
    target_sku: str,
    attributes: Optional[Dict[str, str]] = None,
) -> bool:
    """Select dropdowns using sheet att_* values, then verify SKU on page."""
    att_map = _normalize_att_map(attributes or {})
    variations = _collect_variations_meta(driver)

    if not variations:
        try:
            page_sku = driver.find_element(By.XPATH, '//*[@id="product_reference"]/span').text.strip()
            return sku_matches_exact(page_sku, target_sku)
        except Exception:
            return False

    if att_map:
        return _select_variant_by_exact_attributes(
            driver, wait, target_sku, att_map, variations
        )

    if not target_sku:
        return False

    # No sheet att_*: try every option combo until product_reference matches SKU.
    all_option_lists = [v[2] for v in variations]
    for combo in itertools.product(*all_option_lists):
        for (_, field_idx, _), chosen in zip(variations, combo):
            select_option_by_value(driver, wait, field_idx, chosen["value"])
        time.sleep(1.0)
        try:
            page_sku = driver.find_element(By.XPATH, '//*[@id="product_reference"]/span').text.strip()
            if sku_matches_exact(page_sku, target_sku):
                return True
        except Exception:
            continue
    return False


def page_reference_sku(driver) -> str:
    try:
        return driver.find_element(By.XPATH, '//*[@id="product_reference"]/span').text.strip()
    except Exception:
        return ""


def extract_price_from_page(driver, reference: str = "") -> Optional[str]:
    """
    Motorsport PDP price: ``old_price_display`` first, then ``our_price_display`` + fallbacks.
    """
    selectors: List[str] = [MOTORSPORT_OLD_PRICE_XPATH]
    for sel in PRICE_SELECTORS:
        if sel not in selectors:
            selectors.append(sel)
    for sel in selectors:
        try:
            el = driver.find_element(By.XPATH, sel)
            if el.is_displayed():
                text = el.text.strip()
                if text and any(c.isdigit() for c in text):
                    return text
        except Exception:
            continue
    return None


def wait_for_availability_text(driver, sku: str = "", max_wait: int = 15) -> str:
    start = time.time()
    while time.time() - start < max_wait:
        for xpath in AVAILABILITY_XPATHS:
            try:
                elem = driver.find_element(By.XPATH, xpath)
                if elem.is_displayed():
                    text = elem.text.strip()
                    if text:
                        return text
            except Exception:
                pass
        try:
            for line in driver.find_element(By.TAG_NAME, "body").text.split("\n"):
                lower = line.lower()
                if any(k in lower for k in ["in stock", "en stock", "out of stock", "days"]):
                    return line.strip()
        except Exception:
            pass
        time.sleep(1)
    return ""


def _search_site(driver, query: str) -> bool:
    try:
        if "all-stars-motorsport.com" not in (driver.current_url or ""):
            driver.get(SITE_BASE)
            time.sleep(1.5)
        search = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="search_query_top"]'))
        )
        search.clear()
        time.sleep(0.2)
        search.send_keys(query)
        search.send_keys(Keys.ENTER)
        time.sleep(1.2)
        return True
    except Exception:
        return False


def _collect_result_links(driver, max_links: int = 12) -> List[str]:
    links: List[str] = []
    for xp in [
        '//ul[contains(@class,"product_list")]//a[contains(@class,"product-name")]',
        '//ul[contains(@class,"product_list")]//a[contains(@class,"product_img_link")]',
        '//a[contains(@class,"product-name")]',
    ]:
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


def _page_product_id_matches(driver, expected_pid: str) -> bool:
    pid = extract_product_id_from_url(driver.current_url or "")
    return bool(pid and normalize_id(pid) == expected_pid)


def _open_via_sku_search(
    driver,
    product_id: str,
    reference: str,
    *,
    attributes: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """
    Search motorsport site by SKU/Reference terms, open PDP when product_id matches.
    If att_* are provided, prefer a result where variant selection + reference check succeed.
    """
    expected_pid = normalize_id(product_id)
    target_sku = str(reference or "").strip()
    att_map = _normalize_att_map(attributes or {})
    need_variant_proof = bool(att_map) or bool(target_sku)

    for key in search_keys(reference):
        if not key or not _search_site(driver, key):
            continue

        candidates: List[str] = []
        if _page_product_id_matches(driver, expected_pid):
            candidates.append(driver.current_url or "")

        for href in _collect_result_links(driver):
            if href and href not in candidates:
                candidates.append(href)

        for href in candidates:
            if not href:
                continue
            try:
                if href != (driver.current_url or ""):
                    driver.get(href.split("?")[0])
                    time.sleep(1.0)
            except Exception:
                continue
            if not _page_product_id_matches(driver, expected_pid):
                continue

            if not need_variant_proof:
                return True, "search_by_sku_product_id"

            wait = WebDriverWait(driver, 10)
            if select_variant_by_attributes(driver, wait, target_sku, attributes):
                return True, "search_by_sku_variant_matched"

            # product_id OK but variant/att_* not confirmed — try next search result
            continue

    return False, "product_not_found"


def open_product_page(
    driver,
    product_id: str,
    reference: str,
    url: str = "",
    *,
    attributes: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """
    Open the correct product page. Returns (success, status_reason).

    With a sheet URL: open it when product_id matches; otherwise fall back to SKU search.
    Without URL: search by SKU/Reference (``search_keys``), match ``product_id``, then
    variant/reference via ``att_*`` + translation JSON when attributes are passed.
    """
    expected_pid = normalize_id(product_id)
    if not expected_pid:
        return False, "empty_product_id"

    url_clean = str(url or "").strip()
    if url_clean:
        try:
            driver.get(url_clean.split("?")[0])
            time.sleep(1.5)
            if _page_product_id_matches(driver, expected_pid):
                return True, "opened_url"
        except Exception:
            pass
        # URL missing, wrong product, or id mismatch → search by SKU like rows with no url
        ok, status = _open_via_sku_search(
            driver, product_id, reference, attributes=attributes
        )
        if ok:
            return True, f"url_fallback_{status}"
        return False, "url_product_id_mismatch"

    return _open_via_sku_search(
        driver, product_id, reference, attributes=attributes
    )


def scrape_price_and_availability(
    driver,
    product_id: str,
    reference: str,
    url: str = "",
    attributes: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Returns (raw_price_text, raw_availability_text, status).
    Prices come from all-stars-motorsport.com only.

    Rows without ``url``: site search by SKU/Reference, match ``product_id`` on PDP,
    then ``att_*`` options (Dutch values resolved via att_value_translation_lookup.json)
    and verify Reference on the page.
    """
    target_sku = str(reference or "").strip()
    if not target_sku:
        return None, None, "empty_reference"

    opened, open_status = open_product_page(
        driver,
        product_id,
        reference,
        url=url,
        attributes=attributes,
    )
    if not opened:
        return None, None, open_status

    wait = WebDriverWait(driver, 10)
    att_map = _normalize_att_map(attributes or {})
    # Always select variant from sheet att_* (Type, Tailpipes, …) and verify Reference SKU
    # before reading price — including when the row has a direct product URL.
    if att_map or target_sku:
        if not select_variant_by_attributes(driver, wait, target_sku, attributes):
            return None, None, "variant_not_matched"

    page_sku = page_reference_sku(driver)
    status = "ok"
    if page_sku and not sku_matches_exact(page_sku, target_sku):
        status = f"ok_page_ref_{normalize_ref(page_sku)}"

    price = extract_price_from_page(driver, reference=target_sku)
    avail = wait_for_availability_text(driver, target_sku)
    if not price:
        return None, avail or None, "price_not_found"
    return price, avail or None, status


def scrape_availability_only(
    driver,
    product_id: str,
    reference: str,
    url: str = "",
    attributes: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], str]:
    """
    Availability from all-stars-motorsport.com (price is discarded; use
    ``scrape_price_and_availability`` when motorsport retail fallback is needed).
    Returns (raw_availability_text, status).
    """
    _, avail, status = scrape_price_and_availability(
        driver,
        product_id=product_id,
        reference=reference,
        url=url,
        attributes=attributes,
    )
    if avail:
        return avail, status
    if status in ("ok",) or str(status).startswith("ok_page_ref_"):
        return None, status
    return None, status
