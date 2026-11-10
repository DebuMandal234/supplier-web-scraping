"""Flair Trade - www.flairbeds.co.uk  (Magento 2 + Hyva theme). Prices are shown ex VAT, only when logged in.

Why Flair needs its own rules:
  * products AND categories AND info pages all have one-level URLs (/beds, /flair-zara-...), so the generic
    "URL shape" guess cannot tell them apart -> we use Magento's <body> class instead
    (catalog-product-view = product, catalog-category-view = category);
  * every category page has dozens of filter links (?cat=..&ck_f_colour=..) - following them made the
    crawler wander through filter pages instead of products -> filter links are ignored, ?p=N pages kept;
  * each category page's product links are opened and saved to Excel one by one before the next page;
    the sitemap is a safety net afterwards (products already saved are skipped);
  * logged in / logged out is read from the <body> class (ft-buyer / ft-guest);
  * sizes / colours (configurable products) come from Magento's option config in the page.
"""
import json
import logging
import re

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from core import variants as V
from core.base import BaseScraper
from core.payload import slugify

log = logging.getLogger("scraper")

BODY_JS = "return document.body ? document.body.className : '';"

# Finds Magento's configurable-product config: initConfigurableOptions('123', {...}) (Hyva) or
# "jsonConfig": {...} / "spConfig": {...} (Luma). Returns the parsed object or null.
CONFIG_JS = r"""
function grab(src, start) {
  let i = src.indexOf('{', start); if (i < 0) return null;
  let depth = 0, inStr = false, q = '', esc = false;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (inStr) { if (esc) esc = false; else if (c === '\\') esc = true; else if (c === q) inStr = false; continue; }
    if (c === '"' || c === "'") { inStr = true; q = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { try { return JSON.parse(src.slice(i, j + 1)); } catch (e) { return null; } } }
  }
  return null;
}
// Flair: option data (sizes / colours with SKU, price, photos) sits in <script type="application/json" data-dn-config>
for (const s of document.querySelectorAll('script[data-dn-config], script[type="application/json"]')) {
  const t = s.textContent || '';
  if (t.indexOf('"attributes"') < 0 || t.indexOf('optionPrices') < 0) continue;
  try { const cfg = JSON.parse(t); if (cfg && cfg.attributes && Object.keys(cfg.attributes).length) return cfg; } catch (e) {}
}
const sources = [];
document.querySelectorAll('[x-data]').forEach(el => {
  const x = el.getAttribute('x-data') || '';
  if (x.indexOf('ConfigurableOptions') >= 0 || x.indexOf('Swatch') >= 0) sources.push(x);
});
document.querySelectorAll('script:not([src])').forEach(s => {
  const t = s.textContent || '';
  if (t.indexOf('"attributes"') >= 0 && (t.indexOf('optionPrices') >= 0 || t.indexOf('"index"') >= 0)) sources.push(t);
});
sources.push(document.documentElement.innerHTML);   // last resort: the whole page
for (const src of sources) {
  const re = /(initConfigurableOptions\s*\(\s*['"]?\d+['"]?\s*,|"jsonConfig"\s*:|"spConfig"\s*:|optionConfig\s*:)/g;
  let m;
  while ((m = re.exec(src)) !== null) {
    const cfg = grab(src, m.index + m[0].length);
    if (cfg && cfg.attributes && Object.keys(cfg.attributes).length) return cfg;
  }
}
return null;
"""

# Specification / "At a glance" key:value pairs + spec tables on the product page
FLAIR_SPECS_JS = r"""
const out = {};
const root = document.querySelector('main') || document.body;
root.querySelectorAll('table tr').forEach(tr => {
  const c = tr.querySelectorAll('th,td');
  if (c.length === 2) {
    const k = c[0].innerText.trim().replace(/:$/, ''), v = c[1].innerText.trim();
    if (k && v && k.length < 60) out[k] = v;
  }
});
root.querySelectorAll('dl').forEach(dl => {
  dl.querySelectorAll('dt').forEach(dt => {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') {
      const k = dt.innerText.trim().replace(/:$/, ''), v = dd.innerText.trim();
      if (k && v && k.length < 60) out[k] = v;
    }
  });
});
root.querySelectorAll('li').forEach(el => {
  if (el.closest('nav, header, footer, .ft-pcard, .product-item')) return;
  const t = el.innerText.trim();
  const m = t.match(/^([A-Za-z][A-Za-z0-9 \/()&.'-]{1,40}):\s*(.+)$/);
  if (m && t.length < 250 && !el.querySelector('li')) out[m[1].trim()] = m[2].trim();
});
return out;
"""

