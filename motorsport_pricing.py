#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Retail and cost price calculation for All-Stars Distribution scraped prices.

Distribution retail (sheet price + Shopify):
  scraped × 1.21 + 15 → charm rounding (e.g. 626,15 → 624,99)

Motorsport fallback retail (when distribution has no list price):
  scraped + 15 (no ×1.21, no charm rounding)

Cost (sheet «cost price» + Shopify Cost):
  scraped + 15 (no VAT strip, no ×1.21)
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

PRICE_MULTIPLIER = 1.21
PRICE_ADDITION = 15.0
COST_PRICE_ADDITION = 15.0
MIN_REASONABLE_PRICE = 10.0


def parse_scraped_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    try:
        clean = str(price_str).replace("€", "").replace("$", "").replace("£", "").strip()
        clean = clean.replace(" ", "").replace(",", ".")
        return float(clean)
    except (TypeError, ValueError):
        return None


def round_to_retail_price(value: float) -> float:
    """
    Charm pricing: round to nearest €5, then subtract €0.01.
    Examples: 626.15 → 624.99, 267.76 → 269.99
    """
    if value <= 0:
        return 0.0
    return round(value / 5.0) * 5.0 - 0.01


def format_price_euro(value: float) -> str:
    whole = int(value)
    cents = int(round((value - whole) * 100))
    if cents >= 100:
        whole += 1
        cents = 0
    return f"{whole},{cents:02d}"


def scale_scraped_raw_price(scraped_price_str: str, multiplier: int) -> Optional[str]:
    """
    Multiply per-unit scraped raw by pack quantity before +15 / ×1.21+15.
    Returns Dutch-formatted string or None if input does not parse.
    """
    if multiplier <= 1 or not scraped_price_str:
        return scraped_price_str
    scraped = parse_scraped_price(scraped_price_str)
    if scraped is None:
        return scraped_price_str
    return format_price_euro(scraped * multiplier)


def calculate_final_price(
    scraped_price_str: str,
    sku: str = "",
    *,
    log_fn=None,
) -> Optional[str]:
    """
    Retail: distribution list/card price × 1.21 + 15, then charm rounding.
    Returns Dutch-formatted string (e.g. '624,99') or None.
    """
    scraped = parse_scraped_price(scraped_price_str)
    if scraped is None:
        return None
    if scraped < MIN_REASONABLE_PRICE:
        if log_fn:
            log_fn(sku, f"Scraped price {scraped:.2f} below minimum {MIN_REASONABLE_PRICE}")
        return None

    with_markup = scraped * PRICE_MULTIPLIER
    before_round = with_markup + PRICE_ADDITION
    final_value = round_to_retail_price(before_round)
    final_str = format_price_euro(final_value)

    if log_fn:
        log_fn(
            sku,
            (
                f"raw={scraped:.2f} ×{PRICE_MULTIPLIER}={with_markup:.2f} "
                f"+{PRICE_ADDITION}={before_round:.2f} → {final_str}"
            ),
        )
    return final_str


def calculate_motorsport_fallback_price(
    scraped_price_str: str,
    sku: str = "",
    *,
    log_fn=None,
) -> Optional[str]:
    """
    Retail when distribution has no price: motorsport PDP price + 15 only.
    No ×1.21 multiplier and no charm rounding.
    """
    scraped = parse_scraped_price(scraped_price_str)
    if scraped is None:
        return None
    if scraped < MIN_REASONABLE_PRICE:
        if log_fn:
            log_fn(sku, f"Motorsport price {scraped:.2f} below minimum {MIN_REASONABLE_PRICE}")
        return None

    final_value = scraped + PRICE_ADDITION
    final_str = format_price_euro(final_value)

    if log_fn:
        log_fn(
            sku,
            f"motorsport fallback: raw={scraped:.2f} +{PRICE_ADDITION}={final_value:.2f} → {final_str}",
        )
    return final_str


def calculate_cost_price(
    scraped_price_str: str,
    sku: str = "",
    *,
    log_fn=None,
) -> Optional[str]:
    """
    Distribution cost column: parse scraped price + fixed margin.
    No VAT strip or retail charm rounding beyond +15.
    """
    scraped = parse_scraped_price(scraped_price_str)
    if scraped is None:
        return None
    if scraped < MIN_REASONABLE_PRICE:
        if log_fn:
            log_fn(sku, f"Cost base {scraped:.2f} below minimum {MIN_REASONABLE_PRICE}")
        return None
    cost_value = scraped + COST_PRICE_ADDITION
    final_str = format_price_euro(cost_value)
    if log_fn:
        log_fn(sku, f"cost: raw={scraped:.2f} +{COST_PRICE_ADDITION} → {final_str}")
    return final_str


def calculation_steps(scraped_price_str: str) -> Optional[Tuple[float, float, float, str]]:
    """Return (scraped, with_markup, before_round, final_str) for distribution retail debugging."""
    scraped = parse_scraped_price(scraped_price_str)
    if scraped is None:
        return None
    with_markup = scraped * PRICE_MULTIPLIER
    before_round = with_markup + PRICE_ADDITION
    final_str = format_price_euro(round_to_retail_price(before_round))
    return scraped, with_markup, before_round, final_str


def motorsport_fallback_steps(scraped_price_str: str) -> Optional[Tuple[float, float, str]]:
    """Return (scraped, after_addition, final_str) for motorsport fallback debugging."""
    scraped = parse_scraped_price(scraped_price_str)
    if scraped is None:
        return None
    after_addition = scraped + PRICE_ADDITION
    final_str = format_price_euro(after_addition)
    return scraped, after_addition, final_str
