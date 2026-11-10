"""Shopify selling price from the supplier's price.

    price = supplier price + supplier price x PRICE_MARGIN_PERCENT% + fee
    fee   = PRICE_FEE_BIG (85) for sofas / big items, otherwise PRICE_FEE_STANDARD (35)
    then rounded UP to the next price ending in 9.99:   46.56 -> 49.99,   77.32 -> 79.99,   50.10 -> 59.99

All numbers are in the store's currency (the supplier prices are taken as they are shown).
"""
import math
import os
import re

import config


def _f(name, default):
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return float(default)


MARGIN_PERCENT = _f("PRICE_MARGIN_PERCENT", 20)
FEE_STANDARD = _f("PRICE_FEE_STANDARD", 35)
FEE_BIG = _f("PRICE_FEE_BIG", 85)
ROUND_TO_9_99 = os.getenv("PRICE_ROUND_TO_9_99", "true").strip().lower() != "false"
BIG_KEYWORDS = [k.strip().lower() for k in os.getenv(
    "BIG_ITEM_KEYWORDS",
    "sofa,settee,chaise,corner sofa,corner group,sofa bed,recliner sofa").split(",") if k.strip()]
BIG_MIN_WEIGHT_KG = _f("BIG_ITEM_MIN_WEIGHT_KG", 0)   # 0 = do not use weight


def to_float(v):
    try:
        f = float(str(v).replace(",", "").replace("£", "").replace("€", "").strip())
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def is_big(p):
    """Sofa / big item? By keywords in title, category, breadcrumbs, product type; or by weight."""
    text = " ".join(str(x or "") for x in (p.title, p.category, p.breadcrumbs)).lower()
    if any(re.search(r"\b" + re.escape(k) + r"s?\b", text) for k in BIG_KEYWORDS):
        return True
    w = to_float(p.weight)
    return bool(BIG_MIN_WEIGHT_KG and w and w >= BIG_MIN_WEIGHT_KG)


def round_up_9_99(v):
    """Smallest price ending in 9.99 that is >= v:  46.56 -> 49.99,  77.32 -> 79.99,  9.50 -> 9.99."""
    k = math.ceil(round((v - 9.99) / 10, 6))
    return round(max(k, 0) * 10 + 9.99, 2)


def selling_price(supplier_price, product):
    """Shopify price (string with 2 decimals) or None when the supplier price is unknown."""
    cost = to_float(supplier_price)
    if cost is None:
        return None
    v = cost * (1 + MARGIN_PERCENT / 100) + (FEE_BIG if is_big(product) else FEE_STANDARD)
    if ROUND_TO_9_99:
        v = round_up_9_99(v)
    return f"{v:.2f}"
