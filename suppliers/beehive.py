"""Beehive Furniture Wholesale - portal.beehivefurniture.co.uk (a Shopify store, trade prices after login).

How products are read (checked on the logged-in portal, Nov 2026):
  * Catalogue: the logged-in portal's /products.json lists EVERY product (about 920) with trade price, SKU,
    variants, all images and description - 4 requests instead of clicking through category pages.
  * Stock: Beehive's Stock Feed API (/api/stock, key BEEHIVE_STOCK_TOKEN in .env) gives the exact stock level of
    every SKU; /api/products adds the RRP and spec dimensions. Without a key the stock number is read from each
    product page ("14 in stock").
  * Dimensions / specs: the "Dimensions" block of each product page (Width, Depth, Height, Seat depth ...;
    bundles such as "3+2 Set" list every piece).
  * "Special Order" products are made to order (4-6 weeks): their "Grade 1-4" options carry the same SKU and
    price, so they are listed as ONE variant, marked "Made to order".
API limit: one call per endpoint every 10 minutes - each run calls each endpoint at most once.
"""
import json
import logging
import os
import re
from urllib.parse import urlparse

import requests
from selenium.common.exceptions import WebDriverException

from core import variants as V
from core.base import BaseScraper
from core.models import Product

log = logging.getLogger("scraper")

API_BASE = "https://api.beehivefurniture.co.uk"

FETCH_JSON_JS = r"""
const done = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'include'}).then(r => r.ok ? r.json() : null).then(done).catch(() => done(null));
"""

# stock number + dimensions/spec rows of one product page (fetched inside the logged-in browser)
PAGE_DETAILS_JS = r"""
const done = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'include'}).then(r => r.ok ? r.text() : '').then(html => {
  if (!html) { done(null); return; }
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const t = e => (e ? e.textContent.replace(/\s+/g, ' ').trim() : '');
  const q = doc.querySelector('.bh-prod-stock__qty');
  const badge = doc.querySelector('.bh-prod-stock .beehive-badge');
  const specs = {};
  doc.querySelectorAll('.bh-dimensions__row').forEach(row => {
    const lab = row.querySelector('.bh-dimensions__label') || row.querySelector('[class*="__label"]');
    const val = row.querySelector('.bh-dimensions__value-main') || row.querySelector('[class*="__value"]');
    let label = t(lab), value = t(val);
    if (!label || !value) return;
    const piece = row.closest('.bh-bundle-dimensions__piece');
    const pname = piece ? t(piece.querySelector('.bh-bundle-dimensions__piece-name')) : '';
    if (pname) label = pname.charAt(0).toUpperCase() + pname.slice(1) + ' - ' + label;
    if (/^(width|depth|height|seat|arm|back|length|diameter)/i.test(t(lab)) && /^[\d.]+$/.test(value)) label += ' (cm)';
    specs[label] = value;
  });
  const imgs = [];
  doc.querySelectorAll('.product__media img, media-gallery img, [class*="product-media"] img, .bundle-gallery__main img, .bundle-gallery__thumb img').forEach(i => {
    let u = i.getAttribute('src') || ''; if (!u) return;
    if (u.startsWith('//')) u = 'https:' + u;
    u = u.replace(/([?&])width=\d+/, '$1width=2000');
    if (/cdn\/shop|cdn\.shopify/.test(u) && !imgs.includes(u.split('?')[0])) imgs.push(u.split('?')[0]);
  });
  done({qty: q ? parseInt(t(q), 10) : null, badge: t(badge), specs, images: imgs});
}).catch(() => done(null));
"""

TYPE_FROM_TITLE = [
    (r"u[\s-]?shape", "U-Shape Sofas"), (r"corner|chaise", "Corner Sofas"), (r"sofa ?bed|sleeping function",
                                                                              "Sofa Beds"),
    (r"recliner.*armchair|armchair.*recliner|rise and recline", "Recliner Chairs"), (r"recliner", "Recliner Sofas"),
    (r"armchair|swivel|chair", "Armchairs"), (r"footstool|pouffe|ottoman", "Footstools & Pouffes"),
    (r"wardrobe", "Wardrobes"), (r"\bbed\b", "Beds"), (r"seater|sofa|\d\+\d", "Sofas"),
]
SKIP_TAGS = {"furniture", "sofas", "special order", "bundle", "discontinued", "sleeping function"}


