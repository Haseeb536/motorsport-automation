import os
import sys
import time
import gspread
import requests
import re
import random
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError as GSpreadAPIError
import json

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================
# CONFIGURATION - UPDATED WITH NEW STORE AND TOKEN
# ============================================

# Shopify Configuration
SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "")  # NEW store URL
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")  # NEW access token (private app)
SHOPIFY_API_VERSION = "2024-01"

# Google Sheets configuration
UPDATED_SPREADSHEET_ID = os.environ.get("GOOGLE_UPDATED_SPREADSHEET_ID", "")
UPLOADED_SPREADSHEET_ID = os.environ.get("GOOGLE_UPLOADED_SPREADSHEET_ID", "")

SHEET_NAMES = {
    'products': 'products',
    'options': 'options'
}

# NEW: Configuration for Category/Subcategory/Tags/Collections
CATEGORY_CONFIG = {
    'category_columns': ['Category', 'Main Category', 'Type'],
    'subcategory_columns': ['Subcategory', 'Sub Category', 'Product Type'],
    'brand_columns': ['Brand1', 'Brand', 'Manufacturer'],
    'collection_columns': ['Collection', 'Product Line', 'Series']
}

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

# ============================================
# SHOPIFY AUTHENTICATION - SIMPLIFIED WITH STATIC TOKEN
# ============================================

def get_access_token():
    """Return the static access token provided."""
    return SHOPIFY_ACCESS_TOKEN

# ============================================
# CATEGORY/SUBCATEGORY/TAG/COLLECTION FUNCTIONS
# ============================================

def extract_category_info(variant, column_mapping):
    """
    Extract category, subcategory, brand, and collection information from variant
    """
    category_info = {
        'category': '',
        'subcategory': '',
        'brand': '',
        'collection': ''
    }
    
    # Extract Category
    for col_name in CATEGORY_CONFIG['category_columns']:
        if column_mapping.get(col_name, -1) != -1:
            value = variant.get(col_name, '').strip()
            if value:
                category_info['category'] = value
                break
    
    # Extract Subcategory
    for col_name in CATEGORY_CONFIG['subcategory_columns']:
        if column_mapping.get(col_name, -1) != -1:
            value = variant.get(col_name, '').strip()
            if value:
                category_info['subcategory'] = value
                break
    
    # Extract Brand
    for col_name in CATEGORY_CONFIG['brand_columns']:
        if column_mapping.get(col_name, -1) != -1:
            value = variant.get(col_name, '').strip()
            if value:
                category_info['brand'] = value
                break
    
    # Extract Collection
    for col_name in CATEGORY_CONFIG['collection_columns']:
        if column_mapping.get(col_name, -1) != -1:
            value = variant.get(col_name, '').strip()
            if value:
                category_info['collection'] = value
                break
    
    return category_info

def generate_tags_from_category_info(category_info):
    """
    Generate tags from category information
    Format: Brand, Category, Subcategory
    """
    tags = []
    
    # Add brand as tag (if exists)
    if category_info['brand']:
        brand_tag = clean_text_for_tag(category_info['brand'])
        if brand_tag and brand_tag not in tags:
            tags.append(brand_tag)
    
    # Add category as tag (if exists and different from brand)
    if category_info['category']:
        category_tag = clean_text_for_tag(category_info['category'])
        if category_tag and category_tag not in tags and category_tag != brand_tag:
            tags.append(category_tag)
    
    # Add subcategory as tag (if exists and different from others)
    if category_info['subcategory']:
        subcategory_tag = clean_text_for_tag(category_info['subcategory'])
        if subcategory_tag and subcategory_tag not in tags and subcategory_tag != brand_tag and subcategory_tag != category_tag:
            tags.append(subcategory_tag)
    
    # Add collection as tag if it's different
    if category_info['collection']:
        collection_tag = clean_text_for_tag(category_info['collection'])
        if collection_tag and collection_tag not in tags and collection_tag != brand_tag and collection_tag != category_tag and collection_tag != subcategory_tag:
            tags.append(collection_tag)
    
    return tags

def generate_collections_from_category_info(category_info):
    """
    Generate collection names from category information
    Returns a list of collection names that should be created/used
    """
    collections = []
    
    # Add brand collection (if exists)
    if category_info['brand']:
        brand_collection = clean_text_for_collection(category_info['brand'])
        if brand_collection and brand_collection not in collections:
            collections.append(brand_collection)
    
    # Add category collection (if exists)
    if category_info['category']:
        category_collection = clean_text_for_collection(category_info['category'])
        if category_collection and category_collection not in collections:
            collections.append(category_collection)
    
    # Add subcategory collection (if exists)
    if category_info['subcategory']:
        subcategory_collection = clean_text_for_collection(category_info['subcategory'])
        if subcategory_collection and subcategory_collection not in collections:
            collections.append(subcategory_collection)
    
    # Add specific collection from data (if exists)
    if category_info['collection']:
        specific_collection = clean_text_for_collection(category_info['collection'])
        if specific_collection and specific_collection not in collections:
            collections.append(specific_collection)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_collections = []
    for col in collections:
        if col not in seen:
            seen.add(col)
            unique_collections.append(col)
    
    return unique_collections

def clean_text_for_tag(text):
    """
    Clean text for use as a tag
    """
    if not text:
        return ""
    
    # Remove special characters and make lowercase
    cleaned = re.sub(r'[^\w\s-]', '', text)
    cleaned = cleaned.lower().strip()
    
    # Replace multiple spaces with single space
    cleaned = re.sub(r'\s+', ' ', cleaned)
    
    return cleaned

def clean_text_for_collection(text):
    """
    Clean text for use as a collection name
    """
    if not text:
        return ""
    
    # Capitalize first letter of each word
    cleaned = ' '.join(word.capitalize() for word in text.split())
    
    # Remove special characters but keep spaces
    cleaned = re.sub(r'[^\w\s]', '', cleaned)
    
    return cleaned.strip()

