import time
import re
import os
import sys
import random
import itertools
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def safe_gs_call(func, *args, max_retries=6, base_delay=1.25, **kwargs):
    """Google Sheets API call with exponential backoff for quota/rate-limit errors."""
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
            print(f"[WARN] Google API quota/rate limit hit. Retrying in {sleep_s:.1f}s (attempt {attempt+1}/{max_retries})")
            time.sleep(sleep_s)
        except Exception as e:
            msg = str(e).lower()
            if ("quota" in msg) or ("429" in msg) or ("rate" in msg and "limit" in msg):
                if attempt >= max_retries - 1:
                    raise
                sleep_s = (base_delay * (2**attempt)) + random.uniform(0.0, 0.6)
                print(f"[WARN] Google API quota/rate limit hit. Retrying in {sleep_s:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(sleep_s)
                continue
            raise

# ============================================
# GOOGLE SHEETS CONFIGURATION
# ============================================
SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
SHEET_NAME_PRODUCTS = "products"
SHEET_NAME_OPTIONS = "options"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")

try:
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    products_sheet = spreadsheet.worksheet(SHEET_NAME_PRODUCTS)
    options_sheet = spreadsheet.worksheet(SHEET_NAME_OPTIONS)
    print("[OK] Connected to Google Sheets successfully")
except Exception as e:
    print(f"[ERROR] Error connecting to Google Sheets: {e}")
    print("Please make sure you have the credentials.json file and the correct spreadsheet ID")
    exit(1)

# ============================================
# URLS TO SCRAPE
# All 75 brands from https://www.all-stars-motorsport.com/en/manufacturers
# ============================================
URLS_TO_SCRAPE = [
    "https://www.all-stars-motorsport.com/en/105_034-motorsport",
    "https://www.all-stars-motorsport.com/en/120_acexxon",
    "https://www.all-stars-motorsport.com/en/153_acl-performance",
    "https://www.all-stars-motorsport.com/en/159_aem",
    "https://www.all-stars-motorsport.com/en/91_airtec",
    "https://www.all-stars-motorsport.com/en/69_akrapovic",
    "https://www.all-stars-motorsport.com/en/76_alpha-competition",
    "https://www.all-stars-motorsport.com/en/189_ame-wheels",
    "https://www.all-stars-motorsport.com/en/115_ams-performance",
    "https://www.all-stars-motorsport.com/en/171_apexi",
    "https://www.all-stars-motorsport.com/en/116_apr",
    "https://www.all-stars-motorsport.com/en/126_armaspeed",
    "https://www.all-stars-motorsport.com/en/177_armytrix",
    "https://www.all-stars-motorsport.com/en/183_arp",
    "https://www.all-stars-motorsport.com/en/190_athena",
    "https://www.all-stars-motorsport.com/en/72_autopolar",
    "https://www.all-stars-motorsport.com/en/85_autotech",
    "https://www.all-stars-motorsport.com/en/174_boost-logic",
    "https://www.all-stars-motorsport.com/en/133_by-all-stars",
    "https://www.all-stars-motorsport.com/en/188_carroll-shelby",
    "https://www.all-stars-motorsport.com/en/185_csf",
    "https://www.all-stars-motorsport.com/en/67_cts-turbo",
    "https://www.all-stars-motorsport.com/en/139_dba-brakes",
    "https://www.all-stars-motorsport.com/en/111_deatschwerks",
    "https://www.all-stars-motorsport.com/en/144_dinan",
    "https://www.all-stars-motorsport.com/en/127_dixcel",
    "https://www.all-stars-motorsport.com/en/196_ebc",
    "https://www.all-stars-motorsport.com/en/142_eibach",
    "https://www.all-stars-motorsport.com/en/182_enkei",
    "https://www.all-stars-motorsport.com/en/88_eventuri",
    "https://www.all-stars-motorsport.com/en/114_ferrea-racing",
    "https://www.all-stars-motorsport.com/en/19_forge-motorsport",
    "https://www.all-stars-motorsport.com/en/109_goodridge",
    "https://www.all-stars-motorsport.com/en/140_gruppe-m",
    "https://www.all-stars-motorsport.com/en/155_hks",
    "https://www.all-stars-motorsport.com/en/135_injector-dynamics",
    "https://www.all-stars-motorsport.com/en/27_injen-technology",
    "https://www.all-stars-motorsport.com/en/187_iroz-motorsport",
    "https://www.all-stars-motorsport.com/en/110_itg",
    "https://www.all-stars-motorsport.com/en/184_js-racing",
    "https://www.all-stars-motorsport.com/en/123_je-pistons",
    "https://www.all-stars-motorsport.com/en/121_k1-technologies",
    "https://www.all-stars-motorsport.com/en/192_kinetix-racing",
    "https://www.all-stars-motorsport.com/en/166_manley",
    "https://www.all-stars-motorsport.com/en/47_mishimoto",
    "https://www.all-stars-motorsport.com/en/162_msd",
    "https://www.all-stars-motorsport.com/en/143_motul",
    "https://www.all-stars-motorsport.com/en/104_ngk",
    "https://www.all-stars-motorsport.com/en/89_oem-parts",
    "https://www.all-stars-motorsport.com/en/138_okada-projects",
    "https://www.all-stars-motorsport.com/en/86_p3-gauges",
    "https://www.all-stars-motorsport.com/en/175_pipercross",
    "https://www.all-stars-motorsport.com/en/193_powerflex",
    "https://www.all-stars-motorsport.com/en/164_pracworks",
    "https://www.all-stars-motorsport.com/en/51_prosport",
    "https://www.all-stars-motorsport.com/en/65_ramair",
    "https://www.all-stars-motorsport.com/en/78_racingline",
    "https://www.all-stars-motorsport.com/en/173_rays",
    "https://www.all-stars-motorsport.com/en/93_sachs-performance",
    "https://www.all-stars-motorsport.com/en/102_scorpion",
    "https://www.all-stars-motorsport.com/en/186_seibon",
    "https://www.all-stars-motorsport.com/en/152_ssr-wheels",
    "https://www.all-stars-motorsport.com/en/151_tanabe",
    "https://www.all-stars-motorsport.com/en/197_tarox",
    "https://www.all-stars-motorsport.com/en/191_techart",
    "https://www.all-stars-motorsport.com/en/161_tein",
    "https://www.all-stars-motorsport.com/en/68_the-turbo-engineers",
    "https://www.all-stars-motorsport.com/en/130_turbosmart",
    "https://www.all-stars-motorsport.com/en/141_ultra-racing",
    "https://www.all-stars-motorsport.com/en/136_vht",
    "https://www.all-stars-motorsport.com/en/99_vmaxx",
    "https://www.all-stars-motorsport.com/en/103_whiteline",
    "https://www.all-stars-motorsport.com/en/131_wiseco",
    "https://www.all-stars-motorsport.com/en/198_xtreme-clutch",
    "https://www.all-stars-motorsport.com/en/122_zrp",
]

# ============================================
# AVAILABILITY XPATHS (as used in the second script)
# ============================================
availability_xpaths = [
    '//*[@id="center_column"]/div/div[1]/div/div/div[2]/div[2]/div[2]/div[1]/p',
    '//p[contains(@class, "availability")]',
    '//span[@id="availability_value"]',
    '//div[@id="availability_statut"]//span',
]

# ============================================
# HELPER: EXTRACT BRAND1 FROM URL
# ============================================
def extract_brand1_from_url(url):
    try:
        last_part = url.rstrip("/").split("/")[-1]
        parts = last_part.split("_", 1)
        if len(parts) < 2:
            return ""
        brand = parts[1].replace("-", " ").replace("_", " ")
        return " ".join(word.title() for word in brand.split())
    except:
        return ""

# ============================================
# BASE SHEET HEADERS
# ============================================
BASE_PRODUCT_HEADERS = [
    "product_id", "Title", "Reference", "EC_Approved", "Availability", "Availability_1",
    "Price", "Description", "Brand1"
]

STANDARD_ATTRIBUTES = [
    "att_Color", "att_Option", "att_Thickness", "att_Wastegate", "att_any-other-attributes"
]

OPTIONS_HEADERS = ["product_id", "Brand", "Model", "Type", "Version"]

all_attribute_columns = STANDARD_ATTRIBUTES.copy()

# ============================================
# CLEANING FUNCTIONS
# ============================================
def clean_price(price_str):
    if not price_str:
        return ""
    return re.sub(r"[^0-9,\.]", "", price_str).strip()

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
        return "5"
    return "6"

def clean_availability_1(value: str) -> str:
    return clean_availability(value)

# ============================================
# ROBUST AVAILABILITY FETCH (polling + fallback)
# ============================================
def wait_for_availability_text(driver, max_wait=20, retry_interval=2):
    """Wait for availability text by polling. Returns text or empty string."""
    start_time = time.time()
    attempt = 0
    while time.time() - start_time < max_wait:
        attempt += 1
        for xpath in availability_xpaths:
            try:
                elem = driver.find_element(By.XPATH, xpath)
                if elem.is_displayed():
                    text = elem.text.strip()
                    if text:
                        return text
            except:
                pass
        # Fallback: scan body text
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            lines = body_text.split('\n')
            for line in lines:
                lower_line = line.lower()
                if any(k in lower_line for k in ["in stock", "en stock", "out of stock", "days"]):
                    return line.strip()
        except:
            pass
        print(f"      Availability not yet visible, retrying in {retry_interval}s (attempt {attempt})...")
        time.sleep(retry_interval)
    # Final attempt: any element with stock keywords
    try:
        stock_elements = driver.find_elements(By.XPATH, "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'in stock') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'en stock') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'out of stock') or contains(text(),'days')]")
        for elem in stock_elements:
            text = elem.text.strip()
            if text:
                return text
    except:
        pass
    return ""

# ============================================
# OTHER HELPERS
# ============================================
def safe_get_text(driver, xpath, wait, timeout=10):
    try:
        elem = wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
        driver.execute_script("arguments[0].scrollIntoView(true);", elem)
        return elem.text.strip()
    except:
        return ""

# ============================================
# IMPROVED COMPATIBILITY SCRAPER (from second script)
# ============================================
def get_compatibilities(driver, wait):
    """Scrape compatibility table (after expanding if needed)."""
    compat_rows = []
    try:
        # Click "Show More" if present
        try:
            show_more = driver.find_element(By.XPATH, '//*[@id="showMoreCompat"]')
            driver.execute_script("arguments[0].click();", show_more)
            time.sleep(1)
        except:
            pass

        # Locate the table body (using the exact XPath from the second script)
        tbody = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//*[@id="ukoocompat_tabcontent"]/table/tbody')
        ))
        rows = tbody.find_elements(By.TAG_NAME, "tr")
        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) >= 4:
                compat_rows.append({
                    "Brand": cols[0].text.strip(),
                    "Model": cols[1].text.strip(),
                    "Type": cols[2].text.strip(),
                    "Version": cols[3].text.strip()
                })
    except Exception as e:
        # Optional: print(e) if you want to debug
        pass
    return compat_rows

