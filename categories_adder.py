import os
import sys
import json
import gspread
import time
import random
import argparse
from collections import defaultdict
from openai import OpenAI, RateLimitError, APIConnectionError, APIError
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from gspread.exceptions import APIError as GSpreadAPIError

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================
# CONFIGURATION
# ============================================

# Google Sheets configuration
DEST_SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
SHEET_NAME_PRODUCTS = "products"

# OpenAI API Configuration
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print("[ERROR] OPENAI_API_KEY environment variable is not set")
    print("[INFO] Set it with: export OPENAI_API_KEY='your-api-key-here'")
    exit(1)

OPENAI_MODEL = "gpt-4o"

# API Retry configuration
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 3

# HIERARCHICAL CATEGORY STRUCTURE
CATEGORY_HIERARCHY = {
    "Chassis": {
        "Chassis Versteviging": [],
        "Control / Stuurinrichting": [],
        "Steunen / Bussen": [],
        "Vering": []
    },
    "Exterieur": {
        "Exterieur Accessoires": [],
        "Skirts & Diffusers": [],
        "Stickers": []
    },
    "Interieur": {
        "Interieur Accessoires": [],
        "Meter Accessoires": [],
        "Meters": []
    },
    "Motor": {
        "Brandstof": [],
        "Inlaat": [],
        "Intercoolers en toebehoren / koeling": [],
        "Interne delen": [],
        "Motor Accessoires": [],
        "Obd tuning": [],
        "Olie Systeem": [],
        "Ontsteking en meer": [],
        "Turbo en Toebehoren": [],
        "Uitlaat": []
    },
    "Power kit": {
        "Stage 2": [],
        "Stage 2+": [],
        "Stage 3": [],
        "Stage 4": []
    },
    "Remmen / Wielen": {
        "Remmen": [],
        "Wiel Accessoires": [],
        "Wielen": []
    },
    "Transmissie": {
        "Differentieel": [],
        "Koppeling": [],
        "Versnellingsbak": []
    },
    "Diversen": {
        "": []  # Geen subcategorie voor Diversen
    }
}

# Create flat list of all categories and subcategories
ALL_CATEGORIES = list(CATEGORY_HIERARCHY.keys())
ALL_SUBCATEGORIES = []
for main_cat, subcats in CATEGORY_HIERARCHY.items():
    for subcat in subcats.keys():
        if subcat:  # Skip empty subcategory (Diversen)
            ALL_SUBCATEGORIES.append(subcat)

# List of brand names that should NOT be used as categories
BRAND_NAMES_TO_EXCLUDE = [
    "CTS TURBO", "Akrapovic", "Airtec", "Armaspeed", "Alpha Competition", 
    "034 Motorsport", "Endura Motorsport", "Forge Motorsport", "Gruppe M", 
    "Injen", "Integrated Engineering", "Mishimoto", "MST Performance", 
    "Precision Raceworks", "Racingline", "Ramair", "Scorpion", "Snow Performance", 
    "Turbo Systems", "Wagner Tuning", "Eventuri"
]

# Set up Google Sheets authentication
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Path to your service account JSON key file
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")

# Initialize Google Sheets client
try:
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    print("[OK] Connected to Google Sheets API successfully")
except Exception as e:
    print(f"[ERROR] Error connecting to Google Sheets: {e}")
    exit(1)

