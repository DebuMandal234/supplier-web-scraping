"""Julian Bowen - trade.julian-bowen.co.uk (GOb2b shop, prices ex VAT after login).

How products are read (checked on the logged-in trade shop, Nov 2026):
  * Product list: every category of the main menu, read with the shop's own paging
    (/catalogue/department.aspx?node_id=<category>&pagesize=96&page=N).
  * One product = a "matrix" line (e.g. Agatha Dining Chair) with one SKU per finish/size. The shop's own data
    services (the ones its product page uses) give, per SKU:
      stock code (SKU), trade price (Net, ex VAT), RRP, exact free stock (e.g. 35), stock level
      (High / Low / BackOrder), next available quantity, finish/size options, GTIN when filled in.
  * /ajax/product-images.aspx  -> every photo of each SKU (1200 x 1200)
  * /ajax/product-tabs.aspx    -> Description (HTML) and Dimensions (Height / Width / Depth)
  * Breadcrumbs of the page   -> product type (e.g. "Dining Chairs") and tags.
All of it is fetched inside the logged-in browser (suppliers/jb_product.js) - no clicking through options.
"""
import html
import logging
import os
import re

from selenium.common.exceptions import WebDriverException

from core import variants as V
from core.base import BaseScraper
from core.models import Product

log = logging.getLogger("scraper")

with open(os.path.join(os.path.dirname(__file__), "jb_product.js"), encoding="utf-8") as _f:
    PRODUCT_JS = _f.read()

# all product links of one category (every page). arguments[0] = category path
CATEGORY_JS = r"""
const done = arguments[arguments.length - 1];
const cat = arguments[0];
(async () => {
  const parse = h => new DOMParser().parseFromString(h, 'text/html');
  const h = await fetch(cat, {credentials: 'include'}).then(r => r.ok ? r.text() : '');
  const node = (h.match(/department\.aspx\?node_id=([0-9a-f-]{36})/i) || [])[1];
  const prefix = cat.replace(/\/+$/, '').toLowerCase() + '/';
  const pick = html => [...parse(html).querySelectorAll('a[href]')].map(a => (a.getAttribute('href') || '').split('?')[0].split('#')[0])
    .map(u => u.replace(/^https?:\/\/[^/]+/i, ''))
    .filter(u => u.toLowerCase().startsWith(prefix) && u.slice(prefix.length).length && !u.slice(prefix.length).includes('/'));
  const links = new Set();
  if (!node) { pick(h).forEach(u => links.add(u)); done({node: null, links: [...links]}); return; }
  for (let page = 0; page < 60; page++) {
    const ph = await fetch('/catalogue/department.aspx?node_id=' + node + '&pagesize=96&page=' + page,
                           {credentials: 'include'}).then(r => r.ok ? r.text() : '');
    const found = pick(ph);
    const before = links.size;
    found.forEach(u => links.add(u));
    if (found.length < 96 || links.size === before) break;
  }
  done({node, links: [...links]});
})().catch(e => done({error: String(e)}));
"""

MENU_JS = r"""
return [...new Set([...document.querySelectorAll('a[href]')].map(a => (a.getAttribute('href') || '').replace(/^https?:\/\/trade\.julian-bowen\.co\.uk/i, '').split('?')[0])
  .filter(u => /^\/Catalogue\/[^/]+(\/[^/]+)?$/i.test(u) && !/About-Us|default\.aspx|results\.aspx|notify\.aspx/i.test(u)))];
"""

WEIGHT_RE = re.compile(r"(?:net\s+|gross\s+)?weight[^0-9]{0,15}(\d+(?:\.\d+)?)\s*(kg|kgs|g)\b", re.I)


class JulianBowenScraper(BaseScraper):
    """Trade portal lives on its own sub-domain: trade.julian-bowen.co.uk"""
    name = "julian_bowen"
    base_url = "https://trade.julian-bowen.co.uk/"
    login_url = "https://trade.julian-bowen.co.uk/login/"
    start_urls = ["https://trade.julian-bowen.co.uk/"]
    walk_options = False

    # ------------------------------------------------------------------ helpers
    def _on_shop_page(self):
        """The shop's data services are loaded on every catalogue page - make sure we are on one."""
        try:
            ok = self.driver.execute_script(
                "return !!(window.Aspidistra && Aspidistra.Ecommerce && Aspidistra.Ecommerce.Web.ScriptServices"
                " && Aspidistra.Ecommerce.Web.ScriptServices.ProductScriptService && window.aspidistra)")
        except WebDriverException:
            ok = False
        if not ok:
            self.driver.get(self.base_url.rstrip("/") + "/Catalogue/New-In")
            self.pause()

    def _async(self, js, *args, timeout=180):
        self._on_shop_page()
        try:
            self.driver.set_script_timeout(timeout)
            return self.driver.execute_async_script(js, *args)
        except WebDriverException as e:
            log.warning("[julian_bowen] browser script failed (%s)", str(e)[:100])
            return None

    def is_logged_in(self):
        try:
            if "julian-bowen" not in self.driver.current_url:
                return None
            v = self.driver.execute_script(
                "try{return aspidistra.utilities.getPageDetails().LoggedIn}catch(e){return null}")
        except WebDriverException:
            return None
        return v if isinstance(v, bool) else None

    # ------------------------------------------------------------------ product list
    def product_links(self):
        self.driver.get(self.base_url)
        self.pause()
        menu = self.driver.execute_script(MENU_JS) or []
        # sub-categories first (deepest category wins), then the top ones (New In, Mattresses, Home Office ...)
        menu.sort(key=lambda u: -u.count("/"))
        cats = set(u.lower() for u in menu)
        seen, out = set(), []
        for cat in menu:
            res = self._async(CATEGORY_JS, cat) or {}
            new = 0
            for u in res.get("links") or []:
                if u.lower() in cats:              # a sub-category link, not a product
                    continue
                key = u.rstrip("/").rsplit("/", 1)[-1].lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(self.base_url.rstrip("/") + u)
                new += 1
            if res.get("links"):
                log.info("[julian_bowen] %s: %d products (%d new)", cat, len(res["links"]), new)
        self.stats["sitemap_urls"] = len(out)
        log.info("[julian_bowen] %d products in the shop", len(out))
        return out

    # ------------------------------------------------------------------ one product
    def build(self, d, url):
        crumbs = [c for c in (d.get("crumbs") or []) if c]
        title = (d.get("title") or "").strip()
        if crumbs and crumbs[-1].strip().lower() == title.lower():
            crumbs = crumbs[:-1]
        category = crumbs[-1] if crumbs else ""
        tabs = d.get("tabs") or {}
        desc_tab = next((v for k, v in tabs.items() if "descr" in k.lower()), None) or {}
        dims_tab = next((v for k, v in tabs.items() if "dimension" in k.lower()), None) or {}
        desc = (desc_tab.get("html") or "").strip()
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc))).strip()

        specs = {}
        for line in dims_tab.get("lines") or []:
            m = re.match(r"^([^:]{1,40}):\s*(.+)$", line)
            if m:
                specs[m.group(1).strip()] = m.group(2).strip()
        weight = ""
        m = WEIGHT_RE.search(text) or WEIGHT_RE.search(" ".join(specs.values()))
        if m:
            weight = f"{float(m.group(1)) / (1000 if m.group(2).lower() == 'g' else 1):g}"
            specs.setdefault("Weight", f"{weight} kg")

        vs = d.get("variants") or []
        names = []
        for v in vs:
            for n, _ in v.get("options") or []:
                if n not in names:
                    names.append(n)
        names = names[:3]
        images, rows = [], []
        for v in vs:
            imgs = [u for u in (v.get("images") or []) if u]
            if not imgs and v.get("image"):
                imgs = [v["image"]]
            for u in imgs:
                if u not in images:
                    images.append(u)
            opts = dict(v.get("options") or [])
            price = V.money(v.get("price"))
            rrp = V.money(v.get("rrp"))
            if rrp and price and float(rrp) <= float(price):
                rrp = ""
            qty = v.get("freeStock")
            qty = max(int(qty), 0) if isinstance(qty, (int, float)) else None
            rows.append({"options": [opts.get(n, "") for n in names], "sku": v.get("sku") or "",
                         "price": price, "compare_at_price": rrp,
                         "barcode": re.sub(r"\D", "", v.get("gtin") or ""), "weight": weight,
                         "image": imgs[0] if imgs else "",
                         "in_stock": (qty > 0) if qty is not None else bool(v.get("purchasable")),
                         "qty": qty, "_v": v})

        first = rows[0] if rows else {}
        fv = first.get("_v") or {}
        if len(rows) == 1:                     # one finish only: keep it as a spec, no Shopify options
            for n, val in fv.get("options") or []:
                specs.setdefault(n, val)
        q = first.get("qty")
        if q is not None and q > 0:
            availability = f"{q} in stock"
        elif fv.get("stockLevel", "").lower() == "backorder" or fv.get("stockMessage", "").lower().startswith("back"):
            nq = fv.get("nextQty") or ""
            try:
                nq = int(float(nq))
            except ValueError:
                nq = ""
            availability = "Back order" + (f" ({nq} due{' ' + fv['dueIn'] if fv.get('dueIn') else ''})" if nq else "")
        elif q == 0:
            availability = "Out of stock"
        else:
            availability = fv.get("stockMessage") or "In stock"

        brand = fv.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("Name") or brand.get("name") or ""
        tags = [str(brand).strip()] if brand and str(brand).strip().lower() != "julian bowen" else []
        p = Product(
            supplier=self.name, url=url, title=title, handle=url.rstrip("/").rsplit("/", 1)[-1].lower(),
            sku=first.get("sku", ""), ean=first.get("barcode", ""), brand="Julian Bowen",
            category=category, breadcrumbs=" > ".join(crumbs), tags=tags,
            price=first.get("price", ""), rrp=first.get("compare_at_price", ""), availability=availability,
            weight=weight, description=desc, description_text=text, meta_description=text[:300],
            images=images, specs=specs,
        )
        for r in rows:
            r.pop("_v", None)
        multi = len(rows) > 1 and names
        base = {"sku": p.sku, "price": p.price, "compare_at_price": p.rrp, "barcode": p.ean, "weight": weight}
        p.option_names, p.variants = V.finalize(names if multi else [], rows if multi else [], base)
        if multi:
            for v, r in zip(p.variants, rows):
                v["qty"], v["in_stock"], v["barcode"] = r["qty"], r["in_stock"], r["barcode"]
        elif p.variants and rows:
            p.variants[0].update({k: rows[0][k] for k in ("qty", "in_stock", "barcode", "image")})
        return p

    def read_url(self, url):
        path = re.sub(r"^https?://[^/]+", "", url) or "/"
        d = self._async(PRODUCT_JS, path)
        if d and d.get("loggedIn") is False:
            log.warning("[julian_bowen] not logged in (no trade prices) - logging in again")
            self.login()
            d = self._async(PRODUCT_JS, path)
        if not d or d.get("error") or d.get("notProduct") or d.get("status") or not d.get("variants"):
            if d and d.get("error"):
                log.warning("[julian_bowen] %s: %s", url, d["error"][:150])
            return None
        return self.build(d, url)

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
                log.warning("[julian_bowen] could not read %s: %s", url, str(e)[:120])
                p = None
            if not p:
                self.stats["failed"].append(url)
                continue
            found += 1
            self.stats["products"] += 1
            log.info("[julian_bowen] #%d %s  (%s, %s, %d variants, %d photos)", found, p.title, p.availability,
                     f"£{p.price}" if p.price else "no price", len(p.variants), len(p.images))
            yield p
            if limit and found >= limit:
                return

    def read_product(self, url):
        """STEP 2: read one known product (fresh price + stock of every finish)."""
        try:
            return self.read_url(url)
        except Exception as e:
            log.warning("[julian_bowen] could not read %s: %s", url, str(e)[:120])
            return None