def select_option_by_value(driver, wait, fieldset_index, value):
    select_xpath = f'//*[@id="attributes"]/fieldset[{fieldset_index}]//select'
    try:
        select = wait.until(EC.element_to_be_clickable((By.XPATH, select_xpath)))
    except:
        return False

    try:
        select.click()
    except:
        try:
            driver.execute_script("arguments[0].click();", select)
        except:
            pass
    time.sleep(0.2)

    option_elem = None
    try:
        options = select.find_elements(By.TAG_NAME, "option")
        for o in options:
            if o.get_attribute("value") == value:
                option_elem = o
                break
    except:
        option_elem = None

    if not option_elem:
        return False

    try:
        option_elem.click()
    except:
        try:
            driver.execute_script("arguments[0].click();", option_elem)
        except:
            pass
    try:
        driver.execute_script("arguments[0].selected = true;", option_elem)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", select)
    except:
        pass
    time.sleep(0.2)
    return True

def create_attribute_column_name(label):
    clean_label = re.sub(r'[^a-zA-Z0-9\s]', '', label).strip()
    return f"att_{clean_label.replace(' ', '_')}"

def map_variations_to_attributes(var_data):
    global all_attribute_columns
    mapped = {col: "" for col in all_attribute_columns}
    for label, val in var_data.items():
        l = label.lower()
        column_assigned = False
        if "color" in l:
            mapped["att_Color"] = val
            column_assigned = True
        elif "option" in l:
            mapped["att_Option"] = val
            column_assigned = True
        elif "thickness" in l:
            mapped["att_Thickness"] = val
            column_assigned = True
        elif "wastegate" in l or "version" in l:
            mapped["att_Wastegate"] = val
            column_assigned = True
        if not column_assigned:
            new_col = create_attribute_column_name(label)
            if new_col not in all_attribute_columns:
                all_attribute_columns.append(new_col)
                mapped[new_col] = ""
            mapped[new_col] = val
    return mapped

