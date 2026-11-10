import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))   # works from any folder (e.g. Windows Task Scheduler)


SUPPLIERS = ["beehive", "birlea", "heartlands", "julian_bowen", "flair"]
SUPPLIER_NAMES = {  # name shown in Shopify (vendor tag + collection)
    "beehive": "Beehive", "birlea": "Birlea", "heartlands": "Heartlands",
    "julian_bowen": "Julian Bowen", "flair": "Flair",
}

HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
PAGE_DELAY = float(os.getenv("PAGE_DELAY", "2"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20000"))  # safety cap on pages visited per supplier
SAVE_EVERY = max(1, int(os.getenv("SAVE_EVERY", "1")))  # write to Excel after every N products


def _abs(path):
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


DATA_DIR = _abs(os.getenv("DATA_DIR", "data"))
EXCEL_PATH = _abs(os.getenv("EXCEL_PATH", os.path.join(BASE_DIR, "Supplier Data.xlsx")))

# --- Shopify ---
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "").strip()
_MIN_API = "2026-04"   # inventory mutations need the @idempotent / changeFromQuantity rules of 2026-04+
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10").strip()
if SHOPIFY_API_VERSION < _MIN_API:
    SHOPIFY_API_VERSION = "2026-10"
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_PRODUCT_STATUS = os.getenv("SHOPIFY_PRODUCT_STATUS", "DRAFT").strip().upper()
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID", "").strip()   # empty = the store's first location
SHOPIFY_BATCH_SIZE = max(1, int(os.getenv("SHOPIFY_BATCH_SIZE", "20")))  # upload when this many are waiting
PRICE_MARKUP = float(os.getenv("PRICE_MARKUP", "1.0"))
SHOPIFY_TOKEN_FILE = os.path.join(BASE_DIR, "access token.txt")   # current Shopify access token (never on GitHub)
# Safety switches - what the program may change in Shopify:
SHOPIFY_ALLOW_SYNC = os.getenv("SHOPIFY_ALLOW_SYNC", "false").lower() == "true"          # STEP 2 stock/price
SHOPIFY_CREATE_COLLECTIONS = os.getenv("SHOPIFY_CREATE_COLLECTIONS", "false").lower() == "true"

# --- Stock rules (step 2) ---
STOCK_IN_STOCK_QTY = int(os.getenv("STOCK_IN_STOCK_QTY", "5"))   # supplier says "in stock" without a number
STOCK_BUFFER = int(os.getenv("STOCK_BUFFER", "0"))               # subtracted from a real stock number
MISSING_RUNS_BEFORE_ZERO = int(os.getenv("MISSING_RUNS_BEFORE_ZERO", "2"))


def credentials(supplier: str):
    prefix = supplier.upper()
    return os.getenv(f"{prefix}_USERNAME", ""), os.getenv(f"{prefix}_PASSWORD", "")


def markup(supplier: str) -> float:
    """Price markup for one supplier: FLAIR_MARKUP=1.8 in .env, otherwise PRICE_MARKUP."""
    v = os.getenv(f"{supplier.upper()}_MARKUP", "").strip()
    try:
        return float(v) if v else PRICE_MARKUP
    except ValueError:
        return PRICE_MARKUP
