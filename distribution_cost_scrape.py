#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
All-Stars Distribution: login, search by SKU, exact card match, scrape cost + retail.

On each search-result ``li`` the site shows two prices:
  • **Cost** (wholesale): ``…/li[n]/div/div[3]/div[3]/span``
  • **Retail** (list): ``…/li[n]/div/div[3]/div[2]/div/span[1]/span``

Sheet cost = cost raw + 15. Sheet retail = retail raw × 1.21 + 15 (motorsport fallback: PDP price + 15).

Search results are paginated (``#pagination_bottom``): if the SKU is not on page 1, later pages are checked.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from sku_price_overrides import (
    clean_distribution_sku,
    distribution_scrape_plans,
)

DISTRIBUTION_LOGIN_URL = os.environ.get(
    "DISTRIBUTION_LOGIN_URL", "https://www.all-stars-distribution.com/"
)
DISTRIBUTION_EMAIL = os.environ.get("DISTRIBUTION_EMAIL", "")
DISTRIBUTION_PASSWORD = os.environ.get("DISTRIBUTION_PASSWORD", "")

# 1-based li index on matched search-result card
LIST_COST_XPATH_TMPL = (
    '//*[@id="center_column"]/ul/li[{n}]/div/div[3]/div[3]/span'
)
LIST_RETAIL_XPATH_TMPL = (
    '//*[@id="center_column"]/ul/li[{n}]/div/div[3]/div[2]/div/span[1]/span'
)
CARD_SKU_XPATHS = (
    ".//div/div[2]/div[1]/div/span",
    './/span[contains(@class, "sku") or contains(@class, "reference")]',
)
CARD_COST_REL_XPATH = ".//div/div[3]/div[3]/span"
CARD_RETAIL_REL_XPATH = ".//div/div[3]/div[2]/div/span[1]/span"
CARD_LINK_XPATHS = (
    ".//a[contains(@class, 'product_img_link')]",
    ".//a[contains(@class, 'product-name')]",
    ".//h5/a",
    ".//a[contains(@href, '.html')]",
)

# Search results sometimes load slowly (Chrome no_results flake)
SEARCH_POST_SUBMIT_WAIT_SEC = 4.0
LIST_WAIT_SEC = 18
NO_RESULTS_RETRIES = 3

RESULT_LIST_UL_XPATHS = (
    '//*[@id="center_column"]/ul',
    '//*[@id="center_column"]//ul[contains(@class,"product_list")]',
)
RESULT_LI_XPATH = (
    '//*[@id="center_column"]//li[contains(@class,"ajax_block_product")]'
)
PAGINATION_UL_XPATH = '//*[@id="pagination_bottom"]/ul'
PAGINATION_CLICK_WAIT_SEC = 5.0
MAX_PAGINATION_PAGES = 20


def distribution_search_keys(sheet_reference: str) -> List[str]:
    return [search for search, _ in distribution_scrape_plans(sheet_reference)]


def _card_sku_matches(card_sku: str, target: str) -> bool:
    a = clean_distribution_sku(card_sku)
    b = clean_distribution_sku(target)
    return bool(a and b and a == b)


def _valid_price_text(text: str) -> bool:
    t = str(text or "").strip()
    return bool(t and any(c.isdigit() for c in t))


def _xpath_price(driver, xpath: str) -> Optional[str]:
    try:
        el = driver.find_element(By.XPATH, xpath)
        t = el.text.strip()
        if _valid_price_text(t):
            return t
    except Exception:
        pass
    return None


def login_distribution_site(driver, email: str = "", password: str = "") -> bool:
    email = (email or DISTRIBUTION_EMAIL).strip()
    password = (password or DISTRIBUTION_PASSWORD).strip()
    if not email or not password:
        return False
    try:
        driver.get(DISTRIBUTION_LOGIN_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="email"]'))
        )
        driver.find_element(By.XPATH, '//*[@id="email"]').send_keys(email)
        driver.find_element(By.XPATH, '//*[@id="passwd"]').send_keys(password)
        driver.find_element(By.XPATH, '//*[@id="SubmitLogin"]').click()
        time.sleep(3)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="search_query_top"]'))
        )
        return True
    except Exception:
        return False


