#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hardcoded borderline SKU rules for All-Stars Distribution (cost + retail).

Sheet Reference is unchanged; search and result-card matching use the mapped
distribution SKU below when listed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from motorsport_pricing import scale_scraped_raw_price

# Sheet Reference (strip #, case-insensitive) → distribution search + card match SKU
DISTRIBUTION_COST_SKU_BY_SHEET_REF: Dict[str, str] = {
    "SVWS059": "SVWS059C",
    "SVWS059_1": "SVWS059",
    "SVWS052C_1": "SVW052C",
    "SVW065_1": "SVW066",
    "SVW065-1": "SVW066",
    "VWR650000-CHR": "VWR650000-BLK",
    "VWR12G7R600": "VWR1200R600E",
    "VWR12G7R601": "VWR1200R601E",
    # Motorsport/sheet: FMOC13-BLA — distribution lists as FMOC13-BLACK
    "FMOC13-BLA": "FMOC13-BLACK",
    # Forge clamp kits — with clamps (-HC on distribution)
    "FMKC022-BLA": "FMKC022-BLA-HC",
    "FMKC022-BLU": "FMKC022-BLU-HC",
    "FMKC022-RED": "FMKC022-RED-HC",
    "FMBH18T-BLA": "FMBH18T-BLA-HC",
    "FMBH18T-BLU": "FMBH18T-BLU-HC",
    "FMBH18T-RED": "FMBH18T-RED-HC",
    # _1 / Zonder klemmen — distribution SKU without -HC
    "FMKC022-BLA_1": "FMKC022-BLA",
    "FMKC022-BLU_1": "FMKC022-BLU",
    "FMKC022-RED_1": "FMKC022-RED",
    "FMBH18T-BLA_1": "FMBH18T-BLA",
    "FMBH18T-BLU_1": "FMBH18T-BLU",
    "FMBH18T-RED_1": "FMBH18T-RED",
}

# Excel/sheet «_1» or att_option Zonder klemmen → base Reference on upload sheet
SHEET_SKU_LOOKUP_ALIASES: Dict[str, Tuple[str, str]] = {
    "FMKC022-BLA_1": ("FMKC022-BLA", "zonder klemmen"),
    "FMBH18T-BLA_1": ("FMBH18T-BLA", "zonder klemmen"),
    "FMBH18T-BLU_1": ("FMBH18T-BLU", "zonder klemmen"),
    "FMBH18T-RED_1": ("FMBH18T-RED", "zonder klemmen"),
}

# Forge families using -HC on distribution when «With Clamps»
_FORGE_HC_KIT_PREFIXES = ("FMKC022-", "FMBH18T-")

# Same wholesale/retail on distribution for all colour variants — try BLU if RED not listed.
DISTRIBUTION_COLOR_FALLBACKS: Dict[str, List[str]] = {
    "FMGOLFIND-RED": ["FMGOLFIND-BLU"],
    "FMKTMK7-RED": ["FMKTMK7-BLU"],
}

_FORGE_COLOR_CODES = ("BLA", "BLU", "RED")


def _forge_color_fallback_distribution_skus(primary_match: str) -> List[str]:
    """
    Other Forge clamp-kit colours on distribution (same ``-HC`` suffix as primary).

    Example: ``FMBH18T-BLU-HC`` not listed → try ``FMBH18T-BLA-HC``, ``FMBH18T-RED-HC``.
    """
    m = clean_distribution_sku(primary_match).upper()
    for prefix in _FORGE_HC_KIT_PREFIXES:
        if not m.startswith(prefix):
            continue
        tail = m[len(prefix) :]
        if tail.endswith("-HC"):
            suffix = "-HC"
            color = tail[: -len(suffix)]
        else:
            suffix = ""
            color = tail
        if color not in _FORGE_COLOR_CODES:
            return []
        out: List[str] = []
        for other in _FORGE_COLOR_CODES:
            if other == color:
                continue
            out.append(f"{prefix}{other}{suffix}")
        return out
    return []