class BeehiveScraper(BaseScraper):
    name = "beehive"
    base_url = "https://portal.beehivefurniture.co.uk/"
    login_url = "https://portal.beehivefurniture.co.uk/"
    start_urls = ["https://portal.beehivefurniture.co.uk/"]
    walk_options = False

    def __init__(self, driver):
        super().__init__(driver)
        self._catalog = None
        self._api = None

    # ------------------------------------------------------------------ data sources
    def _fetch_json(self, path):
        try:
            self.driver.set_script_timeout(60)
            return self.driver.execute_async_script(FETCH_JSON_JS, path)
        except WebDriverException as e:
            log.warning("[beehive] could not read %s (%s)", path, str(e)[:80])
            return None

    def catalog(self):
        """Every product of the logged-in portal (/products.json, 250 per page)."""
        if self._catalog is not None:
            return self._catalog
        if not self.driver.current_url.startswith(self.base_url.rstrip("/")):
            self.driver.get(self.base_url)
            self.pause()
        items = []
        for page in range(1, 41):
            data = self._fetch_json(f"/products.json?limit=250&page={page}")
            batch = (data or {}).get("products") or []
            items += batch
            if len(batch) < 250:
                break
        self._catalog = items
        self.stats["sitemap_urls"] = len(items)
        log.info("[beehive] portal catalogue: %d products", len(items))
        return items

    def api(self):
        """{sku: {"stock": int, "rrp": str, "specs": {...}}} from the Stock Feed API ({} without a key)."""
        if self._api is not None:
            return self._api
        self._api = {}
        key = (os.getenv("BEEHIVE_STOCK_TOKEN") or os.getenv("BEEHIVE_API_KEY") or "").strip()
        if not key:
            log.info("[beehive] no BEEHIVE_STOCK_TOKEN in .env - stock is read from each product page")
            return self._api
        headers = {"x-api-key": key}
        for path in ("/api/stock", "/api/products"):
            try:
                r = requests.get(API_BASE + path, headers=headers, timeout=60)
            except requests.RequestException as e:
                log.warning("[beehive] stock API %s not reachable: %s", path, e)
                continue
            if r.status_code == 429:
                log.warning("[beehive] stock API %s: rate limit (1 call / 10 min) - wait %ss or use page data",
                            path, r.headers.get("Retry-After", "?"))
                continue
            if r.status_code == 401:
                log.error("[beehive] stock API key refused (401) - check BEEHIVE_STOCK_TOKEN in .env")
                break
            if not r.ok:
                log.warning("[beehive] stock API %s: HTTP %s", path, r.status_code)
                continue
            for it in (r.json() or {}).get("products") or []:
                sku = str(it.get("sku") or "").strip().lower()
                if not sku:
                    continue
                d = self._api.setdefault(sku, {})
                if "stock_level" in it:
                    d["stock"] = it.get("stock_level")
                if path == "/api/products":
                    d["rrp"] = V.money(it.get("price"))
                    d["specs"] = it.get("metafields") or {}
        log.info("[beehive] stock API: %d SKUs", len(self._api))
        return self._api

    def page_details(self, handle):
        try:
            self.driver.set_script_timeout(60)
            return self.driver.execute_async_script(PAGE_DETAILS_JS, f"/products/{handle}") or {}
        except WebDriverException:
            return {}

    # ------------------------------------------------------------------ one product
    @staticmethod
    def _type(pr):
        tags = [t for t in pr.get("tags") or [] if not t.startswith("MTO:") and t.lower() not in SKIP_TAGS
                and not (t.isupper() and len(t) > 2)]
        if (pr.get("product_type") or "").strip():
            return pr["product_type"].strip().capitalize()
        for t in tags:
            if re.search(r"seater|corner|armchair|chair|pouffe|footstool|recliner|sofa|chaise|wardrobe|bed", t, re.I):
                return t[:1].upper() + t[1:]
        title = (pr.get("title") or "").lower()
        for pat, name in TYPE_FROM_TITLE:
            if re.search(pat, title):
                return name
        return "Sofas"

    def build(self, pr, details=True):
        handle = pr.get("handle") or ""
        url = self.base_url.rstrip("/") + "/products/" + handle
        tags = pr.get("tags") or []
        special = any(t.lower() == "special order" or t.startswith("MTO:") for t in tags)
        api = self.api()
        info = self.page_details(handle) if details else {}
        variants_src = pr.get("variants") or []
        if special:                                   # Grade 1-4 = same SKU + price: one made-to-order variant
            variants_src = variants_src[:1]
        names = [o.get("name") for o in pr.get("options") or [] if o.get("name") and o.get("name") != "Title"]
        if special:
            names = []
        images = [i.get("src") for i in pr.get("images") or [] if i.get("src")] or list((info or {}).get("images") or [])
        rows = []
        for v in variants_src:
            sku = (v.get("sku") or "").strip() or handle.replace("-", "_")   # bundles: handle-based SKU (as the API)
            a = api.get(sku.lower(), {})
            if special:
                in_stock, qty = True, None
            elif a.get("stock") is not None:
                qty = int(a["stock"] or 0)
                in_stock = qty > 0
            elif len(variants_src) == 1 and (info or {}).get("qty") is not None:
                qty = int(info["qty"])
                in_stock = qty > 0
            else:
                qty, in_stock = None, bool(v.get("available", True))
            img = (v.get("featured_image") or {}).get("src") if isinstance(v.get("featured_image"), dict) else ""
            opts = [v.get(f"option{i}") for i in (1, 2, 3)][:len(names)]
            rows.append({"options": [str(o) for o in opts if o is not None], "sku": sku,
                         "price": V.money(v.get("price")), "compare_at_price": a.get("rrp") or "",
                         "barcode": "", "weight": f"{v['grams'] / 1000:.2f}" if v.get("grams") else "",
                         "image": img or "", "in_stock": in_stock, "_qty": qty, "qty": qty})
        first = rows[0] if rows else {}
        specs = dict((api.get(str(first.get("sku", "")).lower(), {}) or {}).get("specs") or {})
        specs.update((info or {}).get("specs") or {})
        ptype = self._type(pr)
        if special:
            availability = "Made to order (4-6 weeks)"
        elif first.get("_qty") is not None:
            availability = f"{first['_qty']} in stock" if first["_qty"] > 0 else "Out of stock"
        else:
            availability = "In stock" if first.get("in_stock") else "Out of stock"
        for r in rows:
            r.pop("_qty", None)
        ranges = [t.title() for t in tags if t.isupper() and len(t) > 2]
        extra = (["Made to order"] if special else []) + (["Discontinued"] if "discontinued" in
                                                           [t.lower() for t in tags] else []) + ranges
        body = pr.get("body_html") or ""
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        p = Product(
            supplier=self.name, url=url, title=(pr.get("title") or "").strip(), handle=handle,
            sku=first.get("sku", ""), brand="", category=ptype, breadcrumbs=f"Sofas > {ptype}" if ptype != "Sofas"
            else "Sofas", tags=extra, price=first.get("price", ""), rrp=first.get("compare_at_price", ""),
            availability=availability, description=body, description_text=text,
            meta_description=text[:300], images=images, specs=specs,
            option_names=names if len(rows) > 1 else [], variants=rows if len(rows) > 1 else [],
        )
        base = {"sku": p.sku, "price": p.price, "rrp": p.rrp, "ean": "", "weight": first.get("weight", ""),
                "image": images[0] if images else ""}
        p.option_names, p.variants = V.finalize(p.option_names, p.variants, base)
        if len(p.variants) == 1:
            p.variants[0]["in_stock"] = first.get("in_stock")
            p.variants[0]["qty"] = first.get("qty")
        return p

    # ------------------------------------------------------------------ STEP 1 + STEP 2 entry points
    def crawl(self, limit=None, known_urls=None):
        """Every product of the portal catalogue (no category browsing). Known products are skipped."""
        known = {self.norm(u) for u in (known_urls or [])}
        found = 0
        for pr in self.catalog():
            url = self.base_url.rstrip("/") + "/products/" + (pr.get("handle") or "")
            if self.norm(url) in known:
                continue
            self.stats["pages"] += 1
            try:
                p = self.build(pr)
            except Exception as e:
                log.warning("[beehive] could not read %s: %s", url, str(e)[:120])
                self.stats["failed"].append(url)
                continue
            found += 1
            self.stats["products"] += 1
            log.info("[beehive] #%d %s  (%s, %s)", found, p.title, p.availability, f"£{p.price}" if p.price else "no price")
            yield p
            if limit and found >= limit:
                return

    def read_product(self, url):
        """STEP 2: the product from the catalogue + live stock (no page browsing needed with an API key)."""
        handle = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        for pr in self.catalog():
            if pr.get("handle") == handle:
                return self.build(pr, details=not self.api())
        return None