def safe_gs_call(func, *args, max_retries=6, base_delay=1.25, **kwargs):
    """Google Sheets API call with exponential backoff for quota/rate-limit errors."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (HttpError, GSpreadAPIError) as e:
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

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ============================================
# HELPER FUNCTIONS
# ============================================

def find_column_index(headers, column_name):
    """Find column index by name (case-insensitive)"""
    for i, header in enumerate(headers):
        if str(header).strip().lower() == column_name.lower():
            return i
    return -1

def extract_attributes_from_row(row, headers):
    """Extract all attributes (att_*) from the row"""
    attributes = {}
    
    for i, header in enumerate(headers):
        if header.startswith('att_'):
            if i < len(row):
                value = str(row[i]).strip()
                if value and value != "":
                    attr_name = header[4:].replace('_', ' ').title()
                    attributes[attr_name] = value
    
    return attributes

def format_attributes_for_chatgpt(attributes):
    """Format attributes data for ChatGPT prompt"""
    if not attributes:
        return "Geen attributen beschikbaar"
    
    formatted = ""
    for attr_name, attr_value in attributes.items():
        formatted += f"• {attr_name}: {attr_value}\n"
    
    return formatted.strip()

def generate_categories_for_product(product_id, product_title, product_description, brand, attributes, max_retries=MAX_RETRIES):
    """Generate categories for a product with automatic retry logic"""
    for attempt in range(max_retries):
        try:
            return generate_categories(product_id, product_title, product_description, brand, attributes)
        except (RateLimitError, APIConnectionError, APIError) as e:
            if attempt == max_retries - 1:
                print(f"❌ Failed after {max_retries} attempts for Product_ID {product_id}: {e}")
                return {"category": "Diversen", "subcategory": ""}
            
            wait_time = INITIAL_RETRY_DELAY * (2 ** attempt)
            print(f"⚠️  API error ({type(e).__name__}) for Product_ID {product_id}. Retry {attempt + 1}/{max_retries} in {wait_time}s...")
            time.sleep(wait_time)
        except Exception as e:
            print(f"❌ Unexpected error for Product_ID {product_id}: {e}")
            return {"category": "Diversen", "subcategory": ""}
    
    return {"category": "Diversen", "subcategory": ""}

def generate_categories(product_id, product_title, product_description, brand, attributes):
    """Generate categories using OpenAI API - ONE CALL PER PRODUCT_ID"""
    
    # Format attributes
    attributes_info = format_attributes_for_chatgpt(attributes)
    
    # System prompt - STRICT NEW PROMPT
    system_prompt = """JE TAAK
Bepaal voor elk product exact 1 hoofdcategorie en 1 subcategorie (indien van toepassing).
De keuze moet gebaseerd zijn op de technische functie van het product.
Marketingnamen, esthetiek of accessoires mogen nooit leidend zijn.

TOEGESTANE CATEGORIEËN (STRIKT)
Gebruik uitsluitend deze categorie structuur. Afwijken is niet toegestaan.

Chassis
- Chassis Versteviging
- Control / Stuurinrichting
- Steunen / Bussen
- Vering

Exterieur
- Exterieur Accessoires
- Skirts & Diffusers
- Stickers

Interieur
- Interieur Accessoires
- Meter Accessoires
- Meters

Motor
- Brandstof
- Inlaat
- Intercoolers en toebehoren / koeling
- Interne delen
- Motor Accessoires
- Obd tuning
- Olie Systeem
- Ontsteking en meer
- Turbo en Toebehoren
- Uitlaat

Power kit
- Stage 2
- Stage 2+
- Stage 3
- Stage 4

Remmen / Wielen
- Remmen
- Wiel Accessoires
- Wielen

Transmissie
- Differentieel
- Koppeling
- Versnellingsbak

Diversen
- (geen subcategorie)

HARDE REGELS (VERPLICHT)

1. INLAAT REGEL
Alles wat onderdeel is van het luchttraject richting motor of turbo moet altijd:
Motor > Inlaat

Dit geldt voor o.a.:
intake
induction
airbox
luchtfilterkast
inlet
inlet hose
turbo inlet
intake hose
intake pipe
inlet duct
air intake duct
silicone inlet hose
aluminium inlet buis

NOOIT Motor Accessoires
NOOIT Diversen

2. MOTOR ACCESSOIRES REGEL
Alleen gebruiken als het product geen invloed heeft op:
lucht, brandstof, olie, ontsteking, turbo, koeling of aandrijving.

Voorbeelden:
motorafdekking
strut top covers
afdekkappen
sierdelen motorruimte

Dan:
Motor > Motor Accessoires

3. TURBO REGEL
Alles wat direct met de turbo of boostregeling te maken heeft:
blow off valve
dump valve
diverter
actuator
wastegate
boost tap
solenoïde
turbo hardware

Dan:
Motor > Turbo en Toebehoren

4. INTERCOOLER REGEL
Alles tussen turbo en gasklep:
intercooler
charge pipes
boost pipes
intercooler slangen
intercooler kits

Dan:
Motor > Intercoolers en toebehoren / koeling