# VWR ignition coil packs: sheet Reference suffix is coil count; scrape is per single coil.
VWR_IGNITION_COIL_PACK_MULTIPLIER: Dict[str, int] = {
    "VWR900004-3": 3,
    "VWR900001-4": 4,
    "VWR900001-6": 6,
    "VWR900001-5": 5,
    "VWR900003-4": 4,
    "VWR900003-5": 5,
    "VWR900003-6": 6,
    "VWR900003-8": 8,
    "VWR900002-4": 4,
    "VWR900002-6": 6,
    "VWR900002-5": 5,
    "VWR90000-4": 4,
    "VWR90000-6": 6,
    "VWR90000-5": 5,
}

# End of string only: _1, _2, -1, -2 (exactly one digit — not _10, -12, mid-string hyphens)
_TRAILING_SINGLE_DIGIT_SUFFIX_RE = re.compile(r"[-_]\d$")


def _sheet_ref_key(reference: str) -> str:
    s = str(reference or "").strip()
    while s.startswith("#"):
        s = s[1:].strip()
    return s.upper()


def clean_distribution_sku(sheet_reference: str) -> str:
    """Strip leading ``#`` only (full Reference without prefix)."""
    return str(sheet_reference or "").strip().lstrip("#").strip()


def strip_single_digit_variant_suffix(sheet_reference: str) -> str:
    """
    Remove trailing variant indices at the **end only**, repeatedly.

    Example: ``#VWR12G7R600ITINLET-1_1`` → ``VWR12G7R600ITINLET`` (strips ``_1`` then ``-1``).

    Each step removes exactly one ``_N`` or ``-N`` (one digit). Does not strip ``_10`` / ``-12``
    in one step (two digits) or hyphens in the middle (``SAU-093CF``).
    """
    s = clean_distribution_sku(sheet_reference)
    if not s:
        return ""
    while True:
        m = _TRAILING_SINGLE_DIGIT_SUFFIX_RE.search(s)
        if not m:
            break
        base = s[: m.start()].rstrip()
        if not base or base == s:
            break
        s = base
    return s


def row_matches_sheet_sku_alias(
    var: Dict[str, Any],
    want_key: str,
) -> bool:
    """Match upload row when CLI/excel uses ``_1`` but sheet uses base Reference + att_option."""
    sku_key = _sheet_ref_key(str(var.get("sku") or ""))
    want_key = _sheet_ref_key(want_key)
    if sku_key == want_key:
        alias = SHEET_SKU_LOOKUP_ALIASES.get(want_key)
        if alias:
            base, opt_need = alias
            if sku_key == _sheet_ref_key(base):
                att = _variant_option_label(var.get("attributes") or {})
                return opt_need in att
        return True
    alias = SHEET_SKU_LOOKUP_ALIASES.get(want_key)
    if alias:
        base, opt_need = alias
        if sku_key == _sheet_ref_key(base):
            return opt_need in _variant_option_label(var.get("attributes") or {})
    return False


def _variant_option_label(attributes: Optional[Dict[str, str]]) -> str:
    if not attributes:
        return ""
    for k, v in attributes.items():
        if "option" in str(k).lower():
            t = str(v or "").strip().lower()
            if t:
                return t
    return ""


def _forge_hc_kit_without_clamps(
    sheet_reference: str,
    attributes: Optional[Dict[str, str]] = None,
) -> bool:
    """Forge clamp kit without clamps: ``_1`` suffix or att_option «Zonder klemmen» (FMKC022, FMBH18T)."""
    key = _sheet_ref_key(sheet_reference)
    if not any(key.startswith(p) for p in _FORGE_HC_KIT_PREFIXES):
        return False
    if key.endswith("_1"):
        return True
    return "zonder klemmen" in _variant_option_label(attributes)