def _extract_card_sku(card) -> str:
    for xp in CARD_SKU_XPATHS:
        try:
            t = card.find_element(By.XPATH, xp).text.strip()
            if t:
                return t
        except Exception:
            continue
    return ""


def _extract_cost_retail_from_card(card) -> Tuple[Optional[str], Optional[str]]:
    cost = retail = None
    try:
        t = card.find_element(By.XPATH, CARD_COST_REL_XPATH).text.strip()
        if _valid_price_text(t):
            cost = t
    except Exception:
        pass
    try:
        t = card.find_element(By.XPATH, CARD_RETAIL_REL_XPATH).text.strip()
        if _valid_price_text(t):
            retail = t
    except Exception:
        pass
    return cost, retail


def _extract_cost_retail_by_li_index(
    driver, li_index_1based: int
) -> Tuple[Optional[str], Optional[str]]:
    n = li_index_1based
    cost = _xpath_price(driver, LIST_COST_XPATH_TMPL.format(n=n))
    retail = _xpath_price(driver, LIST_RETAIL_XPATH_TMPL.format(n=n))
    return cost, retail


def _open_product_from_card(driver, card) -> bool:
    for xp in CARD_LINK_XPATHS:
        try:
            link = card.find_element(By.XPATH, xp)
            href = (link.get_attribute("href") or "").strip()
            if href and ".html" in href:
                driver.get(href.split("?")[0])
                time.sleep(1.5)
                return True
        except Exception:
            continue
    return False


def _scroll_pagination_into_view(driver) -> None:
    try:
        pag = driver.find_element(By.XPATH, PAGINATION_UL_XPATH)
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
            pag,
        )
        time.sleep(0.35)
    except Exception:
        pass


def _pagination_ul(driver):
    try:
        _scroll_pagination_into_view(driver)
        return driver.find_element(By.XPATH, PAGINATION_UL_XPATH)
    except Exception:
        return None


def _active_pagination_li_index(ul) -> int:
    """1-based index of the current page (``li`` with active/current class)."""
    lis = ul.find_elements(By.TAG_NAME, "li")
    for i, li in enumerate(lis, start=1):
        cls = (li.get_attribute("class") or "").lower()
        if "active" in cls or "current" in cls:
            return i
    return 1


def _pagination_li_click_targets(driver) -> List[Tuple[int, str]]:
    """
    Non-active pagination pages: (1-based ``li`` index, label).

    Page 1 is often ``li[1]`` (active); page 2 is ``li[2]`` with ``<a href>``.
    """
    ul = _pagination_ul(driver)
    if not ul:
        return []
    active_idx = _active_pagination_li_index(ul)
    lis = ul.find_elements(By.TAG_NAME, "li")
    targets: List[Tuple[int, str]] = []
    for i, li in enumerate(lis, start=1):
        if i == active_idx:
            continue
        cls = (li.get_attribute("class") or "").lower()
        if "disabled" in cls:
            continue
        try:
            a = li.find_element(By.TAG_NAME, "a")
            href = (a.get_attribute("href") or "").strip()
            if not href or href == "#":
                continue
            label = (a.text or "").strip() or str(i)
            targets.append((i, label))
        except Exception:
            continue
    return targets


def _click_pagination_li(
    driver,
    li_index_1based: int,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """Click ``pagination_bottom/ul/li[n]/a`` and wait for results to load."""
    xpath = f"{PAGINATION_UL_XPATH}/li[{li_index_1based}]//a"
    try:
        link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
            link,
        )
        time.sleep(0.25)
        try:
            link.click()
        except Exception:
            driver.execute_script("arguments[0].click();", link)
        if log_fn:
            log_fn(
                f"clicked pagination li[{li_index_1based}], "
                f"waiting {PAGINATION_CLICK_WAIT_SEC:.0f}s for page load…"
            )
        time.sleep(PAGINATION_CLICK_WAIT_SEC)
        try:
            _wait_for_result_cards(driver, timeout=LIST_WAIT_SEC)
        except Exception:
            # List may still be usable after wait
            pass
        return True
    except Exception as exc:
        if log_fn:
            log_fn(f"pagination click li[{li_index_1based}] failed: {exc}")
        return False