def create_or_get_collection(collection_name, access_token):
    """
    Create a collection if it doesn't exist, or get existing collection
    FIXED: Now creates PUBLISHED collections so they appear immediately
    """
    if not collection_name or not collection_name.strip():
        return None
    
    clean_name = collection_name.strip()
    print(f"   [INFO] Checking/creating collection: '{clean_name}'")
    
    # First, search for existing collection
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/custom_collections.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }
    
    params = {
        "title": clean_name,
        "limit": 10
    }
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            collections = response.json().get('custom_collections', [])
            
            # Find exact match (case-insensitive)
            for collection in collections:
                if collection.get('title', '').lower() == clean_name.lower():
                    collection_id = collection['id']
                    print(f"   [OK] Found existing collection: '{clean_name}' (ID: {collection_id})")
                    return collection_id
        
        # Collection doesn't exist, create it as PUBLISHED
        collection_data = {
            "custom_collection": {
                "title": clean_name,
                "published": True,  # CHANGED: Create as PUBLISHED, not draft
                "published_at": datetime.now().isoformat(),
                "sort_order": "manual",
                "template_suffix": "",
                "body_html": f"<p>Products from {clean_name}</p>"
            }
        }
        
        response = requests.post(url, headers=headers, json=collection_data, timeout=30)
        
        if response.status_code == 201:
            collection = response.json().get('custom_collection', {})
            collection_id = collection.get('id')
            print(f"   [OK] Created NEW collection: '{clean_name}' (ID: {collection_id})")
            return collection_id
        else:
            print(f"   [WARN] Failed to create collection '{clean_name}': {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            return None
            
    except Exception as e:
        print(f"   [ERROR] Error handling collection '{clean_name}': {str(e)[:50]}")
        return None

def add_product_to_collection(product_id, collection_id, access_token):
    """
    Add a product to a collection
    FIXED: Better error handling and logging
    """
    if not product_id or not collection_id:
        print(f"   [ERROR] Missing product_id or collection_id")
        return False
    
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/collects.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }
    
    collect_data = {
        "collect": {
            "product_id": int(product_id),
            "collection_id": int(collection_id),
            "position": 1
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=collect_data, timeout=30)
        
        if response.status_code == 201:
            print(f"   [OK] Successfully added product to collection")
            return True
        else:
            error_data = response.json()
            error_message = str(error_data)
            
            # Check if product is already in collection
            if "already exists" in error_message.lower() or response.status_code == 422:
                print(f"   [INFO] Product already in collection")
                return True
            else:
                print(f"   [WARN] Failed to add product to collection: {response.status_code}")
                print(f"   Error: {error_message[:100]}")
                return False
                
    except Exception as e:
        print(f"   [ERROR] Error adding to collection: {str(e)[:100]}")
        return False

def handle_collections_for_product(product_id, collections, access_token):
    """
    Handle all collections for a product
    FIXED: Better logging and error handling
    """
    if not collections or not isinstance(collections, list):
        print(f"   [INFO] No collections to handle for this product")
        return []
    
    successful_collections = []
    failed_collections = []
    
    print(f"   [INFO] Handling {len(collections)} collections...")
    
    for i, collection_name in enumerate(collections):
        if not collection_name or not collection_name.strip():
            continue
        
        print(f"   [{i+1}/{len(collections)}] Processing collection: '{collection_name}'")
        
        # Create or get collection
        collection_id = create_or_get_collection(collection_name, access_token)
        
        if collection_id:
            # Add product to collection
            if add_product_to_collection(product_id, collection_id, access_token):
                successful_collections.append({
                    'name': collection_name,
                    'id': collection_id
                })
                print(f"   [OK] Added to collection '{collection_name}'")
            else:
                failed_collections.append(collection_name)
                print(f"   [ERROR] Failed to add to collection '{collection_name}'")
        else:
            failed_collections.append(collection_name)
            print(f"   [ERROR] Could not create/get collection '{collection_name}'")
        
        # Small delay between API calls
        time.sleep(0.5)
    
    # Summary
    if successful_collections:
        print(f"   [OK] Successfully added to {len(successful_collections)} collections:")
        for col in successful_collections:
            print(f"     - {col['name']} (ID: {col['id']})")
    
    if failed_collections:
        print(f"   [WARN] Failed to add to {len(failed_collections)} collections:")
        for col_name in failed_collections:
            print(f"     • {col_name}")
    
    return successful_collections

# ============================================
# IMPROVED ATTRIBUTE/VARIANT HANDLING - FIXED
# ============================================

def get_meaningful_attributes_for_product(variants):
    """
    Identify which attribute columns actually contain meaningful variant data
    Returns a list of attribute keys that have values for this specific product
    """
    meaningful_attributes = []
    
    # Define all possible attribute columns
    attribute_columns = [
        'att_Color', 'att_Option', 'att_Thickness', 'att_Wastegate', 
        'att_Type', 'att_Tailpipes', 'att_Valves', 'att_Gearbox', 
        'att_Tips', 'att_Finish', 'att_Diameter', 'att_Design', 
        'att_Can_size', 'att_Side', 'att_Year', 'att_Size', 
        'att_Thread', 'att_any-other-attributes', 'att_Bore', 
        'att_Flow', 'att_Comp_ratio', 'att_Rays_Cap', 'att_Pin_diameter'
    ]
    
    # Check each attribute column across all variants
    for attr_key in attribute_columns:
        values = set()
        
        for variant in variants:
            value = variant.get(attr_key, '').strip()
            if value and value.lower() not in ['n/a', 'na', '-', '']:
                values.add(value)
        
        # If we have at least one variant with a value in this column
        if values:
            meaningful_attributes.append(attr_key)
    
    return meaningful_attributes

def clean_attribute_name(attr_key):
    """
    Convert attribute key to a user-friendly name
    Examples:
    'att_Color' -> 'Color'
    'att_Option' -> 'Option'
    'att_any-other-attributes' -> 'Other Attributes'
    """
    if attr_key.startswith('att_'):
        name = attr_key[4:]
    else:
        name = attr_key
    
    # Replace underscores and hyphens with spaces
    name = name.replace('_', ' ').replace('-', ' ')
    
    # Capitalize each word
    name = ' '.join(word.capitalize() for word in name.split())
    
    return name

def get_variant_title_based_on_attributes(variant, meaningful_attributes, variant_index):
    """
    Create a descriptive variant title based on actual attributes
    FIXED: Add unique identifier to avoid duplicate titles
    Examples:
    - "Black / Ceramix"
    - "Blue / Non-Ceramix"
    - "2.5 inch / Stainless Steel"
    """
    title_parts = []
    
    for attr_key in meaningful_attributes:
        value = variant.get(attr_key, '').strip()
        if value:
            # Clean the value
            value = value.replace('_', ' ').replace('-', ' ').title()
            title_parts.append(value)
    
    if title_parts:
        base_title = " / ".join(title_parts)
        return base_title
    else:
        # If no meaningful attributes, create a generic title with index
        return f"Variant {variant_index + 1}"

def get_product_tags_with_attributes(brand_name, variants, meaningful_attributes, category_tags=None):
    """
    Generate tags including brand and attribute values
    Now also includes category-based tags
    CHANGED: Removed attribute values from tags - only brand, category, subcategory
    """
    tags = []
    
    # Add category-based tags first (Brand, Category, Subcategory)
    if category_tags:
        for tag in category_tags:
            if tag and tag not in tags:
                tags.append(tag)
    
    # Add brand name as tag (if not already added via category_tags)
    if brand_name and brand_name.strip():
        brand_tag = clean_text_for_tag(brand_name.strip())
        if brand_tag and brand_tag not in tags:
            tags.append(brand_tag)
    
    # REMOVED: Attribute values as tags - only brand, category, subcategory should be tags
    
    return tags

def create_product_options_from_meaningful_attributes(variants, meaningful_attributes):
    """
    Create product options only from meaningful attributes
    FIXED: Handle duplicate attribute values properly
    """
    options = {}
    
    for attr_key in meaningful_attributes:
        values = set()
        
        for variant in variants:
            value = variant.get(attr_key, '').strip()
            if value and value.lower() not in ['n/a', 'na', '-', '']:
                values.add(value)
        
        if values:
            # Clean attribute name
            name = clean_attribute_name(attr_key)
            
            # Clean and sort values
            cleaned_values = []
            for value in values:
                # Clean the value for display
                cleaned_value = value.replace('_', ' ').replace('-', ' ')
                cleaned_value = ' '.join(word.capitalize() for word in cleaned_value.split())
                cleaned_values.append(cleaned_value)
            
            cleaned_values.sort()
            options[name] = cleaned_values
    
    # Also check Brand1 and EC_Approved if they vary across variants
    brand_values = set()
    ec_values = set()
    
    for variant in variants:
        brand_val = variant.get('Brand1', '').strip()
        if brand_val:
            brand_values.add(brand_val)
        
        ec_val = variant.get('EC_Approved', '').strip()
        if ec_val:
            ec_values.add(ec_val)
    
    if len(brand_values) > 1:
        options['Brand'] = sorted(list(brand_values))
    
    if len(ec_values) > 1:
        options['EC Approved'] = sorted(list(ec_values))
    
    # If no options found but we have multiple UNIQUE variants, create a default option
    unique_variants = len(set(variant.get('Reference', '').strip() for variant in variants if variant.get('Reference', '').strip()))
    if unique_variants > 1 and not options:
        print(f"   [WARN]  No meaningful attributes found for {unique_variants} unique variants")
        print(f"   Creating default 'Variant' option")
        options['Variant'] = []
        for i in range(unique_variants):
            options['Variant'].append(f"Option {i+1}")
    
    return options

def get_variant_option_values(variant, shopify_options, meaningful_attributes, variant_index):
    """
    Get option values for a specific variant
    FIXED: Handle default "Variant" option properly
    """
    option_values = {}
    
    for i, option in enumerate(shopify_options):
        option_name = option['name']
        value = None
        
        # Check if this option corresponds to an attribute
        for attr_key in meaningful_attributes:
            if clean_attribute_name(attr_key) == option_name:
                value = variant.get(attr_key, '').strip()
                if value:
                    # Clean the value to match option values
                    value = value.replace('_', ' ').replace('-', ' ')
                    value = ' '.join(word.capitalize() for word in value.split())
                break
        
        # Check for Brand or EC Approved options
        if not value:
            if option_name == 'Brand':
                value = variant.get('Brand1', '').strip()
            elif option_name == 'EC Approved':
                value = variant.get('EC_Approved', '').strip()
        
        # If still no value and this is the default 'Variant' option
        if not value and option_name == 'Variant':
            # Assign the appropriate option value based on variant index
            if i < len(option['values']):
                value = option['values'][variant_index % len(option['values'])]
        
        if value and value in option['values']:
            option_values[f"option{i+1}"] = value
        elif value:
            # If value exists but not in option values, log it
            print(f"   [WARN]  Value '{value}' not found in option '{option_name}' values: {option['values']}")
    
    return option_values

# ============================================
# AVAILABILITY TO SHIPPING VALUES MAPPING
# ============================================

def convert_availability_to_shipping_time(availability_value):
    """Convert availability to shipping_time value - NO INTERPRETATION, USE AS-IS"""
    if not availability_value:
        return ""
    
    # Return the value exactly as it is, just cleaned
    value = str(availability_value).strip()
    return value

def convert_availability_to_shipping_margin(availability_value):
    """Convert availability to shipping_time_margin value - NO INTERPRETATION, USE AS-IS"""
    if not availability_value:
        return ""
    
    # Return the value exactly as it is, just cleaned
    value = str(availability_value).strip()
    return value

# ============================================
# SHOPIFY API FUNCTIONS
# ============================================

def create_shopify_draft_product(product_data, access_token):
    """Create a product in Shopify as draft"""
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }
    
    try:
        product_title = product_data['product']['title'][:50]
        vendor = product_data['product'].get('vendor', 'Unknown Vendor')
        print(f"[INFO]  Uploading product: {product_title}...")
        print(f"   Vendor: {vendor}")
        
        # Show variant details being sent to Shopify
        variants = product_data['product'].get('variants', [])
        print(f"   [INFO] Sending {len(variants)} variants to Shopify:")
        
        # Check for duplicate variant titles
        variant_titles = {}
        for i, variant in enumerate(variants):
            variant_title = variant.get('title', 'Unknown')
            if variant_title in variant_titles:
                # This is a duplicate title, we need to make it unique
                variant_titles[variant_title] += 1
                new_title = f"{variant_title} #{variant_titles[variant_title]}"
                variant['title'] = new_title
                print(f"   [WARN]  Fixed duplicate variant title: '{variant_title}' -> '{new_title}'")
            else:
                variant_titles[variant_title] = 1
            
            variant_sku = variant.get('sku', 'No SKU')
            price = variant.get('price', '0.00')
            
            # Show option values if available
            option_values = []
            for j in range(1, 4):
                opt_key = f"option{j}"
                if opt_key in variant:
                    option_values.append(f"{opt_key}: {variant[opt_key]}")
            
            if option_values:
                print(f"   • Variant {i+1}: '{variant.get('title', variant_title)}' | SKU: {variant_sku} | Price: €{price}")
                print(f"     Options: {', '.join(option_values)}")
            else:
                print(f"   • Variant {i+1}: '{variant.get('title', variant_title)}' | SKU: {variant_sku} | Price: €{price}")
        
        response = requests.post(url, headers=headers, json=product_data, timeout=30)
        
        if response.status_code == 201:
            result = response.json()
            product_id = result['product']['id']
            print(f"[OK] Product created as draft (ID: {product_id})")
            return result['product']
        else:
            print(f"[ERROR] Shopify API error: {response.status_code}")
            print(f"Error details: {response.text[:300]}")
            
            # Check for authentication errors
            if response.status_code in [401, 403]:
                print("Authentication error! Token may have expired or is invalid.")
            
            return None
            
    except Exception as e:
        print(f"[ERROR] API Error: {str(e)[:100]}")
        return None

