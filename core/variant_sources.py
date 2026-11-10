"""Variant readers used for EVERY supplier (tried in this order, first one that finds variants wins):

  1. Shopify stores (e.g. Beehive):  /products/<handle>.js  -> real option names, every variant's SKU/price/stock
  2. WooCommerce (e.g. Heartlands):  variation form data, or the "WPC Variation Radio Buttons" (woovr) list
  3. schema.org JSON-LD  hasVariant
  4. Any other site (e.g. Birlea, Julian Bowen): the option dropdowns / radio buttons next to "Add to basket"
     are switched one by one and the price, SKU and stock shown for each choice are read.

If a product clearly has options but none of these could read them, the scraper saves the page to
data/debug/<supplier>_variants_missing_N.html so the reader can be adjusted.
"""
import itertools
import json
import logging
import re
import time

from selenium.common.exceptions import WebDriverException

from core import variants as V

log = logging.getLogger("scraper")

MAX_COMBOS = 40          # option walker: at most this many combinations per product
JUNK_NAME_RE = r"length|qty|quantity|sort|currency|country|lang|per.?page|limit|uom|unit|filter|order|search"

# ---------------------------------------------------------------- 1. Shopify
SHOPIFY_JS = r"""
const done = arguments[arguments.length - 1];
const isShop = !!(window.Shopify || window.ShopifyAnalytics || document.querySelector('script[src*="cdn.shopify"], link[href*="cdn/shop"]'));
const m = location.pathname.match(/^(.*\/products\/[^\/?#]+)/);
if (!isShop || !m) { done(null); return; }
fetch(m[1] + '.js', {credentials: 'include'}).then(r => r.ok ? r.json() : null).then(done).catch(() => done(null));
"""


def shopify_variants(drv):
    try:
        drv.set_script_timeout(20)
        data = drv.execute_async_script(SHOPIFY_JS)
    except WebDriverException:
        return [], []
    if not data or not data.get("variants"):
        return [], []
    names = [(o.get("name") if isinstance(o, dict) else str(o)) for o in (data.get("options") or [])]
    rows = []
    for v in data["variants"]:
        opts = [v.get(f"option{i}") for i in (1, 2, 3)]
        opts = [o for o in opts if o is not None][:len(names)] or list(v.get("options") or [])
        price = v.get("price")
        cmp_ = v.get("compare_at_price")
        img = (v.get("featured_image") or {}).get("src") or ""
        rows.append({
            "options": opts, "sku": str(v.get("sku") or ""),
            "price": f"{price / 100:.2f}" if isinstance(price, (int, float)) else V.money(price),
            "compare_at_price": f"{cmp_ / 100:.2f}" if isinstance(cmp_, (int, float)) and cmp_ > (price or 0) else "",
            "barcode": str(v.get("barcode") or ""),
            "weight": f"{v['weight'] / 1000:.2f}" if isinstance(v.get("weight"), (int, float)) and v["weight"] else "",
            "image": ("https:" + img) if img.startswith("//") else img,
            "in_stock": bool(v.get("available", True)),
        })
    if len(rows) == 1 and [o.lower() for o in rows[0]["options"]] in ([], ["default title"]):
        return [], []        # a product without options
    return names, rows


# ---------------------------------------------------------------- 2. WooCommerce
WOOVR_JS = r"""
const items = [...document.querySelectorAll('.woovr-variation[data-id], .woovr-variations [data-id]')]
  .filter(e => e.getAttribute('data-id') && e.getAttribute('data-id') !== '0');
if (!items.length) return null;
const labels = {};
document.querySelectorAll('form.variations_form select[name^="attribute_"]').forEach(s => {
  const row = s.closest('tr'); const l = row && row.querySelector('label, th');
  labels[s.name] = {label: l ? l.innerText.trim() : '', values: {}};
  [...s.options].forEach(o => { if (o.value) labels[s.name].values[o.value] = o.text.trim(); });
});
return {labels, items: items.map(e => {
  const a = {}; [...e.attributes].forEach(x => { if (x.name.startsWith('data-')) a[x.name.slice(5)] = x.value; });
  const nameEl = e.querySelector('.woovr-variation-name, .woovr-variation-info, label');
  a._text = nameEl ? nameEl.innerText.trim() : e.innerText.trim().split('\n')[0];
  a._stock = (e.querySelector('.woovr-variation-availability, .stock') || {}).innerText || '';
  return a;
})};
"""