5. OLIE REGEL
Alles wat olie koelt, transporteert of regelt:
oliekoeler
sandwich plate
AN lijnen
olie thermostaat

Dan:
Motor > Olie Systeem

6. ONTSTEKING REGEL
Alles wat vonk of ontsteking regelt:
bobines
bougies
ignition upgrades

Dan:
Motor > Ontsteking en meer

7. DIVERSEN REGEL
Gebruik Diversen alleen als:
- het product geen voertuig-specifieke compatibiliteit heeft
- én geen duidelijke motor, chassis, interieur of exterieur functie heeft

Als er merk, model of bouwjaar wordt genoemd:
NOOIT Diversen

CONTROLE (VERPLICHT)
Voor elke keuze controleer:
- Beïnvloedt dit product lucht richting motor of turbo? → Motor > Inlaat
- Zit het product op of aan de turbo? → Motor > Turbo en Toebehoren
- Is het puur cosmetisch in de motorruimte? → Motor > Motor Accessoires
- Is er voertuig-specifieke fitment? → NOOIT Diversen

UITVOER FORMAAT (STRICT)
Geef exact dit terug, niets extra's:

Categorie: <hoofdcategorie>
Subcategorie: <subcategorie of leeg bij Diversen>"""
    
    user_prompt = f"""PRODUCT INFORMATIE:

Product_ID: {product_id}
Titel: {product_title}
Merk: {brand}
Beschrijving: {product_description}
Attributen:
{attributes_info}

Bepaal op basis van bovenstaande regels de juiste categorieën.

