"""Birlea Furniture - birleatradeshop.com (Sana Commerce trade shop, prices ex VAT after login).

How products are read (checked on the logged-in shop, Nov 2026):
  * Every page carries its full data as JSON in a <script> that starts with {"addons" (Sana's page state).
    The scraper fetches pages inside the logged-in browser and reads that JSON - no clicking, no guessing.
  * Product list: the category pages of the main menu, with ?count=1000 so one request returns the whole
    category (about 655 unique products in Nov 2026).
  * One product (state.page.product):
      id = SKU, title, description (HTML), media[] (every photo, large size), inventory (exact stock),
      price (trade price ex VAT), listPrice, specifications (Material, Height/Width/Depth, Range,
      Colour Group, Number of Boxes, Gross Weight, Home delivery type, GTIN), categoriesPaths (breadcrumbs).
  * Colours/sizes are separate products (own SKU) on Birlea, so products normally have no variants.
    If a product does have variants (hasVariants), each one is listed as a Shopify variant.
"""
import html
import logging
import re
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException

from core import variants as V
from core.base import BaseScraper
from core.models import Product

log = logging.getLogger("scraper")

# fetch a page inside the logged-in browser and return its Sana state: {userType, page, menu}
STATE_JS = r"""
const done = arguments[arguments.length - 1];
const withMenu = arguments[1];
fetch(arguments[0], {credentials: 'include'}).then(r => r.ok ? r.text() : '').then(h => {
  if (!h) { done(null); return; }
  const doc = new DOMParser().parseFromString(h, 'text/html');
  const s = [...doc.querySelectorAll('script:not([src])')].map(x => x.textContent).find(t => t.startsWith('{"addons"'));
  if (!s) { done({noState: true}); return; }
  const st = JSON.parse(s);
  const out = {userType: (st.user || {}).type || '', page: st.page || {}};
  if (withMenu) {
    const urls = [];
    const walk = items => (items || []).forEach(i => { if (i.link && i.link.url) urls.push(i.link.url); walk(i.children); });
    walk(((st.navigation || {}).CascadingMainMenu || {}).MAIN);
    out.menu = urls;
  }
  if (out.page.products) {     // a category page: only what the crawl needs
    out.page = {totalCount: out.page.totalCount, component: out.page.component,
                products: out.page.products.map(p => ({id: p.id, url: p.url, title: p.title}))};
  }
  done(out);
}).catch(e => done({error: String(e)}));
"""

SKIP_SPECS = {"gtin", "range", "colour group"}


def _title(s):
    """'LIVING + OCCASIONAL' -> 'Living + Occasional', 'TV UNITS' -> 'TV Units'."""
    words = []
    for w in (s or "").split():
        words.append(w if w in ("TV", "PC", "LED", "USB") or not w.isalpha() else w.capitalize())
    return " ".join(words)


