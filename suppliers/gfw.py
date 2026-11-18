"""GFW (GFW Furniture) - public Salsify customer catalogue, no login.

Catalogue page: https://app.salsify.com/catalogs/fff47cdf-0cd9-4ef9-95c3-a8c22721a367/products
How products are read (checked Nov 2026):
  * The catalogue page loads its data from a public JSON API - the scraper reads that directly:
      list:    /catalogs/api/catalogs/<id>/products?filter=%3D&page=N&per_page=200   (about 590 products)
      product: /catalogs/api/catalogs/<id>/products/<product code>
  * One product = one product code (colours and sizes are separate products with their own code).
  * Per product: Standard Price + Direct Fulfilment Price (trade, GBP), Free Stock (exact), EAN/Barcode,
    Product Group + Product Type, Web Description (HTML) + USP 1-5, colour, materials, assembled dimensions
    and weight, assembly info, number of boxes, every photo (main, roomset, cut-out, detail - 2000 x 2000).
  * Which price is the supplier price: GFW_PRICE=standard (default) or GFW_PRICE=direct in .env
    (Direct Fulfilment = GFW delivers to your customer).
The browser is not needed (public API); if the API refuses plain requests the browser is used as fallback.
"""
import html
import logging
import os
import re
import time

import requests
from selenium.common.exceptions import WebDriverException

from core import variants as V
from core.base import BaseScraper
from core.models import Product

log = logging.getLogger("scraper")

CATALOG_ID = os.getenv("GFW_CATALOG_ID", "fff47cdf-0cd9-4ef9-95c3-a8c22721a367")
SITE = "https://app.salsify.com"
API = f"{SITE}/catalogs/api/catalogs/{CATALOG_ID}"
PAGE = f"{SITE}/catalogs/{CATALOG_ID}/products"

FETCH_JSON_JS = r"""
const done = arguments[arguments.length - 1];
fetch(arguments[0]).then(r => r.ok ? r.json() : null).then(done).catch(() => done(null));
"""

IMAGE_GROUPS = ("roomset", "cut-out", "cut out", "detail", "lifestyle", "image")   # photo groups, in this order
SKIP_GROUPS = ("pricing", "main product details", "shipping")                       # not shown as specs
KEEP_FROM_SKIPPED = {"Total Number of Boxes": "Number of Boxes"}
UNIT_BY_GROUP = (("(cm)", "cm"),)
WEIGHT_WORDS = ("weight",)
KEEP_UPPER = {"TV", "LED", "USB", "MDF", "UK", "XL", "2DR", "3DR"}