def _pretty(slug):
    return re.sub(r"[-_]+", " ", str(slug)).strip().title()


def woovr_variants(drv):
    try:
        data = drv.execute_script(WOOVR_JS)
    except WebDriverException:
        return [], []
    if not data or not data.get("items"):
        return [], []
    labels = data.get("labels") or {}
    names, rows = [], []
    for it in data["items"]:
        try:
            attrs = json.loads(it.get("attrs") or "{}")
        except ValueError:
            attrs = {}
        vals = {}
        for k, slug in attrs.items():
            info = labels.get(k) or {}
            name = info.get("label") or _pretty(k.replace("attribute_pa_", "").replace("attribute_", ""))
            vals[name] = (info.get("values") or {}).get(slug) or _pretty(slug) if slug else "Any"
        if not vals and it.get("_text"):        # "Colour: Black, Size: King"
            for part in re.split(r",\s*", it["_text"]):
                if ":" in part:
                    k, v = part.split(":", 1)
                    vals[k.strip()] = v.strip()
            if not vals:
                vals["Option"] = it["_text"][:60]
        for k in vals:
            if k not in names:
                names.append(k)
        price = V.money(it.get("price") or it.get("display_price"))
        reg = V.money(it.get("regular-price") or it.get("display_regular_price"))
        stock = (it.get("_stock") or it.get("availability") or "").lower()
        rows.append({
            "_vals": vals, "sku": it.get("sku") or "", "price": price,
            "compare_at_price": reg if (reg and price and float(reg) > float(price)) else "",
            "barcode": "", "weight": "", "image": it.get("imagefull") or it.get("image") or "",
            "in_stock": not re.search(r"out of stock|unavailable", stock) and it.get("purchasable", "yes") != "no",
        })
    names = names[:V.MAX_OPTIONS]
    for r in rows:
        r["options"] = [r["_vals"].get(n, "") for n in names]
        r.pop("_vals")
    return names, rows


# ---------------------------------------------------------------- 4. option walker (any site)
FIND_OPTIONS_JS = r"""
const JUNK = new RegExp(arguments[0], 'i');
const buy = /add to (basket|cart|bag|trolley)|buy now|add to order/i;
const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
function nearBuy(el) {
  let n = el;
  for (let i = 0; i < 9 && n; i++, n = n.parentElement) {
    const btns = n.querySelectorAll('button, input[type=submit], a.button, a.btn');
    for (const b of btns) if (buy.test(b.innerText || b.value || '')) return true;
  }
  return false;
}
function labelFor(el) {
  if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l && l.innerText.trim()) return l.innerText.trim(); }
  let n = el.parentElement;
  for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
    const l = n.querySelector('label, legend, .label, th, dt, [class*=label], [class*=title]');
    if (l && l.innerText.trim() && !l.contains(el)) return l.innerText.trim().split('\n')[0];
  }
  return el.name || el.id || 'Option';
}
const groups = [];
document.querySelectorAll('select').forEach((s, i) => {
  if (!visible(s) || s.disabled || JUNK.test((s.name || '') + ' ' + (s.id || '')) || !nearBuy(s)) return;
  const opts = [...s.options].filter(o => o.value && !o.disabled && !/choose|select|please/i.test(o.text))
                             .map(o => ({value: o.value, text: o.text.trim()}));
  if (opts.length < 2) return;
  s.setAttribute('data-cw-opt', 'g' + groups.length);
  groups.push({kind: 'select', key: 'g' + groups.length, label: labelFor(s).replace(/[:*]\s*$/, ''), options: opts});
});
const radios = {};
document.querySelectorAll('input[type=radio]').forEach(r => {
  if (!r.name || JUNK.test(r.name) || r.disabled || !nearBuy(r)) return;
  (radios[r.name] = radios[r.name] || []).push(r);
});
Object.entries(radios).forEach(([name, rs]) => {
  if (rs.length < 2) return;
  const key = 'g' + groups.length;
  const opts = rs.map((r, j) => {
    r.setAttribute('data-cw-opt', key + '-' + j);
    const l = (r.id && document.querySelector('label[for="' + r.id + '"]')) || r.closest('label');
    const t = (l && l.innerText.trim()) || r.getAttribute('aria-label') || r.title || r.value;
    return {value: String(j), text: t.split('\n')[0].trim()};
  });
  const fs = rs[0].closest('fieldset'); const lg = fs && fs.querySelector('legend');
  groups.push({kind: 'radio', key, label: (lg && lg.innerText.trim()) || labelFor(rs[0]), options: opts});
});
return groups.slice(0, 3);
"""

CHOOSE_JS = r"""
const [kind, key, value] = arguments;
if (kind === 'select') {
  const s = document.querySelector('[data-cw-opt="' + key + '"]'); if (!s) return false;
  s.value = value;
  ['input', 'change'].forEach(t => s.dispatchEvent(new Event(t, {bubbles: true})));
  if (window.jQuery) window.jQuery(s).trigger('change');
  return true;
}
const r = document.querySelector('[data-cw-opt="' + key + '-' + value + '"]'); if (!r) return false;
const l = (r.id && document.querySelector('label[for="' + r.id + '"]')) || r.closest('label');
(l || r).click();
if (!r.checked) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }
return true;
"""


def walk_options(scraper):
    """Select every combination of the product's option dropdowns / radio buttons and read what the page shows."""
    d = scraper.driver
    try:
        groups = d.execute_script(FIND_OPTIONS_JS, JUNK_NAME_RE) or []
    except WebDriverException:
        return [], []
    if not groups:
        return [], []
    names = [g["label"][:60] or f"Option {i + 1}" for i, g in enumerate(groups)]
    combos = list(itertools.islice(itertools.product(*[g["options"] for g in groups]), MAX_COMBOS))
    rows = []
    for combo in combos:
        try:
            for g, opt in zip(groups, combo):
                d.execute_script(CHOOSE_JS, g["kind"], g["key"], opt["value"])
                time.sleep(0.4)
            time.sleep(1.2)                       # let the page update price / SKU / stock
            body = d.execute_script("return (document.querySelector('main') || document.body).innerText;") or ""
        except WebDriverException:
            continue
        sku = scraper._text(scraper.selectors.get("sku")) if scraper.selectors.get("sku") else ""
        m = None if sku else re.search(r"\b(?:SKU|Item SKU|Product code|Code)\b\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-_/.]{2,})",
                                       body, re.I)
        stock = re.search(r"(out of stock|in stock|\d+\s+in stock|pre-?order|backorder)", body, re.I)
        rows.append({
            "options": [o["text"] for o in combo],
            "sku": sku or (m.group(1) if m else ""),
            "price": scraper.best_price(),
            "compare_at_price": "", "barcode": "", "weight": "", "image": "",
            "in_stock": not (stock and stock.group(1).lower().startswith("out")),
        })
    total = 1
    for g in groups:
        total *= len(g["options"])
    if total > MAX_COMBOS:
        log.warning("[%s] more than %d option combinations - only the first %d are listed", scraper.name,
                    MAX_COMBOS, MAX_COMBOS)
    # same SKU for every combination = the page did not change per option: SKUs made unique
    skus = [r["sku"] for r in rows]
    if rows and len(set(skus)) == 1 and len(rows) > 1:
        for r in rows:
            r["sku"] = (r["sku"] + "-" + "-".join(re.sub(r"[^A-Za-z0-9]+", "", o)[:12] for o in r["options"])).strip("-")
    return names, rows


# ---------------------------------------------------------------- "does this page have options?"
HAS_OPTIONS_JS = r"""
const b = document.body.className;
if (/product-type-variable|page-product-configurable|woovr-active|has-variants/.test(b)) return true;
const t = (document.querySelector('main') || document.body).innerText;
return /\b(choose|select)\s+(a\s+)?(size|colou?r|option|finish|fabric)/i.test(t);
"""


def page_has_options(drv):
    try:
        return bool(drv.execute_script(HAS_OPTIONS_JS))
    except WebDriverException:
        return False