# ============================================
# GOOGLE SHEETS FUNCTIONS (with backoff)
# ============================================
def get_existing_data(sheet, headers):
    try:
        existing_data = safe_gs_call(sheet.get_all_records)
        existing_tuples = []
        for row in existing_data:
            row_tuple = tuple(str(row.get(header, "")) for header in headers)
            existing_tuples.append(row_tuple)
        return existing_tuples
    except Exception as e:
        print(f"Error reading existing data: {e}")
        return []

def ensure_sheet_headers():
    try:
        current_headers = safe_gs_call(products_sheet.row_values, 1)
        if not current_headers:
            current_headers = BASE_PRODUCT_HEADERS + all_attribute_columns
            safe_gs_call(products_sheet.append_row, current_headers)
            print(f"Initialized sheet headers: {current_headers}")
            return current_headers

        all_headers = BASE_PRODUCT_HEADERS + all_attribute_columns
        headers_changed = False
        for header in all_headers:
            if header not in current_headers:
                current_headers.append(header)
                headers_changed = True
        if headers_changed:
            all_data = safe_gs_call(products_sheet.get_all_values)
            if len(all_data) > 0:
                all_data[0] = current_headers
                safe_gs_call(products_sheet.clear)
                safe_gs_call(products_sheet.append_rows, all_data)
            else:
                safe_gs_call(products_sheet.append_row, current_headers)
            print(f"Updated sheet headers: {current_headers}")
        return current_headers
    except Exception as e:
        print(f"Error ensuring sheet headers: {e}")
        return BASE_PRODUCT_HEADERS + all_attribute_columns