def _scrape_cost_retail_across_pages(
    driver,
    card_match_sku: str,
    *,
    log_fn: Optional[Callable[[str, str], None]] = None,
    sheet_reference: str = "",
) -> Tuple[Optional[str], Optional[str], str]:
    """Current results page, then pagination pages until SKU match or pages exhausted."""
    sheet_ref = str(sheet_reference or "").strip()

    def log(msg: str) -> None:
        if log_fn:
            log_fn(sheet_ref or card_match_sku, msg)

    cost_raw, retail_raw, status = _scrape_cost_retail_from_result_cards(
        driver,
        card_match_sku,
        log_fn=log_fn,
        sheet_reference=sheet_ref,
    )
    if cost_raw or retail_raw:
        return cost_raw, retail_raw, status
    if status not in ("sku_not_found", "no_results"):
        return cost_raw, retail_raw, status

    page_targets = _pagination_li_click_targets(driver)
    if not page_targets:
        log("no pagination links (single page of results)")
        return cost_raw, retail_raw, status

    log(
        f"SKU not on page 1 — clicking pagination for "
        f"{min(len(page_targets), MAX_PAGINATION_PAGES)} more page(s): "
        + ", ".join(f"li[{idx}]={label!r}" for idx, label in page_targets[:MAX_PAGINATION_PAGES])
    )

    for li_idx, label in page_targets[:MAX_PAGINATION_PAGES]:
        clicked = _click_pagination_li(
            driver,
            li_idx,
            log_fn=lambda m: log(m),
        )
        if not clicked:
            log(f"skip page {label!r} (li[{li_idx}] click failed)")
            continue
        log(f"scanning results after pagination li[{li_idx}] (page {label!r})")
        cost_raw, retail_raw, status = _scrape_cost_retail_from_result_cards(
            driver,
            card_match_sku,
            log_fn=log_fn,
            sheet_reference=sheet_ref,
        )
        if cost_raw or retail_raw:
            log(f"found on pagination li[{li_idx}] page {label!r}")
            return cost_raw, retail_raw, status
        if status not in ("sku_not_found", "no_results"):
            return cost_raw, retail_raw, status

    return None, None, "exact_sku_not_found" if status == "sku_not_found" else status


def _wait_for_result_cards(driver, timeout: float = LIST_WAIT_SEC) -> List:
    """Wait for search-result product ``li`` elements."""
    end = time.time() + timeout
    last_err = ""
    while time.time() < end:
        for xp in RESULT_LIST_UL_XPATHS:
            try:
                ul = driver.find_element(By.XPATH, xp)
                if ul.is_displayed():
                    cards = ul.find_elements(By.TAG_NAME, "li")
                    if cards:
                        return cards
            except Exception as e:
                last_err = str(e).split("\n", 1)[0][:120]
        try:
            cards = driver.find_elements(By.XPATH, RESULT_LI_XPATH)
            if cards:
                return cards
        except Exception as e:
            last_err = str(e).split("\n", 1)[0][:120]
        time.sleep(0.5)
    raise TimeoutException(last_err or "product list not visible")


def _extract_price_from_product_page(driver) -> Optional[str]:
    """PDP fallback — single displayed price if card scrape incomplete."""
    for xp in [
        '//*[@id="our_price_display"]',
        '//span[@itemprop="price"]',
        '//div[contains(@class,"content_prices")]//span[contains(@class,"price")]',
        '//span[contains(@class,"price")]',
    ]:
        try:
            el = driver.find_element(By.XPATH, xp)
            if el.is_displayed():
                t = el.text.strip()
                if _valid_price_text(t):
                    return t
        except Exception:
            continue
    return None


def _scrape_cost_retail_from_result_cards(
    driver,
    card_match_sku: str,
    *,
    log_fn: Optional[Callable[[str, str], None]] = None,
    sheet_reference: str = "",
) -> Tuple[Optional[str], Optional[str], str]:
    """Returns (cost_raw, retail_raw, status)."""
    target = clean_distribution_sku(card_match_sku)
    if not target:
        return None, None, "empty_sku"
    sheet_ref = str(sheet_reference or "").strip()

    def log(msg: str) -> None:
        if log_fn:
            log_fn(sheet_ref or target, msg)

    try:
        product_cards = _wait_for_result_cards(driver)
    except Exception as e:
        log(f"no product list: {str(e).split(chr(10), 1)[0][:160]}")
        return None, None, "no_results"

    if not product_cards:
        return None, None, "no_results"

    log(f"found {len(product_cards)} cards, match {target!r}")

    for idx, card in enumerate(product_cards, start=1):
        card_sku = _extract_card_sku(card)
        if not card_sku or not _card_sku_matches(card_sku, target):
            continue

        log(f"exact SKU on li[{idx}] sku={card_sku!r}")

        cost_raw, retail_raw = _extract_cost_retail_by_li_index(driver, idx)
        if not cost_raw and not retail_raw:
            cost_raw, retail_raw = _extract_cost_retail_from_card(card)

        if cost_raw or retail_raw:
            log(f"cost={cost_raw or '(none)'} retail={retail_raw or '(none)'}")
            return cost_raw, retail_raw, "ok_list_price"

        if _open_product_from_card(driver, card):
            pdp = _extract_price_from_product_page(driver)
            if pdp:
                log(f"PDP single price (retail fallback): {pdp}")
                return None, pdp, "ok_pdp_price"
            return None, None, "pdp_price_not_found"

        return None, None, "price_not_found"

    return None, None, "sku_not_found"


def scrape_distribution_cost_and_retail(
    driver,
    sheet_reference: str,
    *,
    attributes: Optional[Dict[str, str]] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Search distribution; return (cost_raw, retail_raw, status) from matched card.

    cost → sheet «cost price» (+15). retail → sheet «price» (×1.21+15) unless motorsport fallback.
    """
    sheet_ref = str(sheet_reference or "").strip()
    if not sheet_ref:
        return None, None, "empty_sku"

    def log(msg: str) -> None:
        if log_fn:
            log_fn(sheet_ref, msg)

    plans = distribution_scrape_plans(sheet_ref, attributes)
    last_status = "sku_not_found"
    # With colour-fallback plans, one no-results wait is enough — then try next colour.
    no_results_retries = 1 if len(plans) > 1 else NO_RESULTS_RETRIES

    for search_key, card_match in plans:
        log(f"search={search_key!r} card_match={card_match!r}")
        try:
            search = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//*[@id="search_query_top"]'))
            )
            search.clear()
            time.sleep(0.2)
            search.send_keys(search_key)
            search.send_keys(Keys.ENTER)
            time.sleep(SEARCH_POST_SUBMIT_WAIT_SEC)
        except Exception as e:
            log(f"distribution search error: {e}")
            return None, None, "search_failed"

        cost_raw = retail_raw = None
        status = "no_results"
        for attempt in range(1, no_results_retries + 1):
            if attempt > 1:
                log(f"no_results retry {attempt}/{no_results_retries} (wait for list)…")
                time.sleep(2.0)
            cost_raw, retail_raw, status = _scrape_cost_retail_across_pages(
                driver,
                card_match,
                log_fn=log_fn,
                sheet_reference=sheet_ref,
            )
            if cost_raw or retail_raw:
                return cost_raw, retail_raw, status
            if status != "no_results":
                break
        last_status = status
        if status in ("price_not_found", "pdp_price_not_found"):
            return None, None, status

    if last_status == "sku_not_found":
        return None, None, "exact_sku_not_found"
    return None, None, last_status


def scrape_distribution_raw_price(
    driver,
    sheet_reference: str,
    *,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> Tuple[Optional[str], str]:
    """Backward compatible: returns retail raw only (or cost if retail missing)."""
    cost_raw, retail_raw, status = scrape_distribution_cost_and_retail(
        driver, sheet_reference, log_fn=log_fn
    )
    if retail_raw:
        return retail_raw, status
    if cost_raw:
        return cost_raw, status
    return None, status
