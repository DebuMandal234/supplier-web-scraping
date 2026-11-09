import html
import logging
import time

import requests

import config
from core.models import Product

log = logging.getLogger("shopify")


class ShopifyClient:
    def __init__(self):
        if not config.SHOPIFY_STORE:
            raise RuntimeError("SHOPIFY_STORE is empty in .env")
        self.store = config.SHOPIFY_STORE.replace("https://", "").rstrip("/")
        self.token = config.SHOPIFY_ACCESS_TOKEN or self._client_credentials_token()
        self.url = f"https://{self.store}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"

    def _client_credentials_token(self):
        if not (config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET):
            raise RuntimeError("Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")
        r = requests.post(
            f"https://{self.store}/admin/oauth/access_token",
            json={"client_id": config.SHOPIFY_CLIENT_ID, "client_secret": config.SHOPIFY_CLIENT_SECRET,
                  "grant_type": "client_credentials"},
            timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def gql(self, query, variables=None, retries=5):
        for attempt in range(retries):
            r = requests.post(self.url, json={"query": query, "variables": variables or {}},
                              headers={"X-Shopify-Access-Token": self.token}, timeout=60)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            errs = data.get("errors")
            if errs and any("THROTTLED" in str(e) for e in errs):
                time.sleep(2 * (attempt + 1))
                continue
            if errs:
                raise RuntimeError(f"Shopify GraphQL error: {errs}")
            return data["data"]
        raise RuntimeError("Shopify rate limit: gave up after retries")

    def sku_exists(self, sku: str) -> bool:
        if not sku:
            return False
        q = 'query($q:String!){ products(first:1, query:$q){ nodes{ id } } }'
        data = self.gql(q, {"q": f'sku:"{sku}"'})
        return bool(data["products"]["nodes"])

    def create_product(self, p: Product):
        """Creates a product with one variant. Returns product id (or None if skipped)."""
        if p.sku and self.sku_exists(p.sku):
            log.info("skip (SKU already in Shopify): %s", p.sku)
            return None

        desc = p.description or ""
        if desc and "<" not in desc:
            desc = "<p>" + html.escape(desc).replace("\n", "<br>") + "</p>"
        if p.options:
            desc += "<ul>" + "".join(f"<li><b>{html.escape(k)}:</b> {html.escape(str(v))}</li>"
                                      for k, v in p.options.items()) + "</ul>"
        tags = [t for t in {p.supplier, p.category} if t]

        m = """
        mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
          productCreate(product: $product, media: $media) {
            product { id variants(first: 1) { nodes { id } } }
            userErrors { field message }
          }
        }"""
        media = [{"originalSource": u, "mediaContentType": "IMAGE", "alt": p.title} for u in p.images[:10]]
        data = self.gql(m, {
            "product": {"title": p.title, "descriptionHtml": desc, "vendor": p.brand or p.supplier,
                        "productType": p.category, "tags": tags, "status": config.SHOPIFY_PRODUCT_STATUS},
            "media": media})
        res = data["productCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"productCreate: {res['userErrors']}")
        pid = res["product"]["id"]
        vid = res["product"]["variants"]["nodes"][0]["id"]

        price = ""
        if p.price:
            try:
                price = f"{float(p.price) * config.PRICE_MARKUP:.2f}"
            except ValueError:
                pass
        v = {"id": vid}
        if price:
            v["price"] = price
        if p.sku:
            v["inventoryItem"] = {"sku": p.sku, "tracked": False}
        m2 = """
        mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $pid, variants: $v) {
            userErrors { field message }
          }
        }"""
        r2 = self.gql(m2, {"pid": pid, "v": [v]})["productVariantsBulkUpdate"]
        if r2["userErrors"]:
            raise RuntimeError(f"variant update: {r2['userErrors']}")
        time.sleep(0.5)
        return pid
