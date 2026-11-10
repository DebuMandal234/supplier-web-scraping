"""Shopify Admin GraphQL client: list products (productSet) and update stock / prices.

Uses the GraphQL Admin API (the REST product endpoints are legacy/deprecated).
Credentials come from .env: SHOPIFY_STORE + SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET.
Needed app scopes: write_products, write_inventory (+ read_locations to set stock quantities).

Access token: with SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET the token is requested once, saved in the file
.shopify_token and reused; a new one is requested only when Shopify refuses the saved one (or it has expired).

What this program changes in Shopify:
  * STEP 1: creates products (status DRAFT) - nothing else.
  * STEP 2 (sync): prices/stock of products it created - only when SHOPIFY_ALLOW_SYNC=true in .env.
  * supplier collections - only when SHOPIFY_CREATE_COLLECTIONS=true in .env.
"""
import os
import logging
import re
import time
import uuid

import requests

import config
from core.payload import body_html

log = logging.getLogger("shopify")


def money(v):
    try:
        f = float(str(v).replace(",", "").replace("£", "").strip())
        return f"{f:.2f}" if f >= 0 else None
    except (ValueError, TypeError):
        return None


def stock_qty(in_stock, availability):
    """Shopify quantity for one variant from what the supplier shows.
    in_stock: True / False / None (unknown);  availability: text such as '12 in stock', 'InStock', 'Out of stock'."""
    t = (availability or "").lower()
    m = re.search(r"(\d+)\s*(?:\+\s*)?(?:in stock|available|left|units?)", t)
    if in_stock is False:
        return 0
    if re.search(r"out\s*of\s*stock|outofstock|sold\s*out|unavailable|discontinued|no longer", t):
        return 0 if in_stock is not True else config.STOCK_IN_STOCK_QTY
    if m:
        return max(0, int(m.group(1)) - config.STOCK_BUFFER)
    return config.STOCK_IN_STOCK_QTY


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    def __init__(self):
        if not config.SHOPIFY_STORE:
            raise ShopifyError("SHOPIFY_STORE is empty in .env (e.g. SHOPIFY_STORE=yourstore.myshopify.com)")
        self.store = config.SHOPIFY_STORE.replace("https://", "").replace("http://", "").strip("/")
        if not (config.SHOPIFY_ACCESS_TOKEN or (config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET)):
            raise ShopifyError("Set SHOPIFY_ACCESS_TOKEN (or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET) in .env")
        self.url = f"https://{self.store}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
        self.uses_client_credentials = bool(config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET)
        self.token = self._saved_token() if self.uses_client_credentials else config.SHOPIFY_ACCESS_TOKEN
        if not self.token:
            self.token = self._client_credentials_token()
        self._location = None
        self._location_warned = False
        self._collections = {}

    # ------------------------------------------------------------------ transport
    def _saved_token(self):
        """Token saved by an earlier run, if it is for this store and not expired (5 minutes margin)."""
        try:
            with open(config.SHOPIFY_TOKEN_FILE, encoding="utf-8") as f:
                parts = f.read().split()
            token, expires, store = parts[0], float(parts[1]), (parts[2] if len(parts) > 2 else self.store)
            if store == self.store and time.time() < expires - 300:
                return token
        except (OSError, ValueError, IndexError):
            pass
        return ""

    def _client_credentials_token(self):
        """Ask Shopify for a new access token and save it for the next runs."""
        r = requests.post(f"https://{self.store}/admin/oauth/access_token", timeout=30, json={
            "client_id": config.SHOPIFY_CLIENT_ID, "client_secret": config.SHOPIFY_CLIENT_SECRET,
            "grant_type": "client_credentials"})
        if not r.ok:
            raise ShopifyError(f"Shopify login (client credentials) failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        token = data["access_token"]
        try:
            with open(config.SHOPIFY_TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(f"{token}\n{time.time() + float(data.get('expires_in') or 86399):.0f}\n{self.store}\n")
        except OSError as e:
            log.warning("could not save the Shopify token file: %s", e)
        log.info("Got a new Shopify access token (saved in %s)", os.path.basename(config.SHOPIFY_TOKEN_FILE))
        return token

    def gql(self, query, variables=None, retries=6):
        refreshed = False
        for attempt in range(retries):
            try:
                r = requests.post(self.url, json={"query": query, "variables": variables or {}}, timeout=90,
                                  headers={"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"})
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise ShopifyError(f"network error: {e}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 401 and self.uses_client_credentials and not refreshed:
                refreshed = True
                self.token = self._client_credentials_token()   # saved token expired/revoked: get a new one once
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise ShopifyError(f"Shopify refused the request (HTTP {r.status_code}). Check the token and the "
                                   f"app's API scopes. {r.text[:200]}")
            if not r.ok:
                raise ShopifyError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            errs = data.get("errors")
            if errs and "THROTTLED" in str(errs):
                time.sleep(2 * (attempt + 1))
                continue
            if errs:
                raise ShopifyError(f"GraphQL error: {str(errs)[:500]}")
            ts = ((data.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            if ts and ts.get("currentlyAvailable", 1000) < 200:   # stay well inside the rate limit
                time.sleep(max(1.0, (200 - ts["currentlyAvailable"]) / max(ts.get("restoreRate", 50), 1)))
            return data["data"]
        raise ShopifyError("Shopify rate limit: gave up after retries")

    # ------------------------------------------------------------------ store info
    def check(self):
        d = self.gql("""{ shop { name myshopifyDomain currencyCode }
                          currentAppInstallation { accessScopes { handle } } }""")
        scopes = sorted(s["handle"] for s in d["currentAppInstallation"]["accessScopes"])
        need = ["read_products", "write_products", "read_inventory", "write_inventory"]
        have = set(scopes) | {s.replace("write_", "read_") for s in scopes}   # write_x includes read_x
        missing = [s for s in need if s not in have]
        return d["shop"], scopes, missing, self.location_id()

    def location_id(self):
        if self._location is not None:
            return self._location or None   # "" = no permission to read locations (warned once)
        if config.SHOPIFY_LOCATION_ID:
            lid = config.SHOPIFY_LOCATION_ID
            self._location = lid if lid.startswith("gid://") else f"gid://shopify/Location/{lid}"
            return self._location
        try:
            d = self.gql("{ locations(first: 5, includeInactive: false) { nodes { id name isActive } } }")
        except ShopifyError as e:
            if not self._location_warned:
                self._location_warned = True
                log.warning("Cannot read the store's locations (%s). Products are listed WITHOUT stock "
                            "quantities. Fix: add the 'read_locations' scope to the app, or put "
                            "SHOPIFY_LOCATION_ID=<number> in .env.", str(e)[:120])
            self._location = ""
            return None
        nodes = d["locations"]["nodes"]
        if not nodes:
            raise ShopifyError("No active location in the store (needed for stock).")
        self._location = nodes[0]["id"]
        log.info("Shopify stock location: %s (%s)", nodes[0]["name"], self._location)
        return self._location

    def supplier_collection(self, name):
        """ID of the manual collection named after the supplier (created when missing)."""
        if name in self._collections:
            return self._collections[name]
        safe = name.replace('"', "")
        d = self.gql('query($q:String!){ collections(first: 5, query: $q) { nodes { id title } } }',
                     {"q": f'title:"{safe}"'})
        cid = next((c["id"] for c in d["collections"]["nodes"] if c["title"].lower() == name.lower()), None)
        if not cid:
            d = self.gql("""mutation($input: CollectionInput!) { collectionCreate(input: $input) {
                              collection { id } userErrors { field message } } }""",
                         {"input": {"title": name, "descriptionHtml": f"<p>Products from {name}</p>"}})
            res = d["collectionCreate"]
            if res["userErrors"]:
                raise ShopifyError(f"collectionCreate: {res['userErrors']}")
            cid = res["collection"]["id"]
            log.info("Created Shopify collection '%s'", name)
        self._collections[name] = cid
        return cid

    # ------------------------------------------------------------------ step 1: listing
    def find_existing(self, p):
        """Product already in Shopify? By SKU (any variant), else by exact title + vendor. Returns product ID or None."""
        skus = [s for s in [p.sku] + [v.get("sku") for v in p.variants] if s]
        for sku in list(dict.fromkeys(skus))[:3]:
            safe = str(sku).replace('"', "")
            d = self.gql('query($q:String!){ productVariants(first: 1, query: $q) { nodes { sku product { id } } } }',
                         {"q": f'sku:"{safe}"'})
            for n in d["productVariants"]["nodes"]:
                if (n.get("sku") or "").lower() == str(sku).lower():
                    return n["product"]["id"]
        if not skus and p.title:
            vendor = config.SUPPLIER_NAMES.get(p.supplier, p.supplier)
            q = 'title:"{}" AND vendor:"{}"'.format(p.title.replace('"', ""), (p.brand or vendor).replace('"', ""))
            d = self.gql('query($q:String!){ products(first: 5, query: $q) { nodes { id title } } }', {"q": q})
            for n in d["products"]["nodes"]:
                if n["title"].strip().lower() == p.title.strip().lower():
                    return n["id"]
        return None

    def product_input(self, p):
        supplier_name = config.SUPPLIER_NAMES.get(p.supplier, p.supplier)
        mk = config.markup(p.supplier)
        loc = self.location_id()   # None = no permission to read locations: list without stock numbers
        names = [n for n in (p.option_names or [])][:3]
        variants_in = p.variants or [{"options": [], "sku": p.sku, "price": p.price, "compare_at_price": p.rrp,
                                      "barcode": p.ean, "weight": p.weight, "in_stock": None}]
        if not names:
            names, variants_in = ["Title"], [dict(variants_in[0], options=["Default Title"])]

        option_values = [[] for _ in names]
        variants = []
        for v in variants_in[:2048]:
            opts = [((v.get("options") or [])[i] if i < len(v.get("options") or []) else "") or "Default"
                    for i in range(len(names))]
            for i, o in enumerate(opts):
                if o not in option_values[i]:
                    option_values[i].append(o)
            cost = money(v.get("price") or p.price)
            var = {
                "optionValues": [{"optionName": n, "name": o} for n, o in zip(names, opts)],
                "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": True, "requiresShipping": True},
            }
            if loc:
                var["inventoryQuantities"] = [{"locationId": loc, "name": "available",
                                               "quantity": stock_qty(v.get("in_stock"), p.availability)}]
            if cost is not None:
                var["price"] = f"{float(cost) * mk:.2f}"
                var["inventoryItem"]["cost"] = cost
            cmp_ = money(v.get("compare_at_price") or (p.rrp if len(variants_in) == 1 else ""))
            if cmp_ and var.get("price") and float(cmp_) > float(var["price"]):
                var["compareAtPrice"] = cmp_
            if v.get("sku"):
                var["sku"] = str(v["sku"])[:255]
            w = v.get("weight") or p.weight
            try:
                if w and float(w) > 0:
                    var["inventoryItem"]["measurement"] = {"weight": {"value": float(w), "unit": "KILOGRAMS"}}
            except ValueError:
                pass
            variants.append(var)

        tags = []
        for t in [supplier_name, p.brand, p.category] + [c.strip() for c in (p.breadcrumbs or "").split(">")]:
            t = (t or "").strip()[:255]
            if t and t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
        files = [{"originalSource": u, "contentType": "IMAGE", "alt": p.title[:500]}
                 for u in list(dict.fromkeys(i for i in p.images if i.startswith("http")))[:20]]
        meta = [{"namespace": "supplier", "key": "name", "type": "single_line_text_field", "value": supplier_name},
                {"namespace": "supplier", "key": "source_url", "type": "url", "value": p.url}]
        if p.sku:
            meta.append({"namespace": "supplier", "key": "sku", "type": "single_line_text_field", "value": p.sku})
        inp = {
            "title": p.title[:255],
            "descriptionHtml": body_html(p),
            "vendor": (p.brand or supplier_name)[:255],
            "productType": supplier_name[:255],   # supplier name = the product's category in Shopify
            "tags": tags[:250],
            "status": config.SHOPIFY_PRODUCT_STATUS if config.SHOPIFY_PRODUCT_STATUS in
            ("DRAFT", "ACTIVE", "ARCHIVED") else "DRAFT",
            "productOptions": [{"name": n, "values": [{"name": o} for o in vals]}
                               for n, vals in zip(names, option_values)],
            "variants": variants,
            "files": files,
            "metafields": meta,
            "seo": {"title": p.title[:70], "description": (p.meta_description or p.description_text or "")[:320]},
        }
        if config.SHOPIFY_CREATE_COLLECTIONS:
            inp["collections"] = [self.supplier_collection(supplier_name)]
        return inp

    # ---- batch: 1 lookup call + 1 create call for up to 20 products
    def find_existing_many(self, products):
        """One API call: which of these products are already in Shopify (by SKU, else by title + vendor)?
        Returns a list of product IDs / None, same order."""
        parts, var_defs, variables, keys = [], [], {}, []
        for i, p in enumerate(products):
            sku = next((s for s in [p.sku] + [v.get("sku") for v in p.variants] if s), "")
            if sku:
                q = 'sku:"{}"'.format(str(sku).replace('"', ""))
                parts.append(f"c{i}: productVariants(first: 1, query: $q{i}) {{ nodes {{ sku product {{ id }} }} }}")
            else:
                vendor = p.brand or config.SUPPLIER_NAMES.get(p.supplier, p.supplier)
                q = 'title:"{}" AND vendor:"{}"'.format(p.title.replace('"', ""), vendor.replace('"', ""))
                parts.append(f"c{i}: products(first: 5, query: $q{i}) {{ nodes {{ id title }} }}")
            var_defs.append(f"$q{i}: String!")
            variables[f"q{i}"] = q
            keys.append(sku)
        if not parts:
            return []
        d = self.gql(f"query({', '.join(var_defs)}) {{ {' '.join(parts)} }}", variables)
        out = []
        for i, p in enumerate(products):
            nodes = (d.get(f"c{i}") or {}).get("nodes") or []
            found = None
            for n in nodes:
                if keys[i] and (n.get("sku") or "").lower() == str(keys[i]).lower():
                    found = n["product"]["id"]
                elif not keys[i] and n.get("title", "").strip().lower() == p.title.strip().lower():
                    found = n["id"]
                if found:
                    break
            out.append(found)
        return out

    def create_products(self, products):
        """Create up to SHOPIFY_BATCH_SIZE products as DRAFT in ONE API call (one aliased productSet per product).
        Products already in Shopify are linked, not created again.
        Returns [(product_id or None, note or error text)] in the same order."""
        results = [None] * len(products)
        existing = self.find_existing_many(products)
        todo = []
        for i, (p, pid) in enumerate(zip(products, existing)):
            if pid:
                results[i] = (pid, "already in Shopify - linked")
            else:
                try:
                    todo.append((i, self.product_input(p)))
                except ShopifyError as e:
                    results[i] = (None, f"ERROR: {e}")
        self._create_inputs(todo, results)
        return results

    def _create_inputs(self, todo, results):
        if not todo:
            return
        defs = ", ".join(f"$i{n}: ProductSetInput!" for n in range(len(todo)))
        body = " ".join(f"p{n}: productSet(synchronous: true, input: $i{n}) "
                        f"{{ product {{ id }} userErrors {{ field message code }} }}" for n in range(len(todo)))
        try:
            d = self.gql(f"mutation({defs}) {{ {body} }}", {f"i{n}": inp for n, (_, inp) in enumerate(todo)})
        except ShopifyError as e:
            if len(todo) > 1 and re.search(r"cost|complex|too large|exceed|timeout|HTTP 5", str(e), re.I):
                half = len(todo) // 2   # batch too big for one call: split it in two and try again
                log.warning("Batch of %d too big for one call (%s) - sending as 2 calls", len(todo), str(e)[:80])
                self._create_inputs(todo[:half], results)
                self._create_inputs(todo[half:], results)
                return
            raise
        for n, (i, _) in enumerate(todo):
            res = d.get(f"p{n}") or {}
            errs = res.get("userErrors") or []
            if errs or not (res.get("product") or {}).get("id"):
                msg = "; ".join(f"{'.'.join(map(str, e.get('field') or []))}: {e['message']}" for e in errs)
                results[i] = (None, f"ERROR: {msg or 'no product returned'}"[:500])
            else:
                results[i] = (res["product"]["id"], "listed")

    def create_product(self, p):
        """Create one product (DRAFT). Returns (product_id, note). Raises ShopifyError on failure."""
        existing = self.find_existing(p)
        if existing:
            return existing, "already in Shopify - linked"
        m = """mutation($input: ProductSetInput!) {
                 productSet(synchronous: true, input: $input) {
                   product { id }
                   userErrors { field message code }
                 } }"""
        res = self.gql(m, {"input": self.product_input(p)})["productSet"]
        if res["userErrors"]:
            raise ShopifyError("; ".join(f"{'.'.join(map(str, e.get('field') or []))}: {e['message']}"
                                         for e in res["userErrors"])[:500])
        return res["product"]["id"], "listed"

    # ------------------------------------------------------------------ step 2: stock + price
    def product_variants(self, product_id):
        q = """query($id: ID!, $loc: ID!) { product(id: $id) { id status
                 variants(first: 250) { nodes { id sku price compareAtPrice selectedOptions { name value }
                   inventoryItem { id tracked unitCost { amount }
                     inventoryLevel(locationId: $loc) { quantities(names: ["available"]) { quantity } } } } } } }"""
        d = self.gql(q, {"id": product_id, "loc": self.location_id()})
        return d["product"]

    def update_variants(self, product_id, updates):
        """updates: [{"id", "price"?, "compareAtPrice"?, "inventoryItem"?}]"""
        if not updates:
            return
        m = """mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) {
                 productVariantsBulkUpdate(productId: $pid, variants: $v) { userErrors { field message } } }"""
        res = self.gql(m, {"pid": product_id, "v": updates})["productVariantsBulkUpdate"]
        if res["userErrors"]:
            raise ShopifyError(f"price update: {res['userErrors']}")

    def activate_inventory(self, inventory_item_id):
        m = """mutation($item: ID!, $loc: ID!) { inventoryActivate(inventoryItemId: $item, locationId: $loc) {
                 userErrors { field message } } }"""
        self.gql(m, {"item": inventory_item_id, "loc": self.location_id()})

    def set_quantities(self, items, note=""):
        """items: [(inventory_item_id, new_qty, current_qty_or_None)]"""
        if not items:
            return
        m = """mutation($input: InventorySetQuantitiesInput!, $key: String!) {
                 inventorySetQuantities(input: $input) @idempotent(key: $key) { userErrors { code field message } } }"""
        inp = {"name": "available", "reason": "correction",
               "referenceDocumentUri": f"supplier-sync://{note or 'stock'}",
               "quantities": [{"inventoryItemId": i, "locationId": self.location_id(), "quantity": int(q),
                               "changeFromQuantity": cur} for i, q, cur in items]}
        res = self.gql(m, {"input": inp, "key": str(uuid.uuid4())})["inventorySetQuantities"]
        if res["userErrors"]:
            raise ShopifyError(f"stock update: {res['userErrors']}")
