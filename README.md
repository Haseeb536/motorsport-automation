# Motorsport Automation Toolkit

Hi! 👋  
This repo is a collection of Python tools I built to work with **All-Stars Motorsport (ASM)** and **All-Stars Distribution (ASD)** data — scraping products, syncing Google Sheets, comparing SKU lists, and keeping Shopify in step.

Everything here is meant to be **practical and scriptable**: run a command, get a sheet updated or a report exported.

---

## What you can do with this

| Area | What it helps with |
|------|-------------------|
| **Scraping** | Pull products from ASM brand pages into Google Sheets (`new.py`) |
| **Distribution** | Scrape all brand SKUs from ASD (`distribution_brand_sku_scrape.py`) |
| **Compare** | Find SKUs on ASM but not ASD (and the other way around) (`compare_asm_asd_skus.py`) |
| **Pricing** | Update cost, retail, and availability from distribution + motorsport sites |
| **Shopify** | Upload draft products and sync variant data (`Uploader.py`) |
| **Sheets** | Merge, translate options, add URLs, and other sheet utilities |

---

## Before you start

You’ll need:

1. **Python 3.10+** (3.11+ recommended)
2. **Google Cloud service account** with Sheets + Drive access  
   - Download the JSON key and save it as `credentials.json` in the project root  
   - Use `credentials.json.example` as a shape reference only — **never commit real keys**
3. **Chrome** (for Selenium / SeleniumBase scrapers)
4. Optional: **Shopify Admin API** token and store URL (for upload / price sync scripts)
5. Optional: **All-Stars Distribution** login (for price and SKU scraping)

---

## Quick setup

```bash
# 1. Clone the repo
git clone https://github.com/Haseeb536/motorsport-automation.git
cd motorsport-automation

# 2. Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure secrets (copy the example and fill in your values)
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux

# 5. Add your Google service account file
copy credentials.json.example credentials.json
# Then replace credentials.json with your real service account JSON from Google Cloud
```

Share your Google spreadsheets with the **service account email** (the `client_email` inside `credentials.json`).

Set spreadsheet IDs in `.env` — see `.env.example` for variable names.

---

## Common tasks

### Scrape ASM products (all brands)

`new.py` includes brand URLs from the ASM manufacturers page. Run:

```bash
python new.py
```

Products land in the Google Sheet configured via `GOOGLE_SPREADSHEET_ID`.

### Scrape every ASD brand SKU to Excel

```bash
python distribution_brand_sku_scrape.py --output distribution_brand_skus.xlsx
```

Test with one brand first:

```bash
python distribution_brand_sku_scrape.py --limit-brands 1
```

### Compare ASM vs ASD SKUs

After you have an ASD Excel export:

```bash
python compare_asm_asd_skus.py --asd-xlsx distribution_brand_skus.xlsx --write-csv
```

This creates two tabs on your ASM spreadsheet:

- `asm_not_in_asd` — on motorsport, not on distribution  
- `asd_not_in_asm` — on distribution, not on motorsport  

SKUs are matched **case-insensitively** (`R06-10-2` = `r06-10-2`).

### Update prices on the upload sheet (+ Shopify)

```bash
python update_prices_and_sheet.py
```

Brand-specific test scripts (dry-run by default):

```bash
python test_csf_pricing.py
python test_forge_pricing.py --apply
```

---

## Project layout (main scripts)

```
new.py                          # ASM product scraper → Google Sheets
distribution_brand_sku_scrape.py
distribution_cost_scrape.py     # ASD login + SKU/price helpers
compare_asm_asd_skus.py
update_prices_and_sheet.py
Uploader.py                     # Shopify draft upload
motorsport_site_scrape.py
motorsport_pricing.py
sku_price_overrides.py
brand_price_config.py
```

---



## Requirements

See `requirements.txt`. Main libraries: `gspread`, `seleniumbase`, `openpyxl`, `pandas`, `requests`, `google-auth`.

---

## Author

**[Haseeb536](https://github.com/Haseeb536)** — motorsport / e-commerce automation tooling.

If you use this repo, feel free to open an issue or suggest improvements. Happy scraping! 🏁