UITVOER:"""
    
    try:
        print(f"🤖 Processing Product_ID: {product_id}")
        print(f"   Titel: '{product_title[:50]}...'")
        
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,  # Strict, no creativity
            max_tokens=50      # Keep response short
        )
        
        content = response.choices[0].message.content.strip()
        print(f"   Response: {content}")
        
        # Parse the response format "Categorie: X\nSubcategorie: Y"
        lines = content.split('\n')
        category = ""
        subcategory = ""
        
        for line in lines:
            if line.startswith('Categorie:'):
                category = line.replace('Categorie:', '').strip()
            elif line.startswith('Subcategorie:'):
                subcategory = line.replace('Subcategorie:', '').strip()
        
        # Validate required fields
        if not category:
            print(f"⚠️ Missing category in response for Product_ID {product_id}")
            return {"category": "Diversen", "subcategory": ""}
        
        # Check if category is a brand name (should not be used)
        if category in BRAND_NAMES_TO_EXCLUDE:
            print(f"⚠️ Category '{category}' is a brand name, using Diversen")
            return {"category": "Diversen", "subcategory": ""}
        
        # Validate category exists
        if category not in ALL_CATEGORIES:
            print(f"⚠️ Category '{category}' invalid, using Diversen")
            return {"category": "Diversen", "subcategory": ""}
        
        # For Diversen, subcategory should be empty
        if category == "Diversen":
            subcategory = ""
        else:
            # Validate subcategory exists under category
            if subcategory not in CATEGORY_HIERARCHY[category]:
                print(f"⚠️ Subcategory '{subcategory}' not under '{category}'")
                # Try to find correct subcategory based on rules
                title_lower = product_title.lower()
                
                # Check intake rule
                intake_keywords = ["intake", "inlaat", "induction", "airbox", "luchtfilter", "inlet", "turbo inlet", "intake hose", "intake pipe", "inlet duct", "air intake"]
                if any(keyword in title_lower for keyword in intake_keywords):
                    return {"category": "Motor", "subcategory": "Inlaat"}
                
                # If not, use first subcategory as fallback
                subcats = list(CATEGORY_HIERARCHY[category].keys())
                if subcats and subcats[0]:  # Skip empty for Diversen
                    subcategory = subcats[0]
                else:
                    subcategory = ""
        
        # EXTRA FORCE CHECK: If title contains intake keywords, force to Motor > Inlaat
        title_lower = product_title.lower()
        intake_keywords = ["intake", "inlaat", "induction", "airbox", "luchtfilter", "inlet", "turbo inlet", "intake hose", "intake pipe", "inlet duct", "air intake", "silicone inlet", "aluminium inlet"]
        if any(keyword in title_lower for keyword in intake_keywords):
            if category != "Motor" or subcategory != "Inlaat":
                print(f"⚠️ FORCING intake product to Motor > Inlaat")
                return {"category": "Motor", "subcategory": "Inlaat"}
        
        print(f"✅ {category} / {subcategory if subcategory else '(geen)'}")
        return {"category": category, "subcategory": subcategory}
        
    except Exception as e:
        print(f"❌ API error for Product_ID {product_id}: {e}")
        # Fallback: check if it's an intake product
        title_lower = product_title.lower()
        intake_keywords = ["intake", "inlaat", "induction", "airbox", "luchtfilter", "inlet"]
        if any(keyword in title_lower for keyword in intake_keywords):
            return {"category": "Motor", "subcategory": "Inlaat"}
        return {"category": "Diversen", "subcategory": ""}

def ensure_columns_exist(dest_sheet):
    """Ensure Category and Subcategory columns exist at the end"""
    try:
        all_data = safe_gs_call(dest_sheet.get_all_values)
        if not all_data:
            print("❌ No data in sheet")
            return -1, -1
        
        headers = all_data[0]
        
        category_col = find_column_index(headers, 'Category')
        subcategory_col = find_column_index(headers, 'Subcategory')
        
        if category_col == -1:
            print("➕ Adding 'Category' column...")
            safe_gs_call(dest_sheet.add_cols, 1)
            category_col = len(headers)
            safe_gs_call(dest_sheet.update_cell, 1, category_col + 1, 'Category')
        
        if subcategory_col == -1:
            print("➕ Adding 'Subcategory' column...")
            safe_gs_call(dest_sheet.add_cols, 1)
            subcategory_col = len(headers) + (0 if category_col == -1 else 1)
            safe_gs_call(dest_sheet.update_cell, 1, subcategory_col + 1, 'Subcategory')
        
        time.sleep(2)
        all_data = safe_gs_call(dest_sheet.get_all_values)
        headers = all_data[0]
        
        category_col = find_column_index(headers, 'Category')
        subcategory_col = find_column_index(headers, 'Subcategory')
        
        return category_col, subcategory_col
        
    except Exception as e:
        print(f"❌ Error ensuring columns: {e}")
        return -1, -1

def update_categories_in_batch(dest_sheet, updates, category_col, subcategory_col):
    """Update categories in batch"""
    try:
        if not updates:
            return 0
        
        batch_updates = []
        
        for row_num, categories in updates:
            category_cell = gspread.utils.rowcol_to_a1(row_num, category_col + 1)
            subcategory_cell = gspread.utils.rowcol_to_a1(row_num, subcategory_col + 1)
            
            batch_updates.extend([
                {'range': category_cell, 'values': [[categories['category']]]},
                {'range': subcategory_cell, 'values': [[categories['subcategory']]]}
            ])
        
        body = {'valueInputOption': 'USER_ENTERED', 'data': batch_updates}
        req = sheets_service.spreadsheets().values().batchUpdate(spreadsheetId=DEST_SPREADSHEET_ID, body=body)
        safe_gs_call(req.execute)
        
        return len(updates)
        
    except Exception as e:
        print(f"❌ Batch update error: {e}")
        return 0

def add_category_columns_to_sheet():
    """Main function"""
    try:
        print("📊 Opening spreadsheet...")
        dest_spreadsheet = safe_gs_call(client.open_by_key, DEST_SPREADSHEET_ID)
        dest_sheet = safe_gs_call(dest_spreadsheet.worksheet, SHEET_NAME_PRODUCTS)
        
        print("\n📝 Adding columns...")
        category_col, subcategory_col = ensure_columns_exist(dest_sheet)
        
        if category_col == -1 or subcategory_col == -1:
            print("❌ Failed to setup columns")
            return False
        
        print("📥 Loading data...")
        time.sleep(3)
        all_data = safe_gs_call(dest_sheet.get_all_values)
        
        if not all_data or len(all_data) < 2:
            print("❌ No data found")
            return False
        
        headers = all_data[0]
        
        # Find columns
        product_id_idx = find_column_index(headers, 'Product_ID')
        title_idx = find_column_index(headers, 'Title')
        description_idx = find_column_index(headers, 'Description')
        brand_idx = find_column_index(headers, 'Brand1') or find_column_index(headers, 'Brand') or find_column_index(headers, 'Merk')
        
        if product_id_idx == -1 or title_idx == -1:
            print("❌ Required columns not found")
            return False
        
        # Group by Product_ID
        product_groups = defaultdict(list)
        for row_num in range(1, len(all_data)):
            row = all_data[row_num]
            while len(row) < len(headers):
                row.append('')
            
            product_id = row[product_id_idx] if product_id_idx < len(row) else ""
            if product_id and product_id.strip():
                product_groups[product_id.strip()].append((row_num + 1, row))
        
        print(f"📊 Found {len(product_groups)} unique Product_IDs")
        
        # Process products
        processed_ids = 0
        updated_rows = 0
        categories_cache = {}
        batch_updates = []
        
        print(f"\n🚀 Starting categorization...")
        
        for product_id, rows_info in product_groups.items():
            processed_ids += 1

            # Skip entire product if every row already has a category assigned
            rows_needing_update = [
                (row_num, row) for row_num, row in rows_info
                if not (row[category_col].strip() if category_col < len(row) else "")
            ]
            if not rows_needing_update:
                continue

            first_row_num, first_row = rows_info[0]
            product_title = first_row[title_idx] if title_idx < len(first_row) else ""
            product_description = first_row[description_idx] if description_idx < len(first_row) and description_idx != -1 else ""
            brand = first_row[brand_idx] if brand_idx < len(first_row) and brand_idx != -1 else ""
            
            if not product_title.strip():
                continue
            
            if product_id in categories_cache:
                categories = categories_cache[product_id]
            else:
                attributes = extract_attributes_from_row(first_row, headers)
                categories = generate_categories_for_product(
                    product_id, product_title, product_description, brand, attributes
                )
                categories_cache[product_id] = categories
            
            for row_num, row in rows_needing_update:
                batch_updates.append((row_num, categories))
                updated_rows += 1
            
            if len(batch_updates) >= 50 or processed_ids % 10 == 0:
                if batch_updates:
                    update_categories_in_batch(dest_sheet, batch_updates, category_col, subcategory_col)
                    batch_updates = []
                    time.sleep(2)
            
            if processed_ids % 10 == 0:
                print(f"   Progress: {processed_ids}/{len(product_groups)} products")
        
        if batch_updates:
            update_categories_in_batch(dest_sheet, batch_updates, category_col, subcategory_col)
        
        print(f"\n✅ Complete! Processed {processed_ids} products, updated {updated_rows} rows")
        print(f"💰 API calls: {len(categories_cache)}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--yes", action="store_true", help="Run non-interactively (auto-confirm)")
    args = parser.parse_args()

    print("="*80)
    print("🏷️  PRODUCT CATEGORIZATION TOOL - STRICT RULES")
    print("="*80)
    print("\n📋 STRICT CATEGORIZATION RULES:")
    print("   • ALL intake/air path products → Motor > Inlaat")
    print("   • Turbo components → Motor > Turbo en Toebehoren")
    print("   • Intercooler/charge pipes → Motor > Intercoolers en toebehoren / koeling")
    print("   • Cosmetic engine parts → Motor > Motor Accessoires")
    print("   • Vehicle-specific products → NEVER Diversen")
    print("="*80)
    print(f"📊 Sheet: {DEST_SPREADSHEET_ID}")
    print(f"📄 Tab: {SHEET_NAME_PRODUCTS}")
    print("="*80)
    
    try:
        is_interactive = bool(sys.stdin.isatty())
    except Exception:
        is_interactive = False
    should_apply = bool(args.yes) or (not is_interactive)
    confirm = 'y' if should_apply else input("\nStart categorization with STRICT rules? (y/n): ")
    if str(confirm).lower() != 'y':
        print("❌ Cancelled")
        return
    
    print("\n⏳ Processing...")
    success = add_category_columns_to_sheet()
    
    if success:
        print("\n🎉 Categorization complete!")
        print("✅ Check your Google Sheet")
        print(f"\n🔗 https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}")
    else:
        print("\n❌ Failed")

if __name__ == "__main__":
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"❌ Missing: {SERVICE_ACCOUNT_FILE}")
        print(f"💡 Place your Google Service Account credentials file in the script directory")
        exit(1)
    
    main()