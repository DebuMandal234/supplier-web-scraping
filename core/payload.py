"""Build the JSON body for Shopify  POST /admin/api/<version>/products.json

    {"product": {"title", "body_html", "vendor", "product_type", "handle", "tags", "status",
                 "options": [{"name","values"}], "variants": [...], "images": [...], "metafields": [...],
                 "metafields_global_title_tag", "metafields_global_description_tag"}}
"""
import html
import re

import config


def slugify(text, max_len=80):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].strip("-")


def _price(v, markup):
    try:
        return f"{float(v) * markup:.2f}"
    except (ValueError, TypeError):
        return None


def body_html(p):
    parts = []
    if p.short_description and p.short_description not in (p.description_text or ""):
        parts.append(f"<p>{html.escape(p.short_description)}</p>")
    desc = p.description or ""
    if desc:
        parts.append(desc if "<" in desc else f"<p>{html.escape(desc)}</p>")
    elif p.description_text:
        parts.append(f"<p>{html.escape(p.description_text)}</p>")
    if p.specs:
        rows = "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
                       for k, v in p.specs.items())
        parts.append(f"<h3>Specifications</h3><table>{rows}</table>")
    return "\n".join(parts)


def build_payload(p, markup=None, status=None):
    markup = config.PRICE_MARKUP if markup is None else markup
    status = (status or config.SHOPIFY_PRODUCT_STATUS or "DRAFT").lower()

    names = list(p.option_names or [])
    variants_in = p.variants or [{"options": [], "sku": p.sku, "price": p.price, "compare_at_price": p.rrp,
                                  "barcode": p.ean, "weight": p.weight, "image": "", "in_stock": True}]

    options = []
    for i, n in enumerate(names):
        vals = []
        for v in variants_in:
            val = (v["options"][i] if i < len(v["options"]) else "") or "Default"
            if val not in vals:
                vals.append(val)
        options.append({"name": n, "values": vals})

    variants = []
    for v in variants_in:
        out = {}
        for i in range(len(names)):
            out[f"option{i + 1}"] = (v["options"][i] if i < len(v["options"]) else "") or "Default"
        price = _price(v.get("price") or p.price, markup)
        if price is not None:
            out["price"] = price
        cmp_ = _price(v.get("compare_at_price"), markup)
        if cmp_ and price and float(cmp_) > float(price):
            out["compare_at_price"] = cmp_
        if v.get("sku"):
            out["sku"] = v["sku"]
        if v.get("barcode"):
            out["barcode"] = v["barcode"]
        if v.get("weight"):
            try:
                out["weight"] = float(v["weight"])
                out["weight_unit"] = "kg"
            except ValueError:
                pass
        out.update({"taxable": True, "requires_shipping": True,
                    "inventory_management": None, "inventory_policy": "continue"})
        variants.append(out)

    images, seen = [], set()
    for u in list(p.images) + [v.get("image", "") for v in variants_in]:
        if u and u not in seen:
            seen.add(u)
            images.append({"src": u, "alt": p.title[:250], "position": len(images) + 1})

    tags = []
    for t in [p.supplier, p.brand, p.category] + [c.strip() for c in (p.breadcrumbs or "").split(">")]:
        t = (t or "").strip()
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)

    product = {
        "title": p.title,
        "body_html": body_html(p),
        "vendor": p.brand or p.supplier,
        "product_type": p.category,
        "handle": p.handle or slugify(p.title),
        "tags": ", ".join(tags),
        "status": status,
        "options": options,
        "variants": variants,
        "images": images[:250],
        "metafields": [
            {"namespace": "supplier", "key": "name", "type": "single_line_text_field", "value": p.supplier},
            {"namespace": "supplier", "key": "sku", "type": "single_line_text_field", "value": p.sku or "-"},
            {"namespace": "supplier", "key": "source_url", "type": "single_line_text_field", "value": p.url},
        ],
        "metafields_global_title_tag": p.title[:70],
        "metafields_global_description_tag": (p.meta_description or p.description_text or "")[:320],
    }
    if not options:
        del product["options"]
    return {"product": product}