class GfwScraper(BaseScraper):
    name = "gfw"
    base_url = SITE + "/"
    login_url = PAGE
    start_urls = [PAGE]
    walk_options = False

    def __init__(self, driver):
        super().__init__(driver)
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0 Safari/537.36",
            "Accept": "application/json", "Referer": PAGE,
        })
        self._use_browser = False

    # ------------------------------------------------------------------ no login on this site
    def login(self):
        log.info("[gfw] public catalogue - no login needed")

    def is_logged_in(self):
        return True

    # ------------------------------------------------------------------ API
    def get(self, url):
        if not self._use_browser:
            for attempt in range(3):
                try:
                    r = self.http.get(url, timeout=60)
                    if r.status_code == 404:
                        return None
                    if r.ok:
                        return r.json()
                    if r.status_code in (401, 403):
                        log.info("[gfw] the API refused a plain request (%s) - using the browser", r.status_code)
                        self._use_browser = True
                        break
                except (requests.RequestException, ValueError) as e:
                    log.warning("[gfw] %s (%s) - retrying", url, str(e)[:80])
                time.sleep(2 * (attempt + 1))
            else:
                return None
        try:
            if not self.driver.current_url.startswith(SITE):
                self.driver.get(PAGE)
                self.pause()
            self.driver.set_script_timeout(60)
            return self.driver.execute_async_script(FETCH_JSON_JS, url)
        except WebDriverException as e:
            log.warning("[gfw] could not read %s (%s)", url, str(e)[:80])
            return None

    def product_codes(self):
        codes = []
        for page in range(1, 100):
            data = self.get(f"{API}/products?filter=%3D&page={page}&per_page=200") or {}
            batch = [p.get("id") for p in data.get("products") or [] if p.get("id")]
            codes += batch
            total = ((data.get("meta") or {}).get("total_entries")) or 0
            if not batch or len(codes) >= total:
                break
        codes = list(dict.fromkeys(codes))
        self.stats["sitemap_urls"] = len(codes)
        log.info("[gfw] %d products in the catalogue", len(codes))
        return codes

    @staticmethod
    def url_for(code):
        return f"{PAGE}/{code}"

    # ------------------------------------------------------------------ one product
    def build(self, d):
        code = str(d.get("id") or "")
        groups = d.get("property_groups") or []
        val, by_group = {}, []
        for g in groups:
            props = []
            for p in g.get("properties") or []:
                vals = [str(v.get("name") if v.get("name") is not None else v.get("id") or "").strip()
                        for v in p.get("values") or []]
                vals = [v for v in vals if v]
                if not vals:
                    continue
                val[p.get("id")] = ", ".join(vals)
                props.append((p.get("name") or p.get("id"), p.get("id"), ", ".join(vals)))
            by_group.append((g.get("name") or "", props))

        title = (val.get("Name") or d.get("name") or code).strip()
        if title.isupper():      # "ADA TEDDY ACCENT CHAIR" -> "Ada Teddy Accent Chair" ("TV", "150cm" kept readable)
            title = " ".join(w if w in KEEP_UPPER else w.lower() if re.fullmatch(r"\d+\w*", w) else w.capitalize()
                             for w in title.split())

        which = (os.getenv("GFW_PRICE", "standard") or "standard").strip().lower()
        price = V.money(val.get("Direct Fulfilment Price") if which.startswith("direct") else val.get("Standard Price"))
        if not price:
            price = V.money(val.get("Standard Price")) or V.money(val.get("Direct Fulfilment Price"))

        qty = None
        try:
            qty = max(int(float(val.get("Free Stock"))), 0) if val.get("Free Stock") not in (None, "") else None
        except ValueError:
            qty = None
        arrival = (val.get("Stock Arrival Date") or "")[:10]
        if qty is None:
            availability = "In stock"
        elif qty > 0:
            availability = f"{qty} in stock"
        else:
            availability = "Out of stock" + (f" (arriving {arrival})" if arrival else "")

        # description: web description + the USP bullet points
        desc = (val.get("Web Description") or "").strip()
        if desc and "<" not in desc:
            desc = "".join(f"<p>{html.escape(x.strip())}</p>" for x in re.split(r"\n\s*\n|\r\n\r\n", desc) if x.strip())
        usps = [val.get(f"USP {i}") for i in range(1, 11) if val.get(f"USP {i}")]
        if usps:
            desc += "<ul>" + "".join(f"<li>{html.escape(u)}</li>" for u in usps) + "</ul>"
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc))).strip()

        # specs: materials, dimensions, assembly, features (not prices / shipping codes)
        specs = {}
        for gname, props in by_group:
            low = gname.lower()
            unit = next((u for k, u in UNIT_BY_GROUP if k in low), "")
            skipped = any(s in low for s in SKIP_GROUPS)
            for label, pid, v in props:
                if skipped:
                    if pid in KEEP_FROM_SKIPPED:
                        specs[KEEP_FROM_SKIPPED[pid]] = v
                    continue
                lu = re.search(r"\((kg|cm|mm)\)\s*$", label, re.I)
                label = re.sub(r"\s*\((?:kg|cm|mm)\)\s*$", "", label, flags=re.I)
                if label == "Colour One":
                    label = "Colour"
                if re.fullmatch(r"[\d.]+", v):
                    if lu:
                        v += " " + lu.group(1).lower()
                    elif any(w in label.lower() for w in WEIGHT_WORDS):
                        v += " kg"
                    elif unit:
                        v += f" {unit}"
                specs[label] = v
        weight = V.money(val.get("Assembled Weight")) or V.money(val.get("Total Carton Weight"))
        weight = f"{float(weight):g}" if weight else ""

        # photos: main photo first, then roomset / cut-out / detail photos (never PDFs / instructions)
        images = []

        def add(asset):
            if not isinstance(asset, dict):
                return
            fmt = ((asset.get("metadata") or {}).get("format") or asset.get("name", "").rsplit(".", 1)[-1]).lower()
            if asset.get("resource_type") != "image" or fmt in ("pdf", "eps", "ai", "psd", "zip"):
                return
            u = asset.get("download_url") or asset.get("large_url") or ""
            m = re.match(r"(https://images\.salsify\.com/image/upload/).*?([^/]+\.\w+)$", u)
            if m:                  # full-size original (2000 x 2000), no download / resize transformation
                u = m.group(1) + m.group(2)
            if u.startswith("http") and u not in images:
                images.append(u)
        add(d.get("profile_asset"))
        assets = d.get("asset_properties") or []
        for word in IMAGE_GROUPS:
            for a in assets:
                n = (a.get("name") or a.get("id") or "").lower()
                if word in n and "instruction" not in n:
                    for v in a.get("values") or []:
                        add(v)

        ptype = val.get("Product Type") or ""
        pgroup = val.get("Product Group") or ""
        tags = [t for t in (val.get("Colour One"), pgroup, val.get("Main Material 1"),
                            "New" if (val.get("New Product") or "").lower() == "yes" else "") if t]
        ean = re.sub(r"\D", "", val.get("Barcode") or "")
        p = Product(
            supplier=self.name, url=self.url_for(code), title=title, handle=re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
            sku=code, ean=ean, brand="GFW", category=ptype, breadcrumbs=" > ".join(x for x in (pgroup, ptype) if x),
            tags=tags, price=price, rrp="", availability=availability, weight=weight, description=desc,
            description_text=text, meta_description=text[:300], images=images, specs=specs,
        )
        base = {"sku": code, "price": price, "compare_at_price": "", "barcode": ean, "weight": weight}
        p.option_names, p.variants = V.finalize([], [], base)
        p.variants[0].update({"barcode": ean, "qty": qty, "in_stock": (qty > 0) if qty is not None else True,
                              "image": images[0] if images else ""})
        return p

    def read_code(self, code):
        d = self.get(f"{API}/products/{requests.utils.quote(code, safe='')}")
        if not d or not d.get("id"):
            return None
        return self.build(d)

    # ------------------------------------------------------------------ STEP 1 + STEP 2 entry points
    def crawl(self, limit=None, known_urls=None):
        known = {self.norm(u) for u in (known_urls or [])}
        found = 0
        for code in self.product_codes():
            url = self.url_for(code)
            if self.norm(url) in known:
                continue
            self.stats["pages"] += 1
            try:
                p = self.read_code(code)
            except Exception as e:
                log.warning("[gfw] could not read %s: %s", code, str(e)[:120])
                p = None
            if not p:
                self.stats["failed"].append(url)
                continue
            found += 1
            self.stats["products"] += 1
            log.info("[gfw] #%d %s  (%s, %s, %d photos)", found, p.title, p.availability,
                     f"£{p.price}" if p.price else "no price", len(p.images))
            yield p
            if limit and found >= limit:
                return

    def read_product(self, url):
        """STEP 2: fresh price + stock of one known product."""
        code = url.rstrip("/").rsplit("/", 1)[-1]
        try:
            return self.read_code(requests.utils.unquote(code))
        except Exception as e:
            log.warning("[gfw] could not read %s: %s", url, str(e)[:120])
            return None