MAIN_INFO_CSS = ".product-info-main, [class*=product-info], .ft-buy, main"


class FlairScraper(BaseScraper):
    name = "flair"
    base_url = "https://www.flairbeds.co.uk/"
    login_url = "https://www.flairbeds.co.uk/customer/account/login/"
    start_urls = ["https://www.flairbeds.co.uk/beds", "https://www.flairbeds.co.uk/kids-beds",
                  "https://www.flairbeds.co.uk/mattresses-and-bedding", "https://www.flairbeds.co.uk/furniture",
                  "https://www.flairbeds.co.uk/trade-picks", "https://www.flairbeds.co.uk/"]
    # the visible sign-in form on the login page (the header also has a hidden pop-up login form)
    user_selector = "#email"
    pass_selector = "#pass"
    submit_selector = "#customer-login-form button[type=submit]"

    # Category page -> open each product on it (read + save to Excel) -> next category page -> ...
    # The sitemap is used afterwards as a safety net for products not linked from any category.
    listing_product_selector = "a.product-item-link"
    warn_suspects = False
    prioritise_product_links = False
    follow_query_links = False
    exclude_regex = re.compile(
        BaseScraper.exclude_regex.pattern +
        r"|/(customer|checkout|wishlist|sales|catalogsearch|quickorder|catalog/product_compare|trade|tradefeed|"
        r"trade-guides|trade-faq|sectors|how-to-order|showroom|delivery|contact|llms[^/]*)(/|$|\?)", re.I)

    # ------------------------------------------------------------------ page type / login state
    def _body_class(self):
        try:
            return self.driver.execute_script(BODY_JS) or ""
        except WebDriverException:
            return ""

    def is_logged_in(self):
        cls = self._body_class()
        if "ft-buyer" in cls:
            return True
        if "ft-guest" in cls:
            return False
        return None

    def is_product_page(self):
        cls = self._body_class()
        self.last_signals = {"body_class": cls[:200]}
        if "catalog-product-view" in cls:
            return True
        if "catalog-category-view" in cls or "cms-" in cls:
            return False
        return super().is_product_page()

    # ------------------------------------------------------------------ extraction
    def _first(self, css, attr=None, within=None):
        try:
            root = within or self.driver
            for el in root.find_elements(By.CSS_SELECTOR, css):
                v = el.get_attribute(attr) if attr else el.text
                if v and v.strip():
                    return v.strip()
        except WebDriverException:
            pass
        return ""

    def _main(self):
        for css in MAIN_INFO_CSS.split(", "):
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, css)
                for el in els:
                    if el.find_elements(By.CSS_SELECTOR, "h1, [data-price-amount]"):
                        return el
            except WebDriverException:
                continue
        return None

    def _prices(self):
        """(price, rrp) of THIS product - from the main product box, not from related products."""
        main = self._main()
        price = self._first("[data-price-type=finalPrice][data-price-amount]", "data-price-amount", main) \
            or self._first("[data-price-amount]", "data-price-amount", main)
        rrp = self._first("[data-price-type=oldPrice][data-price-amount]", "data-price-amount", main)
        if not price:   # logged-in Hyva price box: <div class="final-price"><span class="price">£209.91</span>
            price = self._price_from(self._first(".product-info-main .final-price .price, .final-price .price"))
        if not rrp:     # <div class="old-price"><span class="price">£329.99</span> (RRP inc VAT)
            rrp = self._price_from(self._first(".product-info-main .old-price .price, .old-price .price"))
        price, rrp = V.money(price), V.money(rrp)
        if rrp and price and float(rrp) <= float(price):
            rrp = ""
        return price, rrp

    def _config_variants(self, base_sku):
        try:
            cfg = self.driver.execute_script(CONFIG_JS)
        except WebDriverException:
            cfg = None
        if not cfg:
            return [], []
        def order(a):   # Size first, then Color, then anything else (like the Shopify listing)
            lab = (a.get("label") or a.get("code") or "").lower()
            return (0 if "size" in lab else 1 if ("colour" in lab or "color" in lab) else 2,
                    int(a.get("position") or 0))
        attrs = sorted(cfg.get("attributes", {}).values(), key=order)[:V.MAX_OPTIONS]

        def nice(a):
            lab = (a.get("label") or a.get("code") or "Option").strip()
            return "Color" if lab.lower() in ("colour", "color", "ck_colour") else lab
        names = [nice(a) for a in attrs]
        index = cfg.get("index") or {}
        if not index:   # rebuild product -> option map from the options' product lists
            for a in attrs:
                for o in a.get("options", []):
                    for pid in o.get("products", []):
                        index.setdefault(str(pid), {})[str(a["id"])] = str(o["id"])
        labels = {str(a["id"]): {str(o["id"]): o.get("label", "") for o in a.get("options", [])} for a in attrs}
        prices = cfg.get("optionPrices") or {}
        skus = cfg.get("sku") or cfg.get("skus") or {}
        images = cfg.get("images") or {}
        salable = cfg.get("salable") or {}
        rows = []
        for pid, combo in index.items():
            opts = [labels[str(a["id"])].get(str(combo.get(str(a["id"]), "")), "") for a in attrs]
            pr = prices.get(pid) or prices.get(str(pid)) or {}
            final = V.money((pr.get("finalPrice") or {}).get("amount"))
            old = V.money((pr.get("oldPrice") or {}).get("amount"))
            img = images.get(pid) or images.get(str(pid)) or []
            if isinstance(img, list) and img:   # the variant's main photo (isMain), else its first one
                img = next((i for i in img if isinstance(i, dict) and i.get("isMain")), img[0])
            img = img if isinstance(img, dict) else {}
            sku = skus.get(pid) or skus.get(str(pid)) or ""
            if not sku and base_sku:
                sku = f"{base_sku}-{'-'.join(slug for slug in (re.sub(r'[^A-Za-z0-9]+', '', o) for o in opts) if slug)}"
            in_stock = True
            if salable:
                in_stock = any(str(pid) in [str(x) for x in (v or [])] for vals in salable.values()
                               for v in (vals.values() if isinstance(vals, dict) else []))
            rows.append({
                "options": opts, "sku": str(sku), "price": final,
                "compare_at_price": old if (old and final and float(old) > float(final)) else "",
                "barcode": "", "weight": "", "image": img.get("full") or img.get("img") or "",
                "in_stock": in_stock,
            })
        # same order as the dropdowns on the site
        order = {str(a["id"]): [str(o["id"]) for o in a.get("options", [])] for a in attrs}

        def sort_key(r):
            key = []
            for i, a in enumerate(attrs):
                lab = r["options"][i]
                ids = [oid for oid, l in labels[str(a["id"])].items() if l == lab]
                key.append(order[str(a["id"])].index(ids[0]) if ids else 999)
            return key
        rows.sort(key=sort_key)
        return names, rows

    def extract_variants(self, ld, base):
        names, rows = self._config_variants(base.get("sku", ""))   # Magento option config in the page
        if rows:
            log.info("[flair]   %d variants (%s)", len(rows), ", ".join(names))
            return V.finalize(names, rows, base)
        return super().extract_variants(ld, base)                   # shared readers + debug copy if needed

    def extract_product(self):
        p = super().extract_product()
        d = self.driver

        # price / RRP from the product's own price box (logged-in trade price, ex VAT)
        price, rrp = self._prices()
        if price:
            p.price = price
        if rrp:
            p.rrp = rrp
        elif p.rrp and p.price and V.money(p.rrp) and float(V.money(p.rrp)) <= float(p.price or 0):
            p.rrp = ""

        # title: <h1> without the brand kicker; brand from JSON-LD (already set by the base class)
        h1 = self._first("h1")
        if h1:
            p.title = h1 if (not p.brand or h1.lower().startswith(p.brand.lower())) else f"{p.brand} {h1}"
            p.handle = slugify(p.title)

        # SKU: "SKU CL66988" line under the title, JSON-LD otherwise
        if not p.sku or len(p.sku) < 3:
            m = re.search(r"\bSKU\s*[:#]?\s*([A-Z0-9][A-Z0-9\-_/.]{2,})", self._body_text())
            if m:
                p.sku = m.group(1)

        # breadcrumbs from JSON-LD BreadcrumbList (the page has no visible crumb links)
        try:
            for s in d.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json']"):
                txt = s.get_attribute("textContent") or ""
                if "BreadcrumbList" not in txt:
                    continue
                data = json.loads(txt)
                items = sorted(data.get("itemListElement", []), key=lambda i: i.get("position", 0))
                crumbs = [(i.get("item") or {}).get("name") or i.get("name") or "" for i in items]
                crumbs = [c for c in crumbs if c and c.lower() != "home"][:-1]   # last one = the product
                if crumbs:
                    p.breadcrumbs = " > ".join(crumbs)
                    p.category = crumbs[-1]
                break
        except (WebDriverException, ValueError, AttributeError):
            pass

        # description: the page's Description section (HTML), JSON-LD text as fallback
        for css in ("#description .prose", "#description", ".product.attribute.description .value",
                    "[id*=description] .prose", ".product-description"):
            html = self._html(css)
            if html and len(html) > 40:
                p.description = html
                p.description_text = self._text(css) or p.description_text
                break

        # all product photos (main gallery), full size
        try:
            imgs = d.execute_script(r"""
              // Flair gallery (#gallery.kbg): one slide per photo; lazy slides keep their URLs in data-srcset.
              // Take the biggest size of every slide (1200x1500), so ALL photos are found, not only the first.
              const out = [];
              const biggest = el => {
                const set = el.getAttribute('srcset') || el.getAttribute('data-srcset') || '';
                let best = '', bw = 0;
                set.split(',').forEach(part => {
                  const [u, w] = part.trim().split(/\s+/);
                  const n = parseInt(w || '0', 10);
                  if (u && n >= bw) { best = u; bw = n; }
                });
                return best || el.getAttribute('data-src') || el.getAttribute('src') || '';
              };
              document.querySelectorAll('#gallery .kbg__open img, .kbg .kbg__open img').forEach(i => {
                const u = biggest(i);
                if (u && /catalog\/product\//.test(u)) out.push(new URL(u, location.href).href);
              });
              if (!out.length) {   // older gallery markup
                document.querySelectorAll('[x-data*=gallery] img, .product.media img, #gallery img').forEach(i => {
                  const u = biggest(i);
                  if (u && /catalog\/product\//.test(u)) out.push(new URL(u, location.href).href);
                });
              }
              return out;
            """) or []
            imgs = list(dict.fromkeys(i for i in imgs if i.startswith("http")))
            if imgs:
                p.images = imgs   # all gallery photos, in the site's order
        except WebDriverException:
            pass

        # specifications (Specification / At a glance lists)
        try:
            specs = d.execute_script(FLAIR_SPECS_JS) or {}
            if specs:
                p.specs = specs
        except WebDriverException:
            pass
        if not p.weight:
            for k, v in p.specs.items():
                if "weight" in k.lower():
                    p.weight = V.parse_weight_kg(v)
                    if p.weight:
                        break

        # stock text near the buy box
        body = self._body_text()
        m = re.search(r"(?im)^\s*(in stock[^\n]{0,60}|out of stock[^\n]{0,40}|low stock[^\n]{0,40}|"
                      r"pre-?order[^\n]{0,40}|back in stock[^\n]{0,60}|\d+\s+in stock[^\n]{0,40})\s*$", body)
        if m:
            p.availability = m.group(1).strip()
        elif not p.availability and p.variants:
            p.availability = "InStock" if any(v.get("in_stock", True) for v in p.variants) else "OutOfStock"

        # single (non-configurable) product: keep the variant in line with the corrected fields
        if len(p.variants) == 1 and not p.option_names:
            v = p.variants[0]
            v.update({"sku": p.sku, "price": V.money(p.price), "weight": p.weight or v.get("weight", "")})
            if p.rrp:
                v["compare_at_price"] = V.money(p.rrp)
        else:
            for v in p.variants:
                v["price"] = v.get("price") or V.money(p.price)
        if not p.price and p.variants:
            ps = [float(v["price"]) for v in p.variants if v.get("price")]
            p.price = f"{min(ps):.2f}" if ps else ""
        if not p.price:
            log.warning("[flair] no price on %s - is the session still logged in?", p.url)
        return p