def add_metafield_separately(product_id, namespace, key, value, value_type="single_line_text_field", access_token=None):
    """Add a metafield separately after product creation"""
    if not access_token:
        print("[ERROR] No access token provided for metafield")
        return False
    
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}/metafields.json"
    
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }
    
    metafield_data = {
        "metafield": {
            "namespace": namespace,
            "key": key,
            "value": value,
            "type": value_type
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=metafield_data, timeout=30)
        
        if response.status_code == 201:
            print(f"   [OK] Added product metafield: {namespace}.{key} = '{value}'")
            return True
        else:
            print(f"   [WARN] Failed to add product metafield {namespace}.{key}: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"   [ERROR] Error adding product metafield: {str(e)[:50]}")
        return False

def add_variant_metafield_separately(variant_id, namespace, key, value, value_type="single_line_text_field", access_token=None):
    """Add a metafield to a specific variant"""
    if not access_token:
        print("[ERROR] No access token provided for variant metafield")
        return False
    
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/variants/{variant_id}/metafields.json"
    
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }
    
    metafield_data = {
        "metafield": {
            "namespace": namespace,
            "key": key,
            "value": value,
            "type": value_type
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=metafield_data, timeout=30)
        
        if response.status_code == 201:
            print(f"   [OK] Added variant metafield: {namespace}.{key} = '{value}'")
            return True
        else:
            print(f"   [WARN] Failed to add variant metafield {namespace}.{key}: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"   [ERROR] Error adding variant metafield: {str(e)[:50]}")
        return False

# ============================================
# DUPLICATE CHECKING FUNCTIONS
# ============================================

def get_existing_product_ids():
    """Get all product IDs that are already uploaded from the uploaded sheet"""
    existing_ids = set()
    existing_references = set()
    
    try:
        print("[INFO] Checking for previously uploaded products...")
        uploaded_spreadsheet = safe_gs_call(client.open_by_key, UPLOADED_SPREADSHEET_ID)
        
        try:
            products_sheet = safe_gs_call(uploaded_spreadsheet.worksheet, SHEET_NAMES['products'])
            uploaded_data = safe_gs_call(products_sheet.get_all_values)
            
            if len(uploaded_data) > 1:
                headers = uploaded_data[0]
                
                # Find column indices for product_id and Reference
                product_id_col = -1
                reference_col = -1
                
                for i, header in enumerate(headers):
                    if header.lower() in ['product_id', 'product id', 'id']:
                        product_id_col = i
                    elif header.lower() in ['reference', 'ref', 'sku']:
                        reference_col = i
                
                # Extract all existing product IDs and references
                for row in uploaded_data[1:]:
                    if product_id_col != -1 and product_id_col < len(row):
                        product_id = row[product_id_col].strip()
                        if product_id:
                            existing_ids.add(product_id)
                    
                    if reference_col != -1 and reference_col < len(row):
                        reference = row[reference_col].strip()
                        if reference:
                            existing_references.add(reference)
                
                print(f"   Found {len(existing_ids)} existing product IDs in uploaded sheet")
                print(f"   Found {len(existing_references)} existing references in uploaded sheet")
            
        except Exception as e:
            print(f"   [INFO] No existing uploaded products found or sheet empty: {e}")
    
    except Exception as e:
        print(f"   [ERROR] Could not access uploaded sheet: {e}")
    
    return existing_ids, existing_references

def filter_already_uploaded(grouped_products, existing_ids, existing_references):
    """Filter out products that are already uploaded"""
    if not existing_ids and not existing_references:
        return grouped_products
    
    filtered_products = {}
    skipped_count = 0
    
    print("[INFO] Filtering out already uploaded products...")
    
    for product_id, variants in grouped_products.items():
        # Check if product_id exists in uploaded sheet
        if product_id in existing_ids:
            print(f"   [SKIP] Skipping product {product_id} - already uploaded (by product_id)")
            skipped_count += 1
            continue
        
        # Check if any Reference/SKU exists in uploaded sheet
        skip_product = False
        for variant in variants:
            reference = variant.get('Reference', '').strip()
            if reference and reference in existing_references:
                print(f"   [SKIP] Skipping product {product_id} - Reference '{reference}' already exists")
                skip_product = True
                skipped_count += 1
                break
        
        if not skip_product:
            filtered_products[product_id] = variants
    
    print(f"   [INFO] Filtered results:")
    print(f"   - Original products: {len(grouped_products)}")
    print(f"   - Skipped (already uploaded): {skipped_count}")
    print(f"   - Remaining to upload: {len(filtered_products)}")
    
    return filtered_products

# ============================================
# NEW: ENHANCED HEADER MANAGEMENT
# ============================================

def get_all_headers_from_source():
    """Get all headers from the source sheet"""
    try:
        spreadsheet = safe_gs_call(client.open_by_key, UPDATED_SPREADSHEET_ID)
        sheet = safe_gs_call(spreadsheet.worksheet, SHEET_NAMES['products'])
        all_data = safe_gs_call(sheet.get_all_values)
        
        if len(all_data) > 0:
            return all_data[0]
        return []
    except Exception as e:
        print(f"[ERROR] Error getting headers from source: {e}")
        return []

def get_source_options_headers():
    """Get headers from the source options sheet, if it exists."""
    try:
        spreadsheet = safe_gs_call(client.open_by_key, UPDATED_SPREADSHEET_ID)
        try:
            sheet = safe_gs_call(spreadsheet.worksheet, SHEET_NAMES['options'])
            all_data = safe_gs_call(sheet.get_all_values)
            if len(all_data) > 0:
                return all_data[0]
            else:
                return []
        except gspread.WorksheetNotFound:
            print(f"[INFO] No '{SHEET_NAMES['options']}' sheet found in source spreadsheet. Will use default headers.")
            return []
    except Exception as e:
        print(f"[WARN] Error reading source options sheet: {e}")
        return []


# Fitment/compatibility rows (scraper options tab) + Shopify variant option rows (upload)
FITMENT_OPTIONS_HEADERS = ["product_id", "Brand", "Model", "Type", "Version"]
SHOPIFY_OPTIONS_EXTRA_HEADERS = ["option_name", "option_values", "shopify_product_id"]

OPTIONS_HEADER_ALIASES = {
    "product_id": ("product_id", "product id", "id"),
    "Brand": ("brand", "merk", "brand1", "manufacturer", "fabrikant"),
    "Model": ("model",),
    "Type": ("type", "soort"),
    "Version": ("version", "versie"),
    "option_name": ("option_name", "option name", "optienaam", "optie naam", "optie"),
    "option_values": (
        "option_values",
        "option values",
        "optiewaarden",
        "optie waarden",
        "waarden",
    ),
    "shopify_product_id": ("shopify_product_id", "shopify product id"),
}


def _normalize_options_header_key(header: str) -> str:
    return re.sub(r"\s+", " ", str(header or "").strip().lower())


def header_matches_canonical(header: str, canonical: str) -> bool:
    key = _normalize_options_header_key(header)
    if not key:
        return False
    aliases = OPTIONS_HEADER_ALIASES.get(canonical, (_normalize_options_header_key(canonical),))
    return key in aliases


def _nonempty_headers(row: list) -> list:
    return [str(h).strip() for h in (row or []) if str(h).strip()]


def resolve_uploaded_options_headers(source_headers: list) -> list:
    """
    Headers for uploaded ``options`` tab: fitment columns from source (any language)
    plus Shopify option_name / option_values / shopify_product_id.
    """
    raw = _nonempty_headers(source_headers)
    if not raw:
        return FITMENT_OPTIONS_HEADERS + SHOPIFY_OPTIONS_EXTRA_HEADERS

    out = list(raw)
    for h in SHOPIFY_OPTIONS_EXTRA_HEADERS:
        if not any(header_matches_canonical(existing, h) for existing in out):
            out.append(h)
    return out


def load_source_options_data() -> list:
    """All rows from source spreadsheet options tab (cached per upload run)."""
    try:
        spreadsheet = safe_gs_call(client.open_by_key, UPDATED_SPREADSHEET_ID)
        sheet = safe_gs_call(spreadsheet.worksheet, SHEET_NAMES["options"])
        return safe_gs_call(sheet.get_all_values) or []
    except gspread.WorksheetNotFound:
        return []
    except Exception as e:
        print(f"[WARN] Could not load source options data: {e}")
        return []


def _column_index_for_canonical(headers: list, canonical: str) -> int:
    for i, h in enumerate(headers):
        if header_matches_canonical(h, canonical):
            return i
    return -1


def build_options_row(headers: list, values_by_canonical: dict) -> list:
    """Map canonical field values onto a row for the given header row (Dutch or English)."""
    row = []
    for header in headers:
        value = ""
        for canonical, cell in values_by_canonical.items():
            if header_matches_canonical(header, canonical):
                value = cell if cell is not None else ""
                break
        row.append(value)
    return row


# Uploaded products tab: extra columns appended after source columns
UPLOADED_PRODUCTS_EXTRA_COLUMNS = [
    "Category",
    "Subcategory",
    "Brand",
    "Collection",
    "Tags_Brand",
    "Tags_Category",
    "Tags_Subcategory",
    "Collections_Brand",
    "Collections_Category",
    "Collections_Subcategory",
    "Shopify_Product_ID",
    "Shopify_Variant_ID",
    "Upload_Date",
    "Upload_Status",
    "Shopify_SKU",
]

# Map uploaded header → variant dict keys (column_mapping names)
PRODUCT_HEADER_ALIASES = {
    "product_id": ("product_id", "product id", "id"),
    "Title": ("title", "product title", "name", "titel"),
    "Description": ("description", "body html", "product description", "beschrijving"),
    "Price": ("price", "cost", "sale price", "prijs"),
    "Meta_Title": ("meta_title", "meta title", "seo title"),
    "Meta_Description": ("meta_description", "meta description", "seo description"),
    "Reference": ("reference", "ref", "sku", "product code"),
    "Image_URL": ("image_url", "image url", "images", "picture", "afbeelding"),
    "EC_Approved": ("ec_approved", "ec approved", "tuv"),
    "Availability": ("availability", "stock", "quantity", "voorraad"),
    "Availability_1": ("availability_1", "stock_1", "quantity_1"),
    "Brand1": ("brand1", "brand", "manufacturer", "merk"),
}


def _product_header_key(header: str) -> str:
    return re.sub(r"\s+", " ", str(header or "").strip().lower())


def product_headers_match(header_a: str, header_b: str) -> bool:
    """True if two product sheet column names are the same (incl. aliases)."""
    a = _product_header_key(header_a)
    b = _product_header_key(header_b)
    if not a or not b:
        return False
    if a == b:
        return True
    for _canonical, aliases in PRODUCT_HEADER_ALIASES.items():
        alias_set = {_product_header_key(x) for x in aliases}
        alias_set.add(_product_header_key(_canonical))
        if a in alias_set and b in alias_set:
            return True
    return False


def _variant_dict_keys_for_header(header: str) -> list:
    """Keys to try on variant dict for this sheet column header."""
    keys = [str(header).strip()]
    hk = _product_header_key(header)
    for canonical, aliases in PRODUCT_HEADER_ALIASES.items():
        alias_set = {_product_header_key(x) for x in aliases}
        alias_set.add(_product_header_key(canonical))
        if hk in alias_set:
            keys.append(canonical)
            keys.extend(aliases)
            break
    return keys


def get_variant_cell_for_header(variant: dict, source_headers: list, header: str) -> str:
    """Read one cell by column name from source row or variant dict."""
    if "_source_row" in variant and source_headers:
        for i, src_h in enumerate(source_headers):
            if product_headers_match(src_h, header):
                row = variant["_source_row"]
                if i < len(row):
                    return row[i]
                return ""

    for key in _variant_dict_keys_for_header(header):
        val = variant.get(key, "")
        if val is not None and str(val).strip() != "":
            return val
    return ""


def build_uploaded_products_row(
    uploaded_headers: list,
    variant: dict,
    source_headers: list,
    category_info: dict,
    tags: list,
    collections: list,
    shopify_product_id,
    shopify_variant,
) -> list:
    """Build one uploaded-sheet row aligned to uploaded_headers (not source column order)."""
    tags_list = tags if isinstance(tags, list) else []
    collections_list = collections if isinstance(collections, list) else []
    upload_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = []
    for header in uploaded_headers:
        h = str(header).strip()

        if h == "Category":
            row.append(category_info.get("category", ""))
        elif h == "Subcategory":
            row.append(category_info.get("subcategory", ""))
        elif h == "Brand":
            row.append(category_info.get("brand", ""))
        elif h == "Collection":
            row.append(category_info.get("collection", ""))
        elif h == "Tags_Brand":
            row.append(
                next(
                    (t for t in tags_list if t == clean_text_for_tag(category_info.get("brand", ""))),
                    "",
                )
            )
        elif h == "Tags_Category":
            row.append(
                next(
                    (t for t in tags_list if t == clean_text_for_tag(category_info.get("category", ""))),
                    "",
                )
            )
        elif h == "Tags_Subcategory":
            row.append(
                next(
                    (
                        t
                        for t in tags_list
                        if t == clean_text_for_tag(category_info.get("subcategory", ""))
                    ),
                    "",
                )
            )
        elif h == "Collections_Brand":
            row.append(
                next(
                    (
                        c
                        for c in collections_list
                        if c == clean_text_for_collection(category_info.get("brand", ""))
                    ),
                    "",
                )
            )
        elif h == "Collections_Category":
            row.append(
                next(
                    (
                        c
                        for c in collections_list
                        if c == clean_text_for_collection(category_info.get("category", ""))
                    ),
                    "",
                )
            )
        elif h == "Collections_Subcategory":
            row.append(
                next(
                    (
                        c
                        for c in collections_list
                        if c == clean_text_for_collection(category_info.get("subcategory", ""))
                    ),
                    "",
                )
            )
        elif h == "Shopify_Product_ID":
            row.append(str(shopify_product_id) if shopify_product_id else "")
        elif h == "Shopify_Variant_ID":
            row.append(str(shopify_variant.get("id", "")) if shopify_variant else "")
        elif h == "Upload_Date":
            row.append(upload_stamp)
        elif h == "Upload_Status":
            row.append("Draft")
        elif h == "Shopify_SKU":
            row.append(shopify_variant.get("sku", "") if shopify_variant else "")
        else:
            row.append(get_variant_cell_for_header(variant, source_headers, h))

    return row

def setup_uploaded_sheet_with_extended_headers(source_headers):
    """Setup or get the uploaded data sheet with proper structure including all new columns"""
    try:
        uploaded_spreadsheet = safe_gs_call(client.open_by_key, UPLOADED_SPREADSHEET_ID)
        
        uploaded_sheets = {}
        
        for sheet_name in SHEET_NAMES.values():
            try:
                sheet = safe_gs_call(uploaded_spreadsheet.worksheet, sheet_name)
                print(f"[OK] Found existing sheet: {sheet_name}")
            except:
                print(f"[INFO] Creating new sheet: {sheet_name}")
                sheet = safe_gs_call(uploaded_spreadsheet.add_worksheet, title=sheet_name, rows=1000, cols=80)  # Increased columns
                print(f"[OK] Created new sheet: {sheet_name}")
            
            uploaded_sheets[sheet_name] = sheet
        
        # Setup products sheet
        products_sheet = uploaded_sheets.get('products')
        if not products_sheet:
            print("[ERROR] Products sheet not found")
            return None
        
        current_products_data = safe_gs_call(products_sheet.get_all_values)
        
        # Define NEW columns to add (in addition to Shopify columns)
        new_columns = [
            'Category',
            'Subcategory',
            'Brand',
            'Collection',
            'Tags_Brand',
            'Tags_Category',
            'Tags_Subcategory',
            'Collections_Brand',
            'Collections_Category',
            'Collections_Subcategory'
        ]
        
        # Shopify columns
        shopify_columns = ['Shopify_Product_ID', 'Shopify_Variant_ID', 'Upload_Date', 'Upload_Status', 'Shopify_SKU']
        
        # Combine all columns for products sheet
        all_products_headers = source_headers + new_columns + shopify_columns
        
        if not current_products_data:
            # Sheet is empty, add all headers
            safe_gs_call(products_sheet.update, [all_products_headers], 'A1')
            print(f"[OK] Added ALL headers to products sheet")
            print(f"   Source columns: {len(source_headers)}")
            print(f"   New category/collection columns: {len(new_columns)}")
            print(f"   Shopify columns: {len(shopify_columns)}")
            print(f"   TOTAL: {len(all_products_headers)} columns")
        else:
            # Check if headers need updating
            current_headers = current_products_data[0] if current_products_data else []
            headers_to_add = []
            for header in all_products_headers:
                if header not in current_headers:
                    headers_to_add.append(header)
            
            if headers_to_add:
                print(f"[WARN] Adding {len(headers_to_add)} new columns to products sheet")
                updated_headers = current_headers + headers_to_add
                safe_gs_call(products_sheet.update, [updated_headers], 'A1')
                print(f"[OK] Updated headers in products sheet")
                print(f"   Added columns: {', '.join(headers_to_add)}")
            else:
                print(f"[OK] All headers already present in products sheet")

        # Actual column order on uploaded products tab (may differ from source_headers order)
        products_header_row = safe_gs_call(products_sheet.row_values, 1)
        uploaded_sheets["products_headers"] = products_header_row or all_products_headers
        uploaded_sheets["source_headers"] = source_headers
        print(
            f"[INFO] Uploaded products sheet: {len(uploaded_sheets['products_headers'])} columns "
            f"(values mapped by header name)"
        )
        
        # Setup options sheet (fitment + Shopify variant options; supports translated headers)
        options_sheet = uploaded_sheets.get("options")
        source_options_headers_raw = get_source_options_headers()
        target_options_headers = resolve_uploaded_options_headers(source_options_headers_raw)

        if source_options_headers_raw:
            print(f"[INFO] Source options headers: {_nonempty_headers(source_options_headers_raw)}")
        print(f"[INFO] Uploaded options headers: {target_options_headers}")

        current_options_data = safe_gs_call(options_sheet.get_all_values)
        current_headers = _nonempty_headers(
            current_options_data[0] if current_options_data else []
        )

        if not current_headers:
            safe_gs_call(options_sheet.update, [target_options_headers], "A1")
            print(f"[OK] Added headers to options sheet")
            options_headers = target_options_headers
        else:
            merged = list(current_headers)
            added = []
            for h in target_options_headers:
                if any(header_matches_canonical(existing, h) for existing in merged):
                    continue
                merged.append(h)
                added.append(h)
            if added:
                safe_gs_call(options_sheet.update, [merged], "A1")
                print(f"[OK] Extended options sheet headers: {', '.join(added)}")
                options_headers = merged
            else:
                options_headers = current_headers
                print(f"[OK] Options sheet headers ready ({len(options_headers)} columns)")

        uploaded_sheets["options_headers"] = options_headers
        uploaded_sheets["source_options_data"] = load_source_options_data()
        if uploaded_sheets["source_options_data"]:
            n = max(0, len(uploaded_sheets["source_options_data"]) - 1)
            print(f"[INFO] Loaded {n} source option row(s) for fitment copy")

        return uploaded_sheets
        
    except Exception as e:
        print(f"[ERROR] Error setting up uploaded sheet: {e}")
        import traceback
        traceback.print_exc()
        return None

def copy_fitment_options_to_uploaded_sheet(uploaded_sheets, product_id: str) -> int:
    """
    Copy vehicle fitment rows (Brand/Model/Type/Version or Merk/Model/…) from source
    options tab to uploaded options tab for this product_id.
    """
    options_sheet = uploaded_sheets.get("options")
    headers = uploaded_sheets.get("options_headers", [])
    source_data = uploaded_sheets.get("source_options_data") or []
    if not options_sheet or not headers or len(source_data) < 2:
        return 0

    src_headers = source_data[0]
    pid_col = _column_index_for_canonical(src_headers, "product_id")
    if pid_col < 0:
        return 0

    rows_to_append = []
    want_pid = str(product_id).strip()
    for src_row in source_data[1:]:
        if pid_col >= len(src_row) or str(src_row[pid_col]).strip() != want_pid:
            continue
        values = {"product_id": want_pid}
        for canon in FITMENT_OPTIONS_HEADERS:
            if canon == "product_id":
                continue
            idx = _column_index_for_canonical(src_headers, canon)
            if idx >= 0 and idx < len(src_row):
                values[canon] = src_row[idx]
        rows_to_append.append(build_options_row(headers, values))

    if rows_to_append:
        safe_gs_call(
            options_sheet.append_rows,
            rows_to_append,
            value_input_option="USER_ENTERED",
        )
    return len(rows_to_append)


def save_options_to_uploaded_sheet(uploaded_sheets, product_id, shopify_product_id, options_dict, tags, collections):
    """
    Copy fitment options from source sheet, then append Shopify variant options (Color, etc.).
    """
    try:
        options_sheet = uploaded_sheets.get("options")
        headers = uploaded_sheets.get("options_headers", [])
        if not options_sheet or not headers:
            print("   ⚠️  Options sheet or headers not available, skipping options save.")
            return False

        fitment_rows = copy_fitment_options_to_uploaded_sheet(uploaded_sheets, product_id)
        if fitment_rows:
            print(f"   ✅ Copied {fitment_rows} fitment option row(s) from source options tab")

        rows_to_append = []
        for option_name, option_values in (options_dict or {}).items():
            values = {
                "product_id": str(product_id).strip(),
                "option_name": option_name,
                "option_values": ", ".join(str(v) for v in option_values if v),
                "shopify_product_id": str(shopify_product_id),
            }
            rows_to_append.append(build_options_row(headers, values))

        if rows_to_append:
            safe_gs_call(
                options_sheet.append_rows,
                rows_to_append,
                value_input_option="USER_ENTERED",
            )
            print(f"   ✅ Saved {len(rows_to_append)} Shopify option row(s) to options sheet")
        elif not fitment_rows:
            print("   ℹ️  No fitment or Shopify options to save for this product")

        return True

    except Exception as e:
        print(f"   ⚠️  Error saving options to uploaded sheet: {e}")
        return False

def copy_variant_data_to_uploaded_sheet_extended(
    uploaded_sheets,
    variant_data,
    shopify_product,
    source_headers,
    category_info_dict,
    tags,
    collections,
):
    """
    Copy variant data aligned to the uploaded sheet header row (by column name).
    """
    try:
        products_sheet = uploaded_sheets.get("products")
        uploaded_headers = uploaded_sheets.get("products_headers") or []
        if not products_sheet:
            print("❌ Products sheet not found in uploaded sheets")
            return False
        if not uploaded_headers:
            uploaded_headers = safe_gs_call(products_sheet.row_values, 1)
        if not uploaded_headers:
            print("❌ Uploaded products sheet has no headers")
            return False

        src_headers = uploaded_sheets.get("source_headers") or source_headers or []
        shopify_product_id = shopify_product["id"]
        shopify_variants = shopify_product.get("variants", [])

        rows_added = 0
        batch_rows = []
        batch_size = 100
        expected_len = len(uploaded_headers)

        for variant in variant_data:
            shopify_variant = None
            variant_sku = variant.get("_shopify_sku", "")
            if variant_sku:
                for sv in shopify_variants:
                    if sv.get("sku") == variant_sku:
                        shopify_variant = sv
                        break

            variant_sku_key = variant.get("Reference", "").strip()
            if variant_sku_key in category_info_dict:
                category_info = category_info_dict[variant_sku_key]
            else:
                category_info = {
                    "category": variant.get("Category", ""),
                    "subcategory": variant.get("Subcategory", ""),
                    "brand": variant.get("Brand1", variant.get("Brand", "")),
                    "collection": variant.get("Collection", ""),
                }

            row = build_uploaded_products_row(
                uploaded_headers,
                variant,
                src_headers,
                category_info,
                tags,
                collections,
                shopify_product_id,
                shopify_variant,
            )
            if len(row) != expected_len:
                print(
                    f"   ⚠️  Row length {len(row)} != header count {expected_len} "
                    f"for SKU {variant_sku_key or '(no ref)'}"
                )
                if len(row) < expected_len:
                    row.extend([""] * (expected_len - len(row)))
                else:
                    row = row[:expected_len]

            batch_rows.append(row)
            rows_added += 1

            if len(batch_rows) >= batch_size:
                safe_gs_call(
                    products_sheet.append_rows,
                    batch_rows,
                    value_input_option="USER_ENTERED",
                )
                batch_rows = []

        if batch_rows:
            safe_gs_call(
                products_sheet.append_rows,
                batch_rows,
                value_input_option="USER_ENTERED",
            )

        print(f"✅ Copied {rows_added} variants to uploaded sheet (columns matched by name)")
        return True

    except Exception as e:
        print(f"❌ Error copying variant data to uploaded sheet: {e}")
        import traceback
        traceback.print_exc()
        return False

# ============================================
# DATA PROCESSING FUNCTIONS
# ============================================

def get_products_from_updated_sheet():
    """Get products from the updated Google Sheet and group by product_id"""
    try:
        print("📥 Fetching products from updated sheet...")
        spreadsheet = safe_gs_call(client.open_by_key, UPDATED_SPREADSHEET_ID)
        sheet = safe_gs_call(spreadsheet.worksheet, SHEET_NAMES['products'])
        
        all_data = safe_gs_call(sheet.get_all_values)
        
        if len(all_data) < 2:
            print("⚠️  No product data found in sheet")
            return {}, [], {}
        
        headers = all_data[0]
        print(f"📊 Found {len(headers)} columns in source sheet")
        
        # Create mapping for important columns we need to process
        column_mapping = {}
        
        # Define all possible attribute columns that could be variants
        attribute_columns = [
            'att_Color', 'att_Option', 'att_Thickness', 'att_Wastegate', 
            'att_Type', 'att_Tailpipes', 'att_Valves', 'att_Gearbox', 
            'att_Tips', 'att_Finish', 'att_Diameter', 'att_Design', 
            'att_Can_size', 'att_Side', 'att_Year', 'att_Size', 
            'att_Thread', 'att_any-other-attributes', 'att_Bore', 
            'att_Flow', 'att_Comp_ratio', 'att_Rays_Cap', 'att_Pin_diameter'
        ]
        
        # Basic required columns for processing
        column_names_to_find = {
            'product_id': ['product_id', 'Product_ID', 'Product ID', 'ID'],
            'Title': ['Title', 'Product Title', 'Name'],
            'Description': ['Description', 'Body HTML', 'Product Description'],
            'Price': ['Price', 'Cost', 'Sale Price'],
            'Meta_Title': ['Meta_Title', 'Meta Title', 'SEO Title'],
            'Meta_Description': ['Meta_Description', 'Meta Description', 'SEO Description'],
            'Reference': ['Reference', 'Ref', 'SKU', 'Product Code'],
            'Image_URL': ['Image_URL', 'Image URL', 'Images', 'Picture'],
            'EC_Approved': ['EC_Approved', 'EC Approved', 'TUV'],
            'Availability': ['Availability', 'Stock', 'Quantity'],
            'Availability_1': ['Availability_1', 'Stock_1', 'Quantity_1'],
            'Brand1': ['Brand1', 'Brand', 'Manufacturer']
        }
        
        # Add category/subcategory columns
        column_names_to_find.update({
            col_name: [col_name] for col_name in 
            CATEGORY_CONFIG['category_columns'] + 
            CATEGORY_CONFIG['subcategory_columns'] + 
            CATEGORY_CONFIG['collection_columns']
        })
        
        # Add all attribute columns
        for attr_col in attribute_columns:
            column_names_to_find[attr_col] = [attr_col]
        
        for col_name, possible_names in column_names_to_find.items():
            column_mapping[col_name] = -1
            for possible in possible_names:
                if possible in headers:
                    column_mapping[col_name] = headers.index(possible)
                    print(f"   Found '{col_name}' at column: '{possible}' (index {column_mapping[col_name]})")
                    break
        
        if column_mapping['product_id'] == -1:
            print("❌ Could not find product_id column in products sheet!")
            return {}, [], {}
        
        if column_mapping['Reference'] == -1:
            print("⚠️  Could not find Reference column - SKUs will be generated")
        
        # Group products by product_id
        grouped_products = {}
        
        for row_num, row in enumerate(all_data[1:], start=2):
            product = {}
            
            # Store all columns for later copying
            product['_source_row'] = row
            
            # Map important columns for processing
            for col_name, idx in column_mapping.items():
                if idx != -1 and idx < len(row):
                    product[col_name] = row[idx]
                else:
                    product[col_name] = ""
            
            product_id = product.get('product_id', '').strip()
            
            if product_id and product.get('Title', '').strip():
                product['_row_number'] = row_num
                
                if product_id not in grouped_products:
                    grouped_products[product_id] = []
                
                grouped_products[product_id].append(product)
        
        print(f"✅ Found {len(grouped_products)} unique products with {sum(len(v) for v in grouped_products.values())} total variants")
        
        return grouped_products, headers, column_mapping
        
    except Exception as e:
        print(f"❌ Error reading sheet: {e}")
        import traceback
        traceback.print_exc()
        return {}, [], {}

def create_shopify_options(options_dict):
    """Convert options dictionary to Shopify format"""
    shopify_options = []
    
    # Limit to 3 options maximum (Shopify limitation)
    for i, (option_name, values) in enumerate(list(options_dict.items())[:3]):
        shopify_options.append({
            "name": option_name,
            "position": i + 1,
            "values": values
        })
    
    return shopify_options

def deduplicate_variants_by_sku(variants):
    """
    Deduplicate variants by SKU - if same SKU appears multiple times, keep only one
    Returns deduplicated list of variants
    """
    seen_skus = {}
    deduplicated = []
    
    for variant in variants:
        sku = variant.get('Reference', '').strip()
        if not sku:
            # If no SKU, keep it (it will get a generated SKU later)
            deduplicated.append(variant)
            continue
        
        # Create a unique key based on SKU and all attributes
        key_parts = [sku]
        for attr_key in variant:
            if attr_key.startswith('att_') and variant[attr_key]:
                key_parts.append(f"{attr_key}:{variant[attr_key]}")
        
        key = "|".join(key_parts)
        
        if key not in seen_skus:
            seen_skus[key] = variant
            deduplicated.append(variant)
        else:
            # This is a duplicate, check if price is different
            existing_price = seen_skus[key].get('Price', '0')
            new_price = variant.get('Price', '0')
            if existing_price != new_price:
                print(f"   ⚠️  WARNING: Duplicate SKU '{sku}' found with different prices!")
                print(f"      Existing price: €{existing_price}, New price: €{new_price}")
                print(f"      Keeping the first occurrence with price: €{existing_price}")
            else:
                print(f"   ℹ️  Skipping duplicate variant with SKU '{sku}'")
    
    return deduplicated

def create_shopify_variants(product_id, variants, shopify_options, meaningful_attributes):
    """Create Shopify variants from product variants with proper attribute handling"""
    shopify_variants = []
    
    # Get option names
    option_names = [opt['name'] for opt in shopify_options]
    
    # Track used SKUs to avoid duplicates
    used_skus = set()
    
    # Track variant titles to ensure uniqueness
    used_variant_titles = {}
    
    for i, variant in enumerate(variants):
        # Get SKU from Reference column
        sku = variant.get('Reference', '').strip()
        
        # If SKU is empty, generate one
        if not sku:
            # Generate unique SKU based on product_id and variant index
            base_sku = f"PROD-{product_id}"
            sku_counter = 1
            while True:
                sku = f"{base_sku}-V{sku_counter:03d}"
                if sku not in used_skus:
                    break
                sku_counter += 1
        else:
            # Check for duplicate SKUs (shouldn't happen after deduplication, but just in case)
            if sku in used_skus:
                original_sku = sku
                duplicate_counter = 1
                while sku in used_skus:
                    sku = f"{original_sku}-{duplicate_counter}"
                    duplicate_counter += 1
                print(f"   ⚠️  Duplicate SKU detected, using: {sku}")
        
        # Store the SKU
        used_skus.add(sku)
        variant['_shopify_sku'] = sku
        
        # Generate descriptive variant title
        variant_title = get_variant_title_based_on_attributes(variant, meaningful_attributes, i)
        
        # Check for duplicate titles and make them unique
        if variant_title in used_variant_titles:
            used_variant_titles[variant_title] += 1
            variant_title = f"{variant_title} #{used_variant_titles[variant_title]}"
        else:
            used_variant_titles[variant_title] = 1
        
        # Build option values
        option_values = get_variant_option_values(variant, shopify_options, meaningful_attributes, i)
        
        # Format price
        price_str = variant.get('Price', '0')
        try:
            price_clean = str(price_str).strip()
            price_clean = price_clean.replace('€', '').replace('$', '').replace(' ', '')
            
            if ',' in price_clean and '.' not in price_clean:
                price_clean = price_clean.replace(',', '.')
            elif '.' in price_clean and ',' in price_clean:
                parts = price_clean.split(',')
                if len(parts) == 2:
                    integer_part = parts[0].replace('.', '')
                    decimal_part = parts[1]
                    price_clean = f"{integer_part}.{decimal_part}"
            
            price_float = float(price_clean)
            price = f"{price_float:.2f}"
        except Exception as e:
            price = "0.00"
        
        # Create variant data
        variant_data = {
            "title": variant_title,
            "price": price,
            "sku": sku,
            "inventory_management": None,
            "inventory_policy": "continue",
            "requires_shipping": True,
            "taxable": True,
            "barcode": sku
        }
        
        # Add option values
        variant_data.update(option_values)
        
        shopify_variants.append(variant_data)
    
    return shopify_variants

def prepare_shopify_product(product_group, column_mapping):
    """Prepare product data for Shopify API from product group"""
    
    # Use the first variant as the base product
    base_variant = product_group[0]
    
    title = base_variant.get('Title', 'Untitled Product')
    description = base_variant.get('Description', '')
    meta_title = base_variant.get('Meta_Title', title[:60])
    meta_description = base_variant.get('Meta_Description', '')
    image_url = base_variant.get('Image_URL', '')
    
    # Extract category information from the first variant
    category_info = extract_category_info(base_variant, column_mapping)
    brand_name = category_info.get('brand', '').strip()
    
    # Get EC Approved value - FIXED: use raw value without interpretation
    ec_approved_raw = base_variant.get('EC_Approved', '').strip()
    ec_approved_display = ec_approved_raw  # use exactly as in sheet
    
    # Get availability values from BOTH columns - NO INTERPRETATION
    availability_value = base_variant.get('Availability', '').strip()
    availability_1_value = base_variant.get('Availability_1', '').strip()
    
    # NO INTERPRETATION - Use values exactly as they are
    shipping_time_value = convert_availability_to_shipping_time(availability_value)
    shipping_time_margin_value = convert_availability_to_shipping_margin(availability_1_value)
    
    # Prepare image URLs
    image_urls = []
    if image_url:
        if ',' in image_url:
            image_urls = [url.strip() for url in image_url.split(',')]
        else:
            image_urls = [image_url.strip()]
    
    # DEDUPLICATE VARIANTS BY SKU FIRST
    print(f"   🔍 Checking for duplicate SKUs...")
    original_count = len(product_group)
    deduplicated_variants = deduplicate_variants_by_sku(product_group)
    deduplicated_count = len(deduplicated_variants)
    
    if deduplicated_count < original_count:
        print(f"   ⚠️  Removed {original_count - deduplicated_count} duplicate variants with same SKU")
        print(f"   📊 Now processing {deduplicated_count} unique variants (was {original_count})")
    
    # Identify meaningful attributes for this product (using deduplicated variants)
    meaningful_attributes = get_meaningful_attributes_for_product(deduplicated_variants)
    
    if meaningful_attributes:
        print(f"   Identified meaningful attributes: {', '.join([clean_attribute_name(a) for a in meaningful_attributes])}")
    else:
        print(f"   No meaningful attributes identified")
    
    # Create options from meaningful attributes (using deduplicated variants)
    options_dict = create_product_options_from_meaningful_attributes(deduplicated_variants, meaningful_attributes)
    shopify_options = create_shopify_options(options_dict)
    
    if shopify_options:
        print(f"   Created {len(shopify_options)} product options:")
        for option in shopify_options:
            print(f"     • {option['name']}: {', '.join(option['values'][:3])}{'...' if len(option['values']) > 3 else ''}")
    else:
        print(f"   No product options created")
    
    # Create variants (using deduplicated variants)
    shopify_variants = create_shopify_variants(
        base_variant.get('product_id', ''),
        deduplicated_variants,
        shopify_options,
        meaningful_attributes
    )
    
    print(f"   Created {len(shopify_variants)} UNIQUE variants with descriptive titles")
    
    # Generate tags including category info
    category_tags = generate_tags_from_category_info(category_info)
    
    # Get tags including brand, category, subcategory and attribute values
    # CHANGED: Only pass meaningful_attributes but they won't be used for tags anymore
    tags = get_product_tags_with_attributes(brand_name, product_group, meaningful_attributes, category_tags)
    
    print(f"   Generated {len(tags)} tags: {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}")
    
    # Generate collections
    collections = generate_collections_from_category_info(category_info)
    
    print(f"   Generated {len(collections)} collections: {', '.join(collections)}")
    
    # Prepare product-level metafields
    product_metafields = [
        {
            "namespace": "custom",
            "key": "seo_meta_title",
            "value": meta_title.strip() if meta_title else "",
            "type": "single_line_text_field"
        },
        {
            "namespace": "custom",
            "key": "seo_meta_description",
            "value": meta_description.strip() if meta_description else "",
            "type": "single_line_text_field"
        },
        {
            "namespace": "custom",
            "key": "ec_approved",
            "value": ec_approved_display,
            "type": "single_line_text_field"
        },
        {
            "namespace": "custom",
            "key": "brand_name",
            "value": brand_name if brand_name else "",
            "type": "single_line_text_field"
        }
    ]
    
    # Handle shipping_time metafields according to variant count
    if len(deduplicated_variants) == 1:
        # Single variant: set shipping_time at product level
        product_metafields.append({
            "namespace": "custom",
            "key": "shipping_time",
            "value": shipping_time_value,
            "type": "single_line_text_field"
        })
        product_metafields.append({
            "namespace": "custom",
            "key": "shipping_time_margin",
            "value": shipping_time_margin_value,
            "type": "single_line_text_field"
        })
        print(f"   ⏱️  Single variant: shipping_time set at product level from Availability")
    else:
        # Multi-variant: set product-level shipping_time = "1" (as per user request)
        product_metafields.append({
            "namespace": "custom",
            "key": "shipping_time",
            "value": "1",
            "type": "single_line_text_field"
        })
        print(f"   ⏱️  Multi-variant: product-level shipping_time set to '1' (shipping_time will be set on each variant separately)")
        # No product-level shipping_time_margin added for multi-variant
    
    # Prepare product data
    shopify_product = {
        "product": {
            "title": title,
            "body_html": description,
            "vendor": brand_name if brand_name else "Auto Parts",
            "product_type": "Moshin automation",
            "status": "draft",
            "published": False,
            "published_at": None,
            "images": [],
            "options": shopify_options,
            "variants": shopify_variants,
            "metafields": product_metafields
        }
    }
    
    # Add tags if we have any (limit to 20 tags for Shopify)
    if tags:
        shopify_product["product"]["tags"] = ", ".join(tags[:20])
    
    # Create a dictionary of category info for each variant (for sheet tracking)
    category_info_dict = {}
    for variant in product_group:
        sku = variant.get('Reference', '').strip()
        if sku:
            variant_category_info = extract_category_info(variant, column_mapping)
            category_info_dict[sku] = variant_category_info
    
    return shopify_product, image_urls, base_variant, shipping_time_value, shipping_time_margin_value, ec_approved_display, brand_name, product_group, meaningful_attributes, deduplicated_variants, tags, collections, category_info, category_info_dict, options_dict

# ============================================
# MAIN UPLOAD FUNCTION
# ============================================

def upload_to_shopify():
    """Main function to upload all products to Shopify"""
    
    print("="*70)
    print("🛒 SHOPIFY DRAFT PRODUCT UPLOAD SCRIPT - ENHANCED VERSION")
    print("="*70)
    print(f"📁 Updated Sheet: {UPDATED_SPREADSHEET_ID}")
    print(f"📝 Uploaded Sheet: {UPLOADED_SPREADSHEET_ID}")
    print(f"🏪 Shopify Store: {SHOPIFY_STORE_URL}")
    print("="*70)
    
    # Get access token (static)
    access_token = get_access_token()
    
    # Get products from updated sheet (grouped by product_id)
    grouped_products, source_headers, column_mapping = get_products_from_updated_sheet()
    
    if not grouped_products:
        print("❌ No products found to upload")
        return
    
    # Get already uploaded product IDs and references
    existing_ids, existing_references = get_existing_product_ids()
    
    # Filter out already uploaded products
    filtered_products = filter_already_uploaded(grouped_products, existing_ids, existing_references)
    
    if not filtered_products:
        print("\n📭 No new products to upload. All products are already uploaded.")
        return
    
    # Setup uploaded sheet with EXTENDED headers
    print("\n📋 Setting up uploaded data sheet with EXTENDED columns...")
    uploaded_sheets = setup_uploaded_sheet_with_extended_headers(source_headers)
    if not uploaded_sheets:
        print("❌ Failed to setup uploaded sheet")
        return
    
    print(f"\n🚀 Found {len(filtered_products)} new products to upload")
    print("="*70)
    
    uploaded = []
    failed = []
    
    for i, (product_id, variants) in enumerate(filtered_products.items(), 1):
        print(f"\n📦 Product {i}/{len(filtered_products)}")
        print(f"   Product ID: {product_id}")
        print(f"   Title: {variants[0].get('Title', 'N/A')[:50]}...")
        print(f"   Variants in sheet: {len(variants)}")
        
        # Check for duplicate SKUs in the source data
        skus = [v.get('Reference', '').strip() for v in variants if v.get('Reference', '').strip()]
        unique_skus = set(skus)
        if len(skus) != len(unique_skus):
            print(f"   ⚠️  WARNING: Found {len(skus) - len(unique_skus)} duplicate SKUs in source data")
            print(f"   Original SKUs count: {len(skus)}")
            print(f"   Unique SKUs count: {len(unique_skus)}")
        
        # Extract category info from first variant
        category_info = extract_category_info(variants[0], column_mapping)
        print(f"   Category Info:")
        print(f"     • Brand: {category_info.get('brand', 'Not specified')}")
        print(f"     • Category: {category_info.get('category', 'Not specified')}")
        print(f"     • Subcategory: {category_info.get('subcategory', 'Not specified')}")
        print(f"     • Collection: {category_info.get('collection', 'Not specified')}")
        
        # Prepare product data
        shopify_product, image_urls, base_variant, shipping_time, shipping_time_margin, ec_approved_display, brand_name, all_variants, meaningful_attrs, deduplicated_variants, tags, collections, category_info_main, category_info_dict, options_dict = prepare_shopify_product(variants, column_mapping)
        
        # Upload to Shopify
        try:
            result = create_shopify_draft_product(shopify_product, access_token)
            
            if result:
                shopify_id = result['id']
                
                # Add any missing product metafields (already included in product creation)
                # But we may still need to add them separately if not included (already included)
                
                # Handle collections - FIXED: Now properly creates and links collections
                if collections and access_token:
                    successful_collections = handle_collections_for_product(shopify_id, collections, access_token)
                    print(f"   ✅ Collections handled: {len(successful_collections)} successful")
                else:
                    print(f"   ℹ️  No collections to handle for this product")
                
                # If product has multiple variants, add shipping_time metafields at variant level
                if len(deduplicated_variants) > 1:
                    print(f"   📦 Adding shipping_time metafields to each variant...")
                    # Get created variants from Shopify product
                    shopify_variants = result.get('variants', [])
                    
                    # Build mapping of SKU to variant ID
                    sku_to_variant_id = {v['sku']: v['id'] for v in shopify_variants if v.get('sku')}
                    
                    # For each deduplicated variant, add variant-level metafields
                    for variant in deduplicated_variants:
                        sku = variant.get('_shopify_sku', '')
                        if sku and sku in sku_to_variant_id:
                            variant_id = sku_to_variant_id[sku]
                            
                            # Get availability values from this variant
                            var_avail = variant.get('Availability', '').strip()
                            var_avail1 = variant.get('Availability_1', '').strip()
                            
                            if var_avail:
                                add_variant_metafield_separately(variant_id, "custom", "shipping_time", var_avail, access_token=access_token)
                            if var_avail1:
                                add_variant_metafield_separately(variant_id, "custom", "shipping_time_margin", var_avail1, access_token=access_token)
                        else:
                            print(f"   ⚠️  Could not find variant ID for SKU: {sku}")
                
                # Copy ALL variant data to uploaded sheet with EXTENDED columns
                copy_success = copy_variant_data_to_uploaded_sheet_extended(
                    uploaded_sheets, 
                    all_variants,  # Pass all original variants
                    result, 
                    source_headers,
                    category_info_dict,
                    tags,
                    collections
                )
                
                if copy_success:
                    print(f"   ✅ Successfully copied ALL columns to uploaded sheet")
                else:
                    print(f"   ⚠️  Failed to copy data to uploaded sheet")
                
                # Save options to options sheet
                save_options_to_uploaded_sheet(uploaded_sheets, product_id, shopify_id, options_dict, tags, collections)
                
                uploaded.append({
                    'shopify_id': shopify_id,
                    'product_id': product_id,
                    'title': base_variant.get('Title'),
                    'vendor': brand_name,
                    'variants_count': len(deduplicated_variants),
                    'original_variants_count': len(variants),
                    'attributes': meaningful_attrs,
                    'tags': tags,
                    'collections': collections,
                    'category_info': category_info_main,
                    'variant_metafields': len(deduplicated_variants) > 1  # flag indicating variant-level shipping_time
                })
            else:
                failed.append({
                    'product_id': product_id,
                    'title': base_variant.get('Title'),
                    'vendor': brand_name,
                    'reason': 'Shopify API error'
                })
                
        except Exception as e:
            print(f"   ❌ Upload failed: {e}")
            failed.append({
                'product_id': product_id,
                'title': base_variant.get('Title'),
                'vendor': brand_name,
                'reason': str(e)[:100]
            })
        
        # Rate limiting
        if i < len(filtered_products):
            time.sleep(1)
    
    # Generate summary report
    print(f"\n{'='*70}")
    print("📊 UPLOAD SUMMARY")
    print("="*70)
    print(f"✅ Successfully uploaded: {len(uploaded)}")
    print(f"❌ Failed: {len(failed)}")
    
    total_variants = sum(item['original_variants_count'] for item in uploaded)
    unique_variants = sum(item['variants_count'] for item in uploaded)
    total_tags = sum(len(item.get('tags', [])) for item in uploaded)
    total_collections = sum(len(item.get('collections', [])) for item in uploaded)
    
    print(f"📦 Total variants in sheet: {total_variants}")
    print(f"📦 Unique variants uploaded: {unique_variants}")
    print(f"🔄 Duplicates removed: {total_variants - unique_variants}")
    print(f"🏷️  Total tags generated: {total_tags}")
    print(f"🗂️  Total collections handled: {total_collections}")
    print("="*70)
    
    # Calculate skipped products
    skipped_count = len(grouped_products) - len(filtered_products)
    if skipped_count > 0:
        print(f"\n⏭️  Skipped {skipped_count} already uploaded products")
    
    if uploaded:
        print(f"\n📋 Uploaded products (first 5):")
        for prod in uploaded[:5]:
            print(f"   • ID: {prod['product_id']}, Shopify: {prod['shopify_id']}")
            print(f"     Title: {prod['title'][:50]}...")
            print(f"     Vendor: {prod.get('vendor', 'Not specified')}")
            print(f"     Variants: {prod['variants_count']} (was {prod.get('original_variants_count', '?')} in sheet)")
            
            # Show category info
            cat_info = prod.get('category_info', {})
            if cat_info:
                print(f"     Category: {cat_info.get('category', 'N/A')}")
                print(f"     Subcategory: {cat_info.get('subcategory', 'N/A')}")
            
            # Show tags and collections
            tags_list = prod.get('tags', [])
            if tags_list:
                print(f"     Tags: {', '.join(tags_list[:3])}{'...' if len(tags_list) > 3 else ''}")
            
            collections_list = prod.get('collections', [])
            if collections_list:
                print(f"     Collections: {', '.join(collections_list)}")
            
            # Show shipping time location
            if prod.get('variant_metafields'):
                print(f"     ⏱️  Shipping time set on each variant, product-level shipping_time = 1")
            else:
                print(f"     ⏱️  Shipping time set on product (single variant)")
    
    if failed:
        print(f"\n❌ Failed products (first 10):")
        for prod in failed[:10]:
            print(f"   • ID: {prod['product_id']}")
            print(f"     Title: {prod['title'][:50]}...")
            print(f"     Vendor: {prod.get('vendor', 'Not specified')}")
            print(f"     Reason: {prod['reason']}")
    
    print("\n🎉 Upload process complete!")
    print(f"\n📋 DATA COPIED TO UPLOADED SHEET:")
    print(f"   • All {len(source_headers)} columns from source sheet")
    print(f"   • 10 new category/collection/tag columns")
    print(f"   • 5 Shopify columns")
    print(f"   • Total: {len(source_headers) + 15} columns")
    
    print(f"\n📍 WHERE TO FIND YOUR PRODUCTS:")
    print(f"   1. Go to: https://{SHOPIFY_STORE_URL}/admin/products")
    print(f"   2. Click 'Filters' dropdown")
    print(f"   3. Select 'Moshin automation' (Product Type filter)")
    print(f"   4. Products will appear as DRAFTS (not published)")
    print(f"   5. Check Collections tab for: {', '.join(set([col for prod in uploaded[:3] for col in prod.get('collections', [])]))}...")

# ============================================
# CONFIGURATION CHECK
# ============================================

def check_shopify_config(access_token):
    """Check Shopify configuration"""
    print("🔧 Checking Shopify configuration...")
    
    try:
        url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/shop.json"
        headers = {"X-Shopify-Access-Token": access_token}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            shop_data = response.json().get('shop', {})
            print(f"✅ Connected to Shopify store: {shop_data.get('name', 'Unknown')}")
            return True
        else:
            print(f"❌ Cannot connect to Shopify: {response.status_code}")
            print(f"Response: {response.text[:200]}")
            return False
            
    except Exception as e:
        print(f"❌ Connection test failed: {e}")
        return False

# ============================================
# MAIN EXECUTION
# ============================================

def main():
    """Main execution function"""
    print("="*70)
    print("🛒 ENHANCED SHOPIFY DRAFT PRODUCT UPLOADER")
    print("="*70)
    print("This script will:")
    print("1. Use provided access token for authentication")
    print("2. Read products from UPDATED Google Sheet")
    print("3. DEDUPLICATE variants by SKU")
    print("4. Identify MEANINGFUL attributes")
    print("5. Create UNIQUE variant titles")
    print("6. Handle duplicate SKUs in source data")
    print("7. Extract Category/Subcategory/Brand/Collection info")
    print("8. Generate TAGS: Brand, Category, Subcategory (NO VARIATION ATTRIBUTES)")
    print("9. Create COLLECTIONS: Brand, Category, Subcategory")
    print("10. Skip already uploaded products")
    print("11. DISABLE inventory tracking")
    print("12. Store availability values EXACTLY AS-IS")
    print("13. Add ALL required metafields")
    print("14. Each variant gets unique SKU")
    print("15. Set vendor to BRAND NAME from sheet")
    print("16. Copy ALL columns from source to uploaded sheet")
    print("17. Add 10 NEW category/collection/tag columns")
    print("18. Set product type to 'Moshin automation'")
    print("19. Copy options to options sheet with headers from source")
    print("20. For single‑variant products: shipping_time at product level (from Availability)")
    print("21. For multi‑variant products: product-level shipping_time = '1', and shipping_time on each variant")
    print("="*70)
    
    # Get access token
    access_token = get_access_token()
    if not access_token:
        print("\n❌ No access token provided.")
        return
    
    # Check configuration with the token
    if not check_shopify_config(access_token):
        print("\n❌ Please fix Shopify configuration.")
        return
    
    print("\n🚀 Starting upload of NEW products only...")
    upload_to_shopify()

if __name__ == "__main__":
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"❌ Service account file not found: {SERVICE_ACCOUNT_FILE}")
        print("\n📝 Please create a credentials.json file.")
        exit(1)
    
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Process interrupted by user")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()