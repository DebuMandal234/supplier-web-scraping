import os

from dotenv import load_dotenv

load_dotenv()

SUPPLIERS = ["beehive", "birlea", "heartlands", "julian_bowen", "flair"]

HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
PAGE_DELAY = float(os.getenv("PAGE_DELAY", "2"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20000"))  # safety cap on pages visited per supplier
SAVE_EVERY = max(1, int(os.getenv("SAVE_EVERY", "1")))  # write to Excel after every N products
DATA_DIR = os.getenv("DATA_DIR", "data")

EXCEL_PATH = os.getenv("EXCEL_PATH", r"C:\Users\USER1\Desktop\WebScraping\Supplier Data.xlsx")

# --- Part 2 (Shopify), not used yet ---
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_PRODUCT_STATUS = os.getenv("SHOPIFY_PRODUCT_STATUS", "DRAFT").upper()
PRICE_MARKUP = float(os.getenv("PRICE_MARKUP", "1.0"))


def credentials(supplier: str):
    prefix = supplier.upper()
    return os.getenv(f"{prefix}_USERNAME", ""), os.getenv(f"{prefix}_PASSWORD", "")