def distribution_search_sku(
    sheet_reference: str,
    attributes: Optional[Dict[str, str]] = None,
) -> str:
    """
    Primary SKU for distribution search and card match.

    Order: explicit ``_1`` overrides → without-clamps (by att_option / ``_1``) →
    override map (-HC) → strip suffix → override again.
    """
    key = _sheet_ref_key(sheet_reference)

    if key in DISTRIBUTION_COST_SKU_BY_SHEET_REF and key.endswith("_1"):
        return DISTRIBUTION_COST_SKU_BY_SHEET_REF[key]

    if _forge_hc_kit_without_clamps(sheet_reference, attributes):
        return strip_single_digit_variant_suffix(sheet_reference) or key

    override = DISTRIBUTION_COST_SKU_BY_SHEET_REF.get(key)
    if override:
        return override

    stripped = strip_single_digit_variant_suffix(sheet_reference)
    if stripped:
        override = DISTRIBUTION_COST_SKU_BY_SHEET_REF.get(stripped.upper())
        if override:
            return override
    return stripped or key


def distribution_card_match_sku(
    sheet_reference: str,
    attributes: Optional[Dict[str, str]] = None,
) -> str:
    """Same rules as ``distribution_search_sku`` (exact ``card_sku ==`` this value)."""
    return distribution_search_sku(sheet_reference, attributes)


def distribution_search_terms(
    sheet_reference: str,
    attributes: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Ordered search-box terms (legacy helper — prefer ``distribution_scrape_plans``)."""
    seen: set[str] = set()
    out: List[str] = []
    for search, _card in distribution_scrape_plans(sheet_reference, attributes):
        t = str(search or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def distribution_scrape_plans(
    sheet_reference: str,
    attributes: Optional[Dict[str, str]] = None,
) -> List[Tuple[str, str]]:
    """
    Ordered (search_term, card_match_sku) attempts for All-Stars Distribution.

    Tries the sheet/override SKU first, then colour fallbacks (e.g. RED → BLU).
    When override differs from sheet ref, also searches sheet ref while matching override.
    """
    key = _sheet_ref_key(sheet_reference)
    sheet_clean = clean_distribution_sku(sheet_reference)
    primary_match = distribution_card_match_sku(sheet_reference, attributes)

    match_chain: List[str] = []

    def _add_match(candidate: str) -> None:
        c = clean_distribution_sku(candidate)
        if c and c.upper() not in {m.upper() for m in match_chain}:
            match_chain.append(c)

    _add_match(primary_match)
    for fallback in _forge_color_fallback_distribution_skus(primary_match):
        _add_match(fallback)
    for fallback in DISTRIBUTION_COLOR_FALLBACKS.get(key, []):
        _add_match(fallback)

    plans: List[Tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, match in enumerate(match_chain):
        search_terms = [match]
        if i == 0 and sheet_clean and sheet_clean.upper() != match.upper():
            search_terms.append(sheet_clean)
        for search in search_terms:
            sig = (search.upper(), match.upper())
            if sig in seen:
                continue
            seen.add(sig)
            plans.append((search, match))

    return plans


def vwr_ignition_coil_pack_multiplier(sheet_reference: str) -> int:
    """Pack quantity for explicit VWR ignition-coil SKUs; 1 if not listed."""
    return VWR_IGNITION_COIL_PACK_MULTIPLIER.get(_sheet_ref_key(sheet_reference), 1)


def apply_vwr_pack_multiplier_to_raw(
    sheet_reference: str, raw_price: Optional[str]
) -> Optional[str]:
    """Scale per-unit distribution/motorsport raw by pack count before pricing formulas."""
    mult = vwr_ignition_coil_pack_multiplier(sheet_reference)
    if mult <= 1 or not raw_price:
        return raw_price
    return scale_scraped_raw_price(raw_price, mult)


def uses_motorsport_old_price_display(sheet_reference: str) -> bool:
    """Deprecated: retail prices come from distribution, not motorsport PDP."""
    return False


def list_override_sheet_skus() -> list[str]:
    """Canonical sheet-style references for tests."""
    return [f"#{k}" if not k.startswith("#") else k for k in DISTRIBUTION_COST_SKU_BY_SHEET_REF]