def write_to_google_sheets(products_data, options_data, first_write=False):
    global all_attribute_columns
    try:
        current_headers = ensure_sheet_headers()
        existing_products = get_existing_data(products_sheet, current_headers)
        existing_options = get_existing_data(options_sheet, OPTIONS_HEADERS)

        existing_pairs = set()
        try:
            product_id_col = "product_id" if "product_id" in current_headers else None
            reference_col = "Reference" if "Reference" in current_headers else None
            if product_id_col and reference_col:
                for row in safe_gs_call(products_sheet.get_all_records):
                    pid = str(row.get(product_id_col, "")).strip()
                    ref = str(row.get(reference_col, "")).strip()
                    if pid and ref:
                        existing_pairs.add((pid, ref))
        except Exception as e:
            print(f"Error reading existing product_id/Reference pairs: {e}")

        if products_data:
            products_rows = []
            for product in products_data:
                pid = str(product.get("product_id", "")).strip()
                ref = str(product.get("Reference", "")).strip()
                if pid and ref and (pid, ref) in existing_pairs:
                    print(f"Duplicate product skipped (product_id + Reference): {pid} / {ref}")
                    continue
                row = [product.get(header, "") for header in current_headers]
                product_tuple = tuple(str(v) for v in row)
                if product_tuple not in existing_products:
                    products_rows.append(row)
                    existing_products.append(product_tuple)
                    if pid and ref:
                        existing_pairs.add((pid, ref))
                    print(f"Added product: {product.get('product_id', '')} - {product.get('Title', '')}")
                else:
                    print(f"Duplicate product skipped: {product.get('product_id', '')} - {product.get('Title', '')}")
            if products_rows:
                safe_gs_call(products_sheet.append_rows, products_rows, value_input_option="USER_ENTERED")
                print(f"Added {len(products_rows)} product rows to products sheet")

        if options_data:
            options_rows = []
            for option in options_data:
                option_tuple = tuple(str(option.get(h, "")) for h in OPTIONS_HEADERS)
                if option_tuple not in existing_options:
                    row = [option.get(h, "") for h in OPTIONS_HEADERS]
                    options_rows.append(row)
                    existing_options.append(option_tuple)
                else:
                    print(f"Duplicate option skipped: {option.get('product_id', '')} - {option.get('Brand', '')} {option.get('Model', '')}")
            if options_rows:
                safe_gs_call(options_sheet.append_rows, options_rows, value_input_option="USER_ENTERED")
                print(f"Added {len(options_rows)} option rows to options sheet")
    except Exception as e:
        print(f"Error writing to Google Sheets: {e}")