class BirleaScraper(BaseScraper):
    name = "birlea"
    base_url = "https://birleatradeshop.com/"
    login_url = "https://birleatradeshop.com/profile/login"
    start_urls = ["https://birleatradeshop.com/"]
    walk_options = False

    # ------------------------------------------------------------------ data
    def state(self, path, menu=False):
        if not self.driver.current_url.startswith(self.base_url.rstrip("/")):
            self.driver.get(self.base_url)
            self.pause()
        for attempt in range(3):
            try:
                self.driver.set_script_timeout(120)
                st = self.driver.execute_async_script(STATE_JS, path, menu)
            except WebDriverException as e:
                log.warning("[birlea] could not read %s (%s)", path, str(e)[:80])
                st = None
            if st and not st.get("error"):
                return st
            self.pause(2)
        return None

    def is_logged_in(self):
        try:
            if not self.driver.current_url.startswith(self.base_url.rstrip("/")):
                return None
            t = self.driver.execute_script(
                "const s=[...document.querySelectorAll('script:not([src])')].map(x=>x.textContent)"
                ".find(t=>t.startsWith('{\"addons\"'));"
                "if(!s) return ''; try{return (JSON.parse(s).user||{}).type||''}catch(e){return ''}")
        except WebDriverException:
            return None
        if not t:
            return None
        return t == "REGISTERED"

    def _ensure_login(self, st):
        if st and st.get("userType") and st["userType"] != "REGISTERED":
            log.warning("[birlea] not logged in (no trade prices) - logging in again")
            self.login()
            return True
        return False

    def product_links(self):
        """Every product URL from the main-menu categories (whole category per request)."""
        st = self.state("/", menu=True)
        if self._ensure_login(st):
            st = self.state("/", menu=True)
        menu = (st or {}).get("menu") or []
        tops = []
        for u in menu:          # top categories cover their sub-categories; sub-cats only add safety
            if u.startswith("/") and u not in tops:
                tops.append(u)
        seen = {}
        for u in tops:
            page = (self.state(u + ("&" if "?" in u else "?") + "count=1000") or {}).get("page") or {}
            items = page.get("products") or []
            new = 0
            for it in items:
                if it.get("url") and it["url"] not in seen:
                    seen[it["url"]] = it
                    new += 1
            if items:
                log.info("[birlea] %s: %d products (%d new)", u, len(items), new)
        self.stats["sitemap_urls"] = len(seen)
        log.info("[birlea] %d products in the shop", len(seen))
        return [self.base_url.rstrip("/") + u for u in seen]

    # ------------------------------------------------------------------ one product
    def build(self, pr, url):
        abs_ = lambda u: (self.base_url.rstrip("/") + u) if u and u.startswith("/") else (u or "")
        specs_raw = [(s.get("name") or "", str(s.get("value") or "").strip()) for s in pr.get("specifications") or []]
        sp = {k.lower(): v for k, v in specs_raw}
        specs = {}
        for k, v in specs_raw:
            if not k or not v or k.lower() in SKIP_SPECS:
                continue
            if k.lower() == "gross weight" and re.fullmatch(r"[\d.]+", v):
                v += " kg"
            specs[k] = v
        if sp.get("range"):
            specs["Range"] = sp["range"]
        if sp.get("colour group"):
            specs["Colour"] = sp["colour group"]

        paths = [[c.get("name") for c in (p.get("categories") or []) if c.get("name")]
                 for p in pr.get("categoriesPaths") or []]
        path = max(paths, key=len) if paths else []
        crumbs = [_title(c) for c in path]
        category = crumbs[-1] if crumbs else ""

        images = []
        for m in pr.get("media") or []:
            if (m.get("type") or "ProductImage") != "ProductImage":
                continue
            u = abs_(m.get("large") or m.get("medium") or m.get("small"))
            if u and u not in images:
                images.append(u)
        if not images:
            img = pr.get("image") or {}
            u = abs_(img.get("large") or img.get("medium") or img.get("small"))
            if u:
                images.append(u.replace("/small/", "/large/").replace("/medium/", "/large/"))

        price = V.money(pr.get("price"))
        rrp = V.money(pr.get("listPrice"))
        if rrp and price and float(rrp) <= float(price):
            rrp = ""
        ean = re.sub(r"\D", "", sp.get("gtin", ""))
        weight = V.parse_weight_kg(sp.get("gross weight", "") + " kg") if sp.get("gross weight") else ""
        qty = pr.get("inventory")
        qty = int(qty) if isinstance(qty, (int, float)) else None
        if qty is not None and qty < 0:
            qty = 0
        if qty is None:
            availability = "In stock"
        elif qty > 0:
            availability = f"{qty} in stock"
        else:
            nxt = (pr.get("deliveryLeadTime") or "")[:10]
            availability = "Out of stock" + (f" (next delivery {nxt})" if nxt else "")

        desc = (pr.get("description") or "").strip()
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc))).strip()
        if desc and "<p" not in desc:
            desc = "".join(f"<p>{part.strip()}</p>" for part in re.split(r"(?:<br\s*/?>\s*){2,}", desc) if part.strip())

        tags = [t for t in (sp.get("range"), sp.get("colour group")) if t]
        p = Product(
            supplier=self.name, url=url, title=(pr.get("title") or "").strip(),
            handle=urlparse(url).path.strip("/"), sku=str(pr.get("id") or ""), ean=ean, brand="Birlea",
            category=category, breadcrumbs=" > ".join(crumbs), tags=tags, price=price, rrp=rrp,
            availability=availability, weight=weight, description=desc, description_text=text,
            meta_description=text[:300], images=images, specs=specs,
        )

        rows = []
        for v in pr.get("variants") or []:      # rare on Birlea; read defensively
            if not isinstance(v, dict):
                continue
            vq = v.get("inventory")
            vq = int(vq) if isinstance(vq, (int, float)) else qty
            label = v.get("title") or v.get("name") or v.get("id")
            rows.append({"options": [str(label)], "sku": str(v.get("id") or ""), "price": V.money(v.get("price")) or price,
                         "compare_at_price": rrp, "barcode": "", "weight": weight,
                         "image": abs_(((v.get("image") or {}) if isinstance(v.get("image"), dict) else {}).get("large")),
                         "in_stock": (vq or 0) > 0 if vq is not None else True, "qty": vq})
        base = {"sku": p.sku, "price": price, "compare_at_price": rrp, "barcode": ean, "weight": weight}
        p.option_names, p.variants = V.finalize(["Option"] if len(rows) > 1 else [], rows if len(rows) > 1 else [], base)
        if len(p.variants) == 1:
            p.variants[0].update({"barcode": ean, "in_stock": (qty > 0) if qty is not None else True, "qty": qty,
                                  "image": images[0] if images else ""})
        return p

    def read_url(self, url):
        path = urlparse(url).path or "/"
        st = self.state(path)
        if self._ensure_login(st):
            st = self.state(path)
        pr = ((st or {}).get("page") or {}).get("product")
        if not pr or not pr.get("id"):
            return None
        return self.build(pr, url)

    # ------------------------------------------------------------------ STEP 1 + STEP 2 entry points
    def crawl(self, limit=None, known_urls=None):
        known = {self.norm(u) for u in (known_urls or [])}
        found = 0
        for url in self.product_links():
            if self.norm(url) in known:
                continue
            self.stats["pages"] += 1
            try:
                p = self.read_url(url)
            except Exception as e:
                log.warning("[birlea] could not read %s: %s", url, str(e)[:120])
                p = None
            if not p:
                self.stats["failed"].append(url)
                continue
            found += 1
            self.stats["products"] += 1
            log.info("[birlea] #%d %s  (%s, %s, %d photos)", found, p.title, p.availability,
                     f"£{p.price}" if p.price else "no price", len(p.images))
            yield p
            if limit and found >= limit:
                return

    def read_product(self, url):
        """STEP 2: read one known product (fresh price + stock)."""
        try:
            return self.read_url(url)
        except Exception as e:
            log.warning("[birlea] could not read %s: %s", url, str(e)[:120])
            return None
