"""Shopify selling price from the supplier's price (X).

    with VAT    = X + X x PRICE_VAT_PERCENT%                      (20%)
    cost        = with VAT + delivery charge                      (40 small ... 80 big, see below)
    price       = cost + cost x PROFIT_MARGIN_PERCENT%            (35% profit margin)
    then rounded UP to the next price ending in 9.99              (PRICE_ROUND_TO_9_99=true)

    Example: X = 100, small item (delivery 40):  100 + 20 = 120;  120 + 40 = 160;  160 + 35% = 216  ->  219.99

    PRICE_MARGIN_ON=supplier_price instead takes the 35% of X only:  100 + 20 + 40 + 35 = 195  ->  199.99

Delivery charge by size (DELIVERY_* settings in .env):
    * big items by name/category (sofa, bed, wardrobe, mattress, ...)            -> DELIVERY_MAX (80)
    * otherwise by weight (kg):  DELIVERY_WEIGHT_STEPS = "15:40,25:50,35:60,50:70"
        up to 15 kg -> 40,  up to 25 kg -> 50,  up to 35 kg -> 60,  up to 50 kg -> 70,  heavier -> 80
    * no weight known: medium furniture by name/category (sideboard, chest, desk, ...) -> DELIVERY_MEDIUM (60),
      everything else -> DELIVERY_MIN (40)
"""
import math
import os
import re


def _f(name, default):
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return float(default)


def _words(name, default):
    return [k.strip().lower() for k in os.getenv(name, default).split(",") if k.strip()]


VAT_PERCENT = _f("PRICE_VAT_PERCENT", 20)
MARGIN_PERCENT = _f("PROFIT_MARGIN_PERCENT", 35)   # new name: the old PRICE_MARGIN_PERCENT (20) is not used any more
MARGIN_ON = (os.getenv("PRICE_MARGIN_ON", "total").strip().lower() or "total")   # total | supplier_price
ROUND_TO_9_99 = os.getenv("PRICE_ROUND_TO_9_99", "true").strip().lower() != "false"

DELIVERY_MIN = _f("DELIVERY_MIN", 40)
DELIVERY_MEDIUM = _f("DELIVERY_MEDIUM", 60)
DELIVERY_MAX = _f("DELIVERY_MAX", 80)
BIG_KEYWORDS = _words("BIG_DELIVERY_KEYWORDS",
                      "sofa,settee,chaise,corner group,recliner,bed,bedstead,ottoman bed,bunk,sleeper,daybed,day bed,"
                      "wardrobe,mattress,dining set,bedroom set,sofabed")
NOT_BIG = _words("NOT_BIG_KEYWORDS", "bedside,bed side,bedding,bed rail,bench,headboard only")
MEDIUM_KEYWORDS = _words("MEDIUM_ITEM_KEYWORDS",
                         "sideboard,chest,drawer,dining table,tv unit,tv stand,media unit,bookcase,desk,cabinet,"
                         "dresser,dressing table,display,shoe,storage unit,console,coffee table,armchair,chair,"
                         "ottoman,blanket box,trunk,bench")


def _steps():
    out = []
    for part in os.getenv("DELIVERY_WEIGHT_STEPS", "15:40,25:50,35:60,50:70").split(","):
        try:
            kg, fee = part.split(":")
            out.append((float(kg), float(fee)))
        except ValueError:
            continue
    return sorted(out)


WEIGHT_STEPS = _steps()


def to_float(v):
    try:
        f = float(str(v).replace(",", "").replace("£", "").replace("€", "").strip())
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _text(p):
    return " ".join(str(x or "") for x in (p.title, p.category, p.breadcrumbs)).lower()


def _has(text, words):
    return any(re.search(r"\b" + re.escape(k) + r"s?\b", text) for k in words)


def is_big(p):
    """Big item (sofa, bed, wardrobe, mattress ...) by name / category / breadcrumbs."""
    t = _text(p)
    for w in NOT_BIG:          # "bedside table" is not a bed
        t = re.sub(r"\b" + re.escape(w) + r"s?\b", " ", t)
    return _has(t, BIG_KEYWORDS)


def delivery_charge(p):
    """Delivery charge for this product: 40 (small) ... 80 (big)."""
    if is_big(p):
        return DELIVERY_MAX
    w = to_float(getattr(p, "weight", ""))
    if w:
        for kg, fee in WEIGHT_STEPS:
            if w <= kg:
                return fee
        return DELIVERY_MAX
    return DELIVERY_MEDIUM if _has(_text(p), MEDIUM_KEYWORDS) else DELIVERY_MIN


def round_up_9_99(v):
    """Smallest price ending in 9.99 that is >= v:  46.56 -> 49.99,  77.32 -> 79.99,  9.50 -> 9.99."""
    k = math.ceil(round((v - 9.99) / 10, 6))
    return round(max(k, 0) * 10 + 9.99, 2)


def selling_price(supplier_price, product):
    """Shopify price (string with 2 decimals) or None when the supplier price is unknown."""
    x = to_float(supplier_price)
    if x is None:
        return None
    with_vat = x * (1 + VAT_PERCENT / 100)
    delivery = delivery_charge(product)
    if MARGIN_ON.startswith("supplier"):
        v = with_vat + delivery + x * MARGIN_PERCENT / 100
    else:
        v = (with_vat + delivery) * (1 + MARGIN_PERCENT / 100)
    if ROUND_TO_9_99:
        v = round_up_9_99(v)
    return f"{v:.2f}"