# ============================================
# MAIN SCRAPER
# ============================================
def scrape_all_products():
    first_write = False
    pending_products = []
    pending_options = []
    flush_batch_size = 50
    for base_url in URLS_TO_SCRAPE:
        BRAND1_VALUE = extract_brand1_from_url(base_url)
        print(f"\nScraping URL: {base_url}")
        print(f"Brand1: {BRAND1_VALUE}")

        products_written_for_url = 0
        options_written_for_url = 0

        driver = Driver(uc=True, incognito=True, headless=False)
        driver.maximize_window()
        driver.get(base_url)
        wait = WebDriverWait(driver, 10)

        page = 1
        while True:
            print(f"Scraping page {page}...")
            try:
                product_container = wait.until(
                    EC.presence_of_element_located((By.XPATH, '//*[@id="center_column"]/ul'))
                )
            except:
                print("No products found.")
                break

            products = product_container.find_elements(By.TAG_NAME, "li")

            for product in products:
                try:
                    href = None
                    for l in product.find_elements(By.TAG_NAME, "a"):
                        h = l.get_attribute("href")
                        # Removed the brand filter - now picks up any product link
                        if h and "/en/" in h:
                            href = h
                            break
                    if not href:
                        continue

                    print(f"Scraping: {href}")
                    driver.execute_script(f"window.open('{href}', '_blank');")
                    driver.switch_to.window(driver.window_handles[-1])

                    try:
                        wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="product_name"]')))
                        product_id = ""
                        try:
                            product_id = href.split("/en/")[1].split("/")[1].split("-")[0]
                        except:
                            product_id = ""

                        # Collect variations, skipping placeholder options
                        variations_meta = []
                        try:
                            fieldsets = driver.find_elements(By.XPATH, '//*[@id="attributes"]/fieldset')
                            for idx, fs in enumerate(fieldsets, start=1):
                                label = fs.find_element(By.TAG_NAME, "label").text.strip()
                                select = fs.find_element(By.TAG_NAME, "select")
                                options = select.find_elements(By.TAG_NAME, "option")
                                valid_options = []
                                for opt in options:
                                    val = opt.get_attribute("value")
                                    text = opt.text.strip()
                                    if val == "" or "select" in text.lower():
                                        continue
                                    valid_options.append({"value": val, "text": text})
                                if label and valid_options:
                                    variations_meta.append((label, idx, valid_options))
                        except Exception as e:
                            print(f"Error extracting variations: {e}")

                        description = safe_get_text(driver, '//*[@id="wmfadedesc"]/div/div[3]/div[1]', wait)

                        # Handle variations
                        if variations_meta:
                            all_option_lists = [v[2] for v in variations_meta]
                            for combo in itertools.product(*all_option_lists):
                                for (label, fieldset_idx, _), chosen_option in zip(variations_meta, combo):
                                    select_option_by_value(driver, wait, fieldset_idx, chosen_option["value"])
                                time.sleep(2)

                                title = safe_get_text(driver, '//*[@id="product_name"]', wait)
                                reference = safe_get_text(driver, '//*[@id="product_reference"]/span', wait)
                                ec_approved = safe_get_text(driver, '//*[@id="wmhomolog_no"]', wait)

                                raw_av = wait_for_availability_text(driver)
                                availability = clean_availability(raw_av)
                                availability_1 = clean_availability_1(raw_av)

                                price = safe_get_text(driver, '//*[@id="our_price_display"]', wait)
                                price = clean_price(price)

                                compat_rows = get_compatibilities(driver, wait)

                                var_data = {label: opt["text"] for (label, _, _), opt in zip(variations_meta, combo)}
                                mapped = map_variations_to_attributes(var_data)

                                product_row = {
                                    "product_id": product_id,
                                    "Title": title,
                                    "Reference": reference,
                                    "EC_Approved": ec_approved,
                                    "Availability": availability,
                                    "Availability_1": availability_1,
                                    "Price": price,
                                    "Description": description,
                                    "Brand1": BRAND1_VALUE,
                                    **mapped
                                }
                                pending_products.append(product_row)
                                products_written_for_url += 1

                                formatted_options = []
                                for c in compat_rows:
                                    option_row = {
                                        "product_id": product_id,
                                        "Brand": c["Brand"],
                                        "Model": c["Model"],
                                        "Type": c["Type"],
                                        "Version": c["Version"]
                                    }
                                    formatted_options.append(option_row)
                                    pending_options.append(option_row)
                                    options_written_for_url += 1

                                if len(pending_products) >= flush_batch_size or len(pending_options) >= (flush_batch_size * 3):
                                    write_to_google_sheets(pending_products, pending_options, first_write)
                                    first_write = False
                                    pending_products = []
                                    pending_options = []

                        else:
                            # No variations
                            title = safe_get_text(driver, '//*[@id="product_name"]', wait)
                            reference = safe_get_text(driver, '//*[@id="product_reference"]/span', wait)
                            ec_approved = safe_get_text(driver, '//*[@id="wmhomolog_no"]', wait)

                            raw_av = wait_for_availability_text(driver)
                            availability = clean_availability(raw_av)
                            availability_1 = clean_availability_1(raw_av)

                            price = safe_get_text(driver, '//*[@id="our_price_display"]', wait)
                            price = clean_price(price)

                            compat_rows = get_compatibilities(driver, wait)

                            product_row = {
                                "product_id": product_id,
                                "Title": title,
                                "Reference": reference,
                                "EC_Approved": ec_approved,
                                "Availability": availability,
                                "Availability_1": availability_1,
                                "Price": price,
                                "Description": description,
                                "Brand1": BRAND1_VALUE
                            }
                            for attr_col in all_attribute_columns:
                                product_row[attr_col] = ""

                            pending_products.append(product_row)
                            products_written_for_url += 1

                            formatted_options = []
                            for c in compat_rows:
                                option_row = {
                                    "product_id": product_id,
                                    "Brand": c["Brand"],
                                    "Model": c["Model"],
                                    "Type": c["Type"],
                                    "Version": c["Version"]
                                }
                                formatted_options.append(option_row)
                                pending_options.append(option_row)
                                options_written_for_url += 1

                            if len(pending_products) >= flush_batch_size or len(pending_options) >= (flush_batch_size * 3):
                                write_to_google_sheets(pending_products, pending_options, first_write)
                                first_write = False
                                pending_products = []
                                pending_options = []

                    finally:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                except Exception as e:
                    print(f"Error: {e}")

            # Pagination
            try:
                pagination = driver.find_element(By.XPATH, '//*[@id="pagination_bottom"]/ul')
                pages = pagination.find_elements(By.TAG_NAME, "li")
                next_page_found = False
                for li in pages:
                    try:
                        a = li.find_element(By.TAG_NAME, "a")
                        txt = a.text.strip()
                        if txt.isdigit() and int(txt) == page + 1:
                            print(f"Switching to page {page + 1}...")
                            driver.execute_script("arguments[0].click();", a)
                            time.sleep(3)
                            page += 1
                            next_page_found = True
                            break
                    except:
                        continue
                if not next_page_found:
                    print("No more pages.")
                    break
            except:
                print("No pagination found.")
                break

        # Final upload for this URL
        if pending_products or pending_options:
            write_to_google_sheets(pending_products, pending_options, first_write)
            first_write = False
            pending_products = []
            pending_options = []

        print(f"Finished scraping {base_url}. Added {products_written_for_url} products and {options_written_for_url} options to Google Sheets")

    print("\nScraping complete! All URLs processed.")

if __name__ == "__main__":
    scrape_all_products()