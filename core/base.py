import json
import logging
import os
import re
import time
from collections import Counter, deque
from urllib.parse import urldefrag, urlparse

import requests
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import config
from core import variants as V
from core.models import Product
from core.payload import slugify

log = logging.getLogger("scraper")

PRICE_RE = re.compile(r"£\s?([\d,]+(?:\.\d{1,2})?)")
LOWER = "translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"

SPECS_JS = r"""
const out = {};
const root = document.querySelector('main') || document.body;
root.querySelectorAll('table tr').forEach(tr => {
  const c = tr.querySelectorAll('th,td');
  if (c.length === 2) {
    const k = c[0].innerText.trim().replace(/:$/, ''), v = c[1].innerText.trim();
    if (k && v && k.length < 60) out[k] = v;
  }
});
root.querySelectorAll('dt').forEach(dt => {
  const dd = dt.nextElementSibling;
  if (dd && dd.tagName === 'DD') {
    const k = dt.innerText.trim().replace(/:$/, ''), v = dd.innerText.trim();
    if (k && v && k.length < 60) out[k] = v;
  }
});
root.querySelectorAll('li, p').forEach(el => {
  const t = el.innerText.trim();
  const m = t.match(/^([A-Za-z][A-Za-z0-9 \/()&.-]{1,40}):\s*(.+)$/);
  if (m && t.length < 250 && !el.querySelector('li')) out[m[1].trim()] = m[2].trim();
});
return out;
"""

OPTIONS_JS = r"""
const out = {};
document.querySelectorAll('select').forEach(s => {
  const label = (s.getAttribute('aria-label') || s.name || s.id || '').trim();
  const vals = Array.from(s.options).map(o => o.text.trim())
      .filter(t => t && !/^(choose|select|please|--)/i.test(t));
  if (vals.length && label) out[label] = vals;
});
return out;
"""

WOO_JS = r"""
const f = document.querySelector('form.variations_form');
if (!f) return null;
let vars = null;
try { vars = JSON.parse(f.getAttribute('data-product_variations') || 'null'); } catch (e) {}
const attrs = [];
f.querySelectorAll('select[name^="attribute_"]').forEach(s => {
  let label = '';
  const row = s.closest('tr');
  if (row) { const l = row.querySelector('label, th'); if (l) label = l.innerText.trim(); }
  if (!label) label = s.name.replace('attribute_pa_', '').replace('attribute_', '').replace(/-/g, ' ');
  const values = {};
  Array.from(s.options).forEach(o => { if (o.value) values[o.value] = o.text.trim(); });
  attrs.push({name: s.name, label: label, values: values});
});
return {variations: vars, attrs: attrs};
"""

SKU_RE = re.compile(r"\bSKU\b\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-_/.]{2,})")
PRICE_JUNK_RE = re.compile(r"deliver|shipping|spend|free|minimum|over\s*£|orders?\s+over|save\b", re.I)
BRAND_JS = ("const h=document.querySelector('h1'); const p=h&&h.previousElementSibling; "
            "return p?p.innerText.trim():'';")
LINKS_JS = "return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);"
PAGINATION_CSS = (".pages a, .pages-items a, .pagination a, .page-numbers a, nav[aria-label*=agination] a, ul.pager a, .pager a, "
                  ".woocommerce-pagination a, a[rel=next]")
SITEMAP_CHILD_RE = re.compile(r"product|categor|collection|shop|catalog|range", re.I)


def parse_sitemap(text):
    """-> (is_index, [urls])  from sitemap XML text."""
    locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", text, re.S)
    return ("<sitemapindex" in text), locs


