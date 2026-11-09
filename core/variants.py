"""Pure helpers (no Selenium) to turn scraped variant data into one clean structure.

Variant dict: {"options": [str,...], "sku", "price", "compare_at_price", "barcode",
               "weight", "image", "in_stock"}
"""
import itertools
import re

MAX_OPTIONS = 3      # Shopify limit
MAX_VARIANTS = 100   # Shopify REST default limit


def money(v):
    try:
        f = float(str(v).replace(",", "").replace("£", "").strip())
        return f"{f:.2f}" if f > 0 else ""
    except (ValueError, TypeError):
        return ""


def parse_weight_kg(text):
    """'12.5 kg' -> '12.5', '800g' -> '0.8'. Returns '' if not found."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kgs?|kilograms?|g|grams?)\b", (text or "").lower())
    if not m:
        return ""
    val = float(m.group(1))
    if m.group(2) in ("g", "gram", "grams"):
        val = val / 1000.0
    return f"{val:g}"


def woo_variants(attrs, variations):
    """WooCommerce: attrs = [{"name","label","values":{slug:text}}], variations = data-product_variations."""
    attrs = attrs[:MAX_OPTIONS]
    names = [a["label"] for a in attrs]
    out = []
    for var in variations or []:
        va = var.get("attributes") or {}
        choices = []
        for a in attrs:
            slug = va.get(a["name"])
            choices.append([a["values"].get(slug, slug)] if slug else list(a["values"].values()))
        combos = list(itertools.product(*choices))
        price = money(var.get("display_price"))
        regular = money(var.get("display_regular_price"))
        compare = regular if (regular and price and float(regular) > float(price)) else ""
        img = var.get("image") or {}
        w = var.get("weight")
        for i, combo in enumerate(combos):
            sku = str(var.get("sku") or "")
            if sku and len(combos) > 1:
                sku = f"{sku}-{i + 1}"
            out.append({
                "options": list(combo), "sku": sku, "price": price, "compare_at_price": compare,
                "barcode": "", "weight": parse_weight_kg(f"{w} kg") if w not in (None, "") else "",
                "image": img.get("full_src") or img.get("src") or "",
                "in_stock": bool(var.get("is_in_stock", True)),
            })
    return names, out


def ld_variants(ld):
    """schema.org Product/ProductGroup with hasVariant."""
    items = ld.get("hasVariant")
    if not isinstance(items, list) or not items:
        return [], []
    keys = ["color", "colour", "size", "material", "pattern"]
    rows, names = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        vals = {}
        for k in keys:
            v = it.get(k)
            if isinstance(v, dict):
                v = v.get("name")
            if v:
                vals[k.capitalize()] = str(v)
        if not vals and it.get("name"):
            vals["Variant"] = str(it["name"])
        offers = it.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        offers = offers if isinstance(offers, dict) else {}
        img = it.get("image")
        img = img[0] if isinstance(img, list) and img else img
        rows.append((vals, str(it.get("sku") or ""), money(offers.get("price")),
                     img if isinstance(img, str) else "", str(it.get("gtin13") or it.get("gtin") or "")))
        for k in vals:
            if k not in names:
                names.append(k)
    names = names[:MAX_OPTIONS]
    out = []
    for vals, sku, price, img, bc in rows:
        out.append({"options": [vals.get(n, "") for n in names], "sku": sku, "price": price,
                    "compare_at_price": "", "barcode": bc, "weight": "", "image": img, "in_stock": True})
    return names, out


def finalize(names, variants, base):
    """Clean up: unique option combos, caps, fill missing price/sku from `base`.
    base = {"sku","price","compare_at_price","barcode","weight"} of the product itself."""
    names = [n for n in names[:MAX_OPTIONS]]
    out, seen = [], set()
    for v in variants:
        opts = (list(v.get("options", [])) + [""] * len(names))[:len(names)]
        key = tuple(o.lower() for o in opts)
        if names and key in seen:
            continue
        seen.add(key)
        v = dict(v)
        v["options"] = opts
        v["price"] = v.get("price") or base.get("price", "")
        v["weight"] = v.get("weight") or base.get("weight", "")
        out.append(v)
    out = out[:MAX_VARIANTS]
    if not out:
        names = []
        out = [{"options": [], "sku": base.get("sku", ""), "price": base.get("price", ""),
                "compare_at_price": base.get("compare_at_price", ""), "barcode": base.get("barcode", ""),
                "weight": base.get("weight", ""), "image": "", "in_stock": True}]
    return names, out