class BaseScraper:
    """Generic login + full crawl (sitemap + categories + all pagination) + extract.
    A supplier subclass only sets attributes (URLs / selectors) and can override any method."""

    name = ""
    base_url = ""
    login_url = ""
    start_urls: list = []
    login_link_texts: list = []
    user_selector = None
    pass_selector = None
    submit_selector = None
    product_url_regex = None
    next_selectors: list = []
    use_sitemap = True
    walk_options = True               # read variants by switching the option menus next to "Add to basket"
    # Crawl order / link options (defaults = old behaviour, so working suppliers are unchanged)
    sitemap_first = False             # True: visit every sitemap URL before following menu/category links
    prioritise_product_links = True   # push product-looking links of a listing to the front of the queue
    listing_product_selector = None   # CSS of product links on a category page: each one is opened, read and
                                      # saved to Excel right away, before the next category page
    warn_suspects = True              # save debug files for pages that look like products but were not recognised
    follow_query_links = True         # False: ignore ?filter=... links (layered navigation); ?p=N pages are kept
    PAGE_QUERY_RE = re.compile(r"^[?&]?(p|page|pg)=\d+$", re.I)
    # URLs matching this are never followed
    exclude_regex = re.compile(
        r"(/(login|logout|log-out|sign-?in|register|my-account|account|cart|basket|checkout|wishlist|"
        r"contact(-us)?|privacy[^/]*|terms[^/]*|cookie[^/]*|delivery[^/]*|returns[^/]*|blog|news|about-us|"
        r"faq|careers|press)(/|$|\?)"
        r"|\.(pdf|jpe?g|png|gif|webp|zip|svg)(\?|$)|[?&](add-to-cart|orderby|add_to_wishlist)=)", re.I)
    # CSS selector overrides: title, price, rrp, sku, description, short_description, images, category
    selectors: dict = {}

    def __init__(self, driver):
        self.driver = driver
        self.product_shapes = set()   # URL "shapes" known to be product pages (learned while crawling)
        self.stats = {"pages": 0, "listing_pages": 0, "products": 0, "sitemap_urls": 0, "failed": []}

    # ------------------------------------------------------------------ helpers
    def pause(self, mult=1.0):
        time.sleep(config.PAGE_DELAY * mult)

    def _visible(self, css):
        for el in self.driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except WebDriverException:
                continue
        return None

    def _xpath_text(self, text, tags="self::a or self::button"):
        return f"//*[{tags}][contains({LOWER},'{text.lower()}')]"

    def dismiss_cookies(self):
        for text in ("accept all", "accept", "allow all", "agree"):
            try:
                for el in self.driver.find_elements(By.XPATH, self._xpath_text(text))[:3]:
                    if el.is_displayed():
                        el.click()
                        time.sleep(0.5)
                        return
            except WebDriverException:
                pass

    def _click_link_text(self, text):
        for el in self.driver.find_elements(By.XPATH, self._xpath_text(text)):
            try:
                if el.is_displayed():
                    el.click()
                    return True
            except WebDriverException:
                continue
        return False

    # ------------------------------------------------------------------ login
    ERROR_RE = re.compile(r"(incorrect|invalid|wrong (user|email|password)|locked|unsuccessful|login failed|"
                          r"failed to log|try again|not recogni[sz]ed|does not match)", re.I)
    _LOWER_V = ("translate(concat(normalize-space(.),' ',@value),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')")

    def _body_text(self):
        try:
            return self.driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            return ""

    def _find_user_field(self, pw_el):
        """The closest visible text/email box BEFORE the password box (never the site's search box)."""
        if self.user_selector:
            return self._visible(self.user_selector)
        try:
            cands = pw_el.find_elements(By.XPATH, "preceding::input")
        except WebDriverException:
            cands = []
        for el in reversed(cands):
            try:
                t = (el.get_attribute("type") or "text").lower()
                hint = " ".join((el.get_attribute(a) or "") for a in
                                ("name", "id", "placeholder", "aria-label", "class")).lower()
                if t in ("text", "email", "tel") and "search" not in hint and el.is_displayed() and el.is_enabled():
                    return el
            except WebDriverException:
                continue
        return None

    def _fill(self, el, value):
        el.clear()
        el.send_keys(value)
        if (el.get_attribute("value") or "") != value:   # some sites ignore typing: set it directly
            self.driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", el, value)

    def _click_submit_after(self, pw_el):
        """Click the first 'login / sign in' button that comes after the password box."""
        xp = ("./following::*[(self::button or self::a or self::input[@type='submit' or @type='button']) and "
              f"(contains({self._LOWER_V},'login') or contains({self._LOWER_V},'log in') or "
              f"contains({self._LOWER_V},'sign in') or contains({self._LOWER_V},'submit'))]")
        for el in pw_el.find_elements(By.XPATH, xp):
            try:
                if el.is_displayed():
                    el.click()
                    return True
            except WebDriverException:
                continue
        return False

    def login(self):
        user, pw = config.credentials(self.name)
        if not user or not pw:
            raise RuntimeError(f"Missing {self.name.upper()}_USERNAME / _PASSWORD in .env")
        d = self.driver
        d.get(self.login_url or self.base_url)
        self.pause()
        self.dismiss_cookies()
        if self.is_logged_in() is True:          # session still valid (e.g. re-login during the crawl)
            log.info("[%s] already logged in", self.name)
            return

        pw_css = self.pass_selector or "input[type=password]"
        pw_el = self._visible(pw_css)
        if not pw_el:
            for text in self.login_link_texts + ["trade login", "log in", "login", "sign in", "my account"]:
                if self._click_link_text(text):
                    self.pause()
                    pw_el = self._visible(pw_css)
                    if pw_el:
                        break
        if not pw_el:
            self.dump_debug("login_no_form")
            raise RuntimeError(f"[{self.name}] could not find the login form. See the data/debug folder.")

        user_el = self._find_user_field(pw_el)
        if not user_el:
            self.dump_debug("login_no_user_field")
            raise RuntimeError(f"[{self.name}] could not find the username field")
        self._fill(user_el, user)
        self._fill(pw_el, pw)

        had_error_before = bool(self.ERROR_RE.search(self._body_text()))
        submit = self._visible(self.submit_selector) if self.submit_selector else None
        if submit:
            submit.click()
        else:
            pw_el.send_keys(Keys.RETURN)

        # Wait for the password box to disappear (= logged in). With a visible browser you get up to
        # 2 minutes to solve a captcha / 2FA by hand. A clear "incorrect password" message stops it at once.
        limit = 15 if config.HEADLESS else 120
        t0 = time.time()
        clicked = False
        while time.time() - t0 < limit:
            time.sleep(1)
            state = self.is_logged_in()
            if state is True or (state is None and not self._visible("input[type=password]")):
                log.info("[%s] logged in", self.name)
                self.pause()
                return
            elapsed = time.time() - t0
            if elapsed >= 3:
                body = self._body_text()
                m = self.ERROR_RE.search(body)
                if m and not had_error_before:
                    line = next((l.strip() for l in body.splitlines() if self.ERROR_RE.search(l)), m.group(0))
                    self.dump_debug("login_rejected")
                    raise RuntimeError(
                        f"[{self.name}] the website rejected the login: \"{line[:200]}\". "
                        f"Check {self.name.upper()}_USERNAME / _PASSWORD in .env by logging in by hand in Chrome.")
            if elapsed >= 4 and not clicked:
                clicked = True
                try:
                    if (pw_el.get_attribute("value") or ""):   # Enter did nothing -> click the button
                        self._click_submit_after(pw_el)
                except WebDriverException:
                    pass  # page already reloaded = the form was submitted
        self.dump_debug("login_failed")
        raise RuntimeError(f"[{self.name}] login did not complete (wrong credentials, captcha or 2FA?)")

    def is_logged_in(self):
        """Site-specific check: True / False, or None = unknown (then the password-box test is used)."""
        return None

    def is_logged_out(self):
        """Called on every crawled page: has the session expired?"""
        state = self.is_logged_in()
        if state is not None:
            return not state
        return bool(re.search(r"login|sign-?in|account", self.driver.current_url, re.I)
                    and self._visible("input[type=password]"))

    def dump_debug(self, label):
        os.makedirs(os.path.join(config.DATA_DIR, "debug"), exist_ok=True)
        base = os.path.join(config.DATA_DIR, "debug", f"{self.name}_{label}")
        try:
            self.driver.save_screenshot(base + ".png")
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
        except WebDriverException:
            pass

    # ------------------------------------------------------------------ URL helpers
    def same_site(self, url):
        host = urlparse(self.base_url or self.start_urls[0]).netloc.replace("www.", "")
        return urlparse(url).netloc.replace("www.", "") == host

    def allowed(self, url):
        return url.startswith("http") and self.same_site(url) and not self.exclude_regex.search(url)

    @staticmethod
    def norm(url):
        """Identity of a product page: host + path, no query / fragment / trailing slash."""
        p = urlparse(urldefrag(url)[0])
        return f"{p.scheme}://{p.netloc.replace('www.', '')}{p.path.rstrip('/')}".lower()

    def canonical_url(self):
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, "link[rel=canonical]")
            href = els[0].get_attribute("href") if els else ""
            if href and href.startswith("http") and self.same_site(href):
                return href
        except WebDriverException:
            pass
        return urldefrag(self.driver.current_url)[0].split("?")[0]

    def page_links(self):
        try:
            links = self.driver.execute_script(LINKS_JS)
        except WebDriverException:
            return []
        out = []
        for l in links:
            l = urldefrag(l)[0]
            if not self.allowed(l):
                continue
            q = urlparse(l).query
            if q and not self.follow_query_links and not self.PAGE_QUERY_RE.match(q):
                continue   # filter / sort link of a category page: same products again
            out.append(l)
        return out

    @staticmethod
    def shape(url):
        """URL pattern: (number of path segments, first segment if deeper than 1). Products of a shop
        nearly always share one shape, e.g. (1,'*') for /some-bed-name or (2,'product') for /product/x."""
        segs = [s for s in urlparse(url).path.split("/") if s]
        return (len(segs), "*") if len(segs) <= 1 else (len(segs), segs[0].lower())

    def order_sitemap_urls(self, urls):
        """Biggest URL-shape group first (that is the product group), keeping sitemap order inside a group.
        This way products are visited before the few category / brand / info pages."""
        if not urls:
            return urls
        counts = Counter(self.shape(u) for u in urls)
        top, n = counts.most_common(1)[0]
        if n >= 20 and n / len(urls) >= 0.3:
            self.product_shapes.add(top)
        return sorted(urls, key=lambda u: -counts[self.shape(u)])

    # ------------------------------------------------------------------ sitemap
    def discover_sitemap_urls(self):
        """Read robots.txt / sitemap.xml (public or with our login cookies). Returns same-site URLs."""
        if not self.use_sitemap:
            return []
        s = requests.Session()
        try:
            for c in self.driver.get_cookies():
                s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            s.headers["User-Agent"] = self.driver.execute_script("return navigator.userAgent")
        except WebDriverException:
            pass
        p = urlparse(self.base_url)
        root = f"{p.scheme}://{p.netloc}"
        todo = []
        try:
            r = s.get(root + "/robots.txt", timeout=20)
            if r.ok:
                todo += [(u, 0) for u in re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)]
        except requests.RequestException:
            pass
        todo += [(root + path, 0) for path in
                 ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/product-sitemap.xml")]
        done, urls = set(), []
        while todo and len(urls) < 100000:
            url, depth = todo.pop(0)
            if url in done or depth > 3:
                continue
            done.add(url)
            try:
                r = s.get(url, timeout=30)
            except requests.RequestException:
                continue
            if not r.ok or "<loc>" not in r.text:
                continue
            is_index, locs = parse_sitemap(r.text)
            if is_index:
                preferred = [l for l in locs if SITEMAP_CHILD_RE.search(l)]
                todo += [(l, depth + 1) for l in (preferred or locs)]
            else:
                urls += locs
        out = list(dict.fromkeys(u for u in urls if self.allowed(u)))
        log.info("[%s] sitemap: %d URLs", self.name, len(out))
        return out

    # ------------------------------------------------------------------ listing helpers
    def expand_listing(self):
        """Lazy-loading / 'Load more' listings: scroll down and click load-more buttons."""
        d = self.driver
        try:
            last = 0
            for _ in range(10):
                d.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.8)
                for text in ("load more", "show more", "view more"):
                    for el in d.find_elements(By.XPATH, self._xpath_text(text)):
                        if el.is_displayed():
                            el.click()
                            time.sleep(1.2)
                h = d.execute_script("return document.body.scrollHeight")
                if h == last:
                    break
                last = h
        except WebDriverException:
            pass

    def pagination_links(self):
        """All pagination links on the page (numbered pages and 'next'), in page order."""
        d = self.driver
        out = []
        css = ", ".join(self.next_selectors + [PAGINATION_CSS]) if self.next_selectors else PAGINATION_CSS
        try:
            for el in d.find_elements(By.CSS_SELECTOR, css):
                href = el.get_attribute("href")
                if href:
                    href = urldefrag(href)[0]
                    if self.allowed(href) and href != urldefrag(d.current_url)[0] and href not in out:
                        out.append(href)
        except WebDriverException:
            pass
        for text in ("next", "›", "»"):
            try:
                for el in d.find_elements(By.XPATH, self._xpath_text(text, "self::a")):
                    href = el.get_attribute("href")
                    if href:
                        href = urldefrag(href)[0]
                        if self.allowed(href) and href != urldefrag(d.current_url)[0] and href not in out:
                            out.append(href)
            except WebDriverException:
                continue
        return out

    def listing_product_links(self):
        """Product links of the current category page (only when listing_product_selector is set)."""
        if not self.listing_product_selector:
            return []
        out = []
        try:
            for el in self.driver.find_elements(By.CSS_SELECTOR, self.listing_product_selector):
                href = el.get_attribute("href")
                if href:
                    href = urldefrag(href)[0]
                    if self.allowed(href) and href not in out:
                        out.append(href)
        except WebDriverException:
            pass
        return out

    # ------------------------------------------------------------------ product page detection
    def _looks_like_product(self):
        d = self.driver
        try:
            if not d.find_elements(By.CSS_SELECTOR, "h1"):
                return False
            xp = (f"//*[self::button or self::a][contains({LOWER},'add to basket') or "
                  f"contains({LOWER},'add to cart') or contains({LOWER},'add to bag')]")
            btns = [e for e in d.find_elements(By.XPATH, xp) if e.is_displayed()]
            if not (1 <= len(btns) <= 2):
                return False
            return bool(PRICE_RE.search(d.find_element(By.TAG_NAME, "body").text))
        except WebDriverException:
            return False

    def product_signals(self):
        """What the current page looks like (used for detection and for debugging with `probe`)."""
        d = self.driver
        sig = {}
        try:
            body = d.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            body = ""
        sig["url_regex"] = bool(self.product_url_regex and re.search(self.product_url_regex, d.current_url))
        sig["json_ld_product"] = bool(self.jsonld_product())
        sig["og_type"] = self._meta("meta[property='og:type'], meta[name='og:type']")
        try:
            sig["h1"] = bool(d.find_elements(By.CSS_SELECTOR, "h1"))
            xp = (f"//*[self::button or self::a][contains({LOWER},'add to basket') or "
                  f"contains({LOWER},'add to cart') or contains({LOWER},'add to bag')]")
            sig["add_to_cart_buttons"] = len([x for x in d.find_elements(By.XPATH, xp) if x.is_displayed()])
        except WebDriverException:
            sig.setdefault("h1", False)
            sig["add_to_cart_buttons"] = 0
        sig["sku_mentions"] = len(SKU_RE.findall(body))   # a category page shows many, a product page 1-2
        sig["price_text"] = bool(PRICE_RE.search(body))
        return sig

    def is_product_page(self):
        sig = self.product_signals()
        self.last_signals = sig
        if sig["url_regex"] or sig["json_ld_product"] or sig["og_type"] == "product":
            return True
        if sig["h1"] and sig["sku_mentions"] in (1, 2) and (sig["price_text"] or sig["add_to_cart_buttons"]):
            return True
        return bool(sig["h1"] and 1 <= sig["add_to_cart_buttons"] <= 2 and sig["price_text"])

    # ------------------------------------------------------------------ crawl
    def crawl(self, limit=None, known_urls=None):
        """Visit every category / listing page (following ALL pagination links), plus every URL in the
        sitemap, and yield a Product for each product page found. Already-known products are skipped."""
        known = {self.norm(u) for u in (known_urls or [])}
        queue = deque()
        seen = set()
        visited = set()

        def push(u, front=False, move=False):
            """Queue a URL. move=True: if it is already waiting further back, bring it to the front."""
            u = urldefrag(u)[0]
            if not u or u in visited:
                return
            if u in seen:
                if not (move and front):
                    return
                try:
                    queue.remove(u)
                except ValueError:
                    return
            seen.add(u)
            (queue.appendleft if front else queue.append)(u)

        sitemap_urls = self.discover_sitemap_urls()
        self.stats["sitemap_urls"] = len(sitemap_urls)
        firsts = list(self.start_urls or [self.base_url])
        ordered = self.order_sitemap_urls(sitemap_urls)
        for u in ((ordered + firsts) if self.sitemap_first else (firsts + ordered)):
            push(u)

        found, relogins = 0, 0
        while queue and self.stats["pages"] < config.MAX_PAGES and (limit is None or found < limit):
            url = queue.popleft()
            visited.add(url)
            if self.norm(url) in known:
                continue  # product already scraped earlier
            try:
                self.driver.get(url)
            except WebDriverException as e:
                log.warning("skip %s (%s)", url, str(e)[:80])
                self.stats["failed"].append(url)
                continue
            self.stats["pages"] += 1
            self.pause()
            if self.stats["pages"] == 1:
                self.dismiss_cookies()

            # session expired -> log in again
            if relogins < 3 and self.is_logged_out():
                relogins += 1
                log.warning("[%s] logged out - logging in again (%d)", self.name, relogins)
                try:
                    self.login()
                    queue.appendleft(url)
                    continue
                except Exception as e:
                    log.error("[%s] re-login failed: %s", self.name, e)
                    break

            try:
                is_product = self.is_product_page()
            except Exception as ex:
                log.warning("[%s] could not analyse %s: %s", self.name, url, str(ex)[:100])
                self.stats["failed"].append(url)
                continue
            if is_product:
                key = self.norm(self.canonical_url())
                if key in known:
                    continue
                known.add(key)
                self.product_shapes.add(self.shape(self.canonical_url()))
                try:
                    p = self.extract_product()
                except Exception as e:
                    log.warning("extract failed on %s: %s", url, e)
                    self.stats["failed"].append(url)
                    continue
                if p and p.title:
                    found += 1
                    self.stats["products"] = found
                    log.info("[%s] #%d %s  (%d variants)", self.name, found, p.title, len(p.variants))
                    yield p
                continue  # product pages don't need link expansion

            # listing / category page
            self.stats["listing_pages"] += 1
            log.info("[%s] category/listing page: %s", self.name, url)
            if self.warn_suspects and self.shape(url) in self.product_shapes and self.stats.get("suspects", 0) < 3 \
                    and len(urlparse(url).path.strip("/").split("/")) == 1 and self.stats["listing_pages"] > 5:
                # looks like a product URL but was not recognised: save evidence for debugging
                self.stats["suspects"] = self.stats.get("suspects", 0) + 1
                log.warning("[%s] page has a product-like URL but was not recognised as a product (%s). "
                            "Saved to data/debug/%s_suspect_%d.*", self.name,
                            getattr(self, "last_signals", {}), self.name, self.stats["suspects"])
                self.dump_debug(f"suspect_{self.stats['suspects']}")
            self.expand_listing()
            tiles = self.listing_product_links()
            for l in reversed(self.pagination_links()):
                push(l, front=True)  # finish this category's pages before moving on
            new_tiles = [l for l in tiles if self.norm(l) not in known]
            if tiles:
                log.info("[%s]   %d products on this page, %d new -> opening each one now",
                         self.name, len(tiles), len(new_tiles))
            for l in reversed(new_tiles):
                push(l, front=True, move=True)  # this page's products first: open, read, save, next
            links = self.page_links()
            if self.prioritise_product_links:
                hot = [l for l in links if self.shape(l) in self.product_shapes and self.norm(l) not in known]
                for l in reversed(hot):
                    push(l, front=True)  # product-looking links of this page: visit them first
            for l in links:
                push(l)
            if self.stats["pages"] % 25 == 0:
                log.info("[%s] visited %d pages, %d queued, %d products", self.name,
                         self.stats["pages"], len(queue), found)

    # ------------------------------------------------------------------ step 2: open one known product directly
    def read_product(self, url):
        """Open a product link directly (no category browsing) and read it.
        Returns a Product, or None when the page is gone / no longer a product page."""
        for attempt in range(2):
            try:
                self.driver.get(url)
            except WebDriverException as e:
                log.warning("[%s] could not open %s (%s)", self.name, url, str(e)[:80])
                return None
            self.stats["pages"] += 1
            self.pause()
            if self.stats["pages"] == 1:
                self.dismiss_cookies()
            if attempt == 0 and self.is_logged_out():
                log.warning("[%s] logged out - logging in again", self.name)
                self.login()
                continue
            break
        try:
            if not self.is_product_page():
                return None
            p = self.extract_product()
        except Exception as e:
            log.warning("[%s] could not read %s: %s", self.name, url, str(e)[:120])
            return None
        return p if p and p.title else None

    # ------------------------------------------------------------------ extract
    def jsonld_product(self):
        try:
            scripts = self.driver.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json']")
        except WebDriverException:
            return None
        for s in scripts:
            try:
                data = json.loads(s.get_attribute("textContent") or "")
            except (ValueError, WebDriverException):
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if "@graph" in node:
                        stack.extend(node["@graph"])
                    t = node.get("@type")
                    if t in ("Product", "ProductGroup") or (isinstance(t, list) and ("Product" in t or "ProductGroup" in t)):
                        return node
                elif isinstance(node, list):
                    stack.extend(node)
        return None

    def _text(self, css):
        if not css:
            return ""
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            return els[0].text.strip() if els else ""
        except WebDriverException:
            return ""

    def _html(self, css):
        if not css:
            return ""
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            return els[0].get_attribute("innerHTML").strip() if els else ""
        except WebDriverException:
            return ""

    def _meta(self, css):
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            return (els[0].get_attribute("content") or "").strip() if els else ""
        except WebDriverException:
            return ""

    @staticmethod
    def _price_from(text):
        m = PRICE_RE.search(text or "") or re.search(r"([\d,]+\.\d{2})", text or "")
        return m.group(1).replace(",", "") if m else ""

    def best_price(self):
        """Price of THIS product: price attributes first, then price boxes, then the first price-looking line
        that is not about delivery / offers."""
        d = self.driver
        for css, attr in (("[data-price-amount]", "data-price-amount"),
                          ("meta[property='product:price:amount']", "content"),
                          ("meta[itemprop=price]", "content"), ("[itemprop=price]", "content")):
            try:
                for el in d.find_elements(By.CSS_SELECTOR, css)[:3]:
                    v = V.money(el.get_attribute(attr) or el.text)
                    if v:
                        return v
            except WebDriverException:
                pass
        for css in (".price-box .price", ".product-info-price .price", ".product-price", ".price"):
            p = self._price_from(self._text(css))
            if p:
                return p
        try:
            body = d.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            return ""
        return self.price_from_lines(body)

    @staticmethod
    def price_from_lines(body):
        for line in (body or "").splitlines():
            if PRICE_RE.search(line) and not PRICE_JUNK_RE.search(line):
                m = PRICE_RE.search(line)
                return m.group(1).replace(",", "")
        return ""

    def extract_variants(self, ld, base):
        """Sizes / colours / options of the product - same readers for every supplier (core/variant_sources.py)."""
        from core import variant_sources as VS
        d = self.driver
        names, rows = VS.shopify_variants(d)                       # 1. Shopify stores
        if not rows:
            try:
                woo = d.execute_script(WOO_JS)
            except WebDriverException:
                woo = None
            if woo and isinstance(woo.get("variations"), list) and woo["variations"] and woo.get("attrs"):
                names, rows = V.woo_variants(woo["attrs"], woo["variations"])   # 2a. WooCommerce form
        if not rows:
            names, rows = VS.woovr_variants(d)                      # 2b. WooCommerce radio-button list
        if not rows and ld.get("hasVariant"):
            names, rows = V.ld_variants(ld)                         # 3. JSON-LD
        if not rows and self.walk_options:
            names, rows = VS.walk_options(self)                     # 4. click through the option menus
        if not rows and VS.page_has_options(d):
            n = self.stats.get("variants_missing", 0) + 1
            self.stats["variants_missing"] = n
            if n <= 3:
                self.dump_debug(f"variants_missing_{n}")
                log.warning("[%s] this product has options but they could not be read - page saved as "
                            "data/debug/%s_variants_missing_%d.html (send it to Claude)", self.name, self.name, n)
        if rows:
            log.info("[%s]   %d variants (%s)", self.name, len(rows), ", ".join(names))
        return V.finalize(names, rows, base)

    def extract_product(self):
        d = self.driver
        ld = self.jsonld_product() or {}
        sel = self.selectors

        title = self._text(sel.get("title")) or ld.get("name") or self._text("h1") or self._meta("meta[property='og:title']")
        sku = self._text(sel.get("sku")) or str(ld.get("sku") or ld.get("mpn") or "")
        sku = re.sub(r"^(sku|product code|code|ref)[:\s]+", "", sku.strip(), flags=re.I)

        desc_html = self._html(sel.get("description"))
        desc_text = self._text(sel.get("description"))
        if not desc_html:
            desc_html = ld.get("description") or ""
            desc_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc_html)).strip()
        short = self._text(sel.get("short_description"))
        meta_desc = self._meta("meta[name='description']") or self._meta("meta[property='og:description']")

        price = self._price_from(self._text(sel.get("price")))
        offers = ld.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        offers = offers if isinstance(offers, dict) else {}
        if not price:
            price = str(offers.get("price") or offers.get("lowPrice") or "")
        if not price:
            price = self.best_price()
        rrp = self._price_from(self._text(sel.get("rrp") or "del, .was-price, .old-price, .rrp"))
        currency = offers.get("priceCurrency") or "GBP"
        availability = str(offers.get("availability") or "").rsplit("/", 1)[-1]

        images = []
        if sel.get("images"):
            for el in d.find_elements(By.CSS_SELECTOR, sel["images"]):
                src = el.get_attribute("data-large_image") or el.get_attribute("src") or el.get_attribute("data-src")
                if src:
                    images.append(src)
        ldimg = ld.get("image")
        if isinstance(ldimg, str):
            images.append(ldimg)
        elif isinstance(ldimg, list):
            images += [i if isinstance(i, str) else i.get("url", "") for i in ldimg]
        if not images:
            images.append(self._meta("meta[property='og:image']"))
        images = list(dict.fromkeys(i for i in images if i and i.startswith("http")))

        brand = ld.get("brand")
        brand = brand.get("name", "") if isinstance(brand, dict) else (brand or "")
        brand_guessed = False
        if not brand:   # many shops print the brand in a small line right above the <h1>
            try:
                g = (d.execute_script(BRAND_JS) or "").strip()
                if g and len(g) <= 30 and not re.search(r"[\d/>|]", g):
                    brand, brand_guessed = g, True
            except WebDriverException:
                pass
        if brand_guessed and title and not title.lower().startswith(brand.lower()):
            title = f"{brand} {title}"
        ean = str(ld.get("gtin13") or ld.get("gtin") or ld.get("gtin8") or ld.get("gtin12") or "")

        crumbs = d.find_elements(By.CSS_SELECTOR, ".breadcrumb a, .breadcrumbs a, nav[aria-label*=readcrumb] a")
        names = [c.text.strip() for c in crumbs if c.text.strip() and c.text.strip().lower() != "home"]
        category = self._text(sel.get("category")) or (names[-1] if names else "")

        try:
            specs = d.execute_script(SPECS_JS) or {}
            options = d.execute_script(OPTIONS_JS) or {}
        except WebDriverException:
            specs, options = {}, {}

        if not sku:
            for k, v in specs.items():
                if k.strip().lower() in ("sku", "product code", "item code", "code", "model", "mpn", "ref", "reference"):
                    sku = str(v).strip()
                    break
        if not sku:
            try:
                m = SKU_RE.search(d.find_element(By.TAG_NAME, "body").text)
                sku = m.group(1) if m else ""
            except WebDriverException:
                pass

        weight = ""
        for k, v in specs.items():
            if "weight" in k.lower():
                weight = V.parse_weight_kg(v)
                if weight:
                    break

        base = {"sku": sku, "price": V.money(price), "compare_at_price": V.money(rrp),
                "barcode": ean, "weight": weight}
        option_names, variants = self.extract_variants(ld, base)

        return Product(
            supplier=self.name, url=self.canonical_url(), title=title, handle=slugify(title), sku=sku.strip(),
            ean=ean, brand=str(brand), category=category, breadcrumbs=" > ".join(names), price=price, rrp=rrp,
            currency=currency, availability=availability, weight=weight, short_description=short,
            description=desc_html, description_text=desc_text, meta_description=meta_desc, images=images,
            options=options, specs=specs, option_names=option_names, variants=variants,
        )
