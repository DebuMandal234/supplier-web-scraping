# Supplier scraper → Excel → Shopify

Logs in to each supplier site (Beehive, Birlea, Heartlands, Julian Bowen, Flair), saves every product to
`Supplier Data.xlsx` (one tab per supplier, saved the moment each product is read) and lists the products in
Shopify. Column **A** of every tab is the **Shopify product ID**.

## The two steps

| Step | Command | When | What it does |
|---|---|---|---|
| 1 – list | `python main.py list all` | now, then **every Sunday** | browses the supplier sites, saves **new** products to Excel and creates them in Shopify as **DRAFT**, in batches of 20 – **one API call per batch** (plus one quick duplicate check). The last, smaller batch is uploaded at the end. The Shopify ID goes to column A. |
| 2 – sync | `python main.py sync all` | **every day** | no browsing: opens each listed product **directly by its link**, reads price + stock and sends only the changes to Shopify. |

**No duplicates:** a product is skipped when its link or its SKU is already in the supplier's tab, and it is
linked (not created again) when its SKU is already in Shopify. Rows that failed to upload are retried on the
next run.

## Run (VS Code → Terminal, or Command Prompt)

```
cd C:\Users\USER1\Desktop\WebScraping
.venv\Scripts\activate

python main.py shopify-check              # 1st: test the Shopify key + permissions
python main.py list flair --limit 5       # small test: 5 new Flair products -> Excel + Shopify
python main.py list flair                 # STEP 1, one supplier
python main.py list all                   # STEP 1, all five one after another
python main.py sync flair --limit 5       # small test of STEP 2
python main.py sync all                   # STEP 2, all suppliers
python main.py upload all                 # only upload Excel rows without a Shopify ID (no browsing)
python main.py schedule                   # create the Windows tasks: list Sunday 02:00, sync daily 06:00
```
Other: `scrape <supplier>` (Excel only, no Shopify), `login <supplier>`, `probe <supplier> <url>`, `cleanup`.
Every start prints `Supplier scraper version ...` – it should say **r18**.

- **Keep the Excel file closed while it runs.** If it is open, data goes to `Supplier Data - backup.xlsx` and is
  copied into the main file on the next run.
- Stop any time with `Ctrl+C`; the products scraped so far are in Excel and the waiting batch is uploaded.
- Scheduled runs write their log to `data\logs\list.log` and `data\logs\sync.log`.

## Shopify set-up (once)

1. Shopify admin → Settings → Apps → Develop apps (or the Dev Dashboard) → create an app for your store.
2. Admin API scopes: `write_products`, `write_inventory`, and `read_locations` (without it products are listed
   without stock numbers – or put `SHOPIFY_LOCATION_ID=<number>` in `.env`).
3. Put the credentials in `.env` (never in the code, never on GitHub):
   ```
   SHOPIFY_STORE=yourstore.myshopify.com
   SHOPIFY_CLIENT_ID=...
   SHOPIFY_CLIENT_SECRET=shpss_...
   SHOPIFY_PRODUCT_STATUS=DRAFT
   ```
   Every API call uses the token in **`access token.txt`**. When Shopify refuses it (tokens expire after ~24 h),
   a new one is requested with the client ID + secret and written into `access token.txt`.
   Changed the credentials? Empty or delete `access token.txt`.
4. `python main.py shopify-check` must say "Shopify is ready".

**Safety switches in `.env`** – what the program may change in your store:

| Setting | Default | Allows |
|---|---|---|
| (always) | – | creating new products as `SHOPIFY_PRODUCT_STATUS` (DRAFT) |
| `SHOPIFY_ALLOW_SYNC` | false | STEP 2: price/stock updates of the products it listed |
| `SHOPIFY_CREATE_COLLECTIONS` | false | creating one collection per supplier and adding its products |

Product type = what the product is (the most specific category of the supplier's breadcrumb, e.g. "Ottoman & Storage Beds"); the supplier name is the vendor (unless a brand is shown) and a tag. All product photos are listed (column "Image URLs", comma separated).

## Prices and stock (settings in `.env`)

**Shopify price = supplier price + 20% + £35 fee (£85 for sofas / big items), rounded UP to ...9.99**
(e.g. 46.56 → 49.99, 77.32 → 79.99). Each size/colour gets its own price from its own supplier price.
Compare-at price = the supplier's RRP when it is higher. Cost per item = the supplier price.

| Setting | Meaning | Default |
|---|---|---|
| `PRICE_MARGIN_PERCENT` | margin on top of the supplier price | 20 |
| `PRICE_FEE_STANDARD` / `PRICE_FEE_BIG` | fixed amount added; big = sofas / big items | 35 / 85 |
| `BIG_ITEM_KEYWORDS` | words in title/category that make a product "big" | sofa, settee, chaise, corner sofa, … |
| `BIG_ITEM_MIN_WEIGHT_KG` | also "big" from this weight (0 = off) | 0 |
| `PRICE_ROUND_TO_9_99` | round up to ...9.99 | true |
| `STOCK_IN_STOCK_QTY` | quantity used when the supplier only says "in stock" | 5 |
| `STOCK_BUFFER` | subtracted when the supplier shows a real number ("12 in stock" → 12 − buffer) | 0 |
| `MISSING_RUNS_BEFORE_ZERO` | a product missing on the supplier site this many daily runs in a row gets stock 0 | 2 |
| `SHOPIFY_BATCH_SIZE` | products per upload batch | 20 |
| `SHOPIFY_LOCATION_ID` | stock location (empty = the store's first location) | – |

Each product is listed with: supplier collection (e.g. "Flair"), vendor, product type, tags, description +
specs, images, options/variants with own SKU and price, cost per item (supplier price), compare-at (RRP),
weight, stock tracked with "stop selling when out of stock", and `supplier.*` metafields (name, SKU, source URL).

**Safety:** if a supplier's site can't be read (8 products in a row fail, login broken), the daily sync stops
for that supplier and changes nothing else. A product that disappears is set to 0 only after
`MISSING_RUNS_BEFORE_ZERO` runs, never deleted.

## Variants (sizes / colours / options) – all suppliers

Read in this order, the first that works wins (`core/variant_sources.py`):
1. Shopify stores (Beehive): the product's `.js` data – real option names, SKU, price, stock per variant.
2. WooCommerce (Heartlands): the variation form, or the radio-button variation list.
3. schema.org `hasVariant` data in the page.
4. Any other site (Birlea, Julian Bowen, …): the option dropdowns / radio buttons next to "Add to basket" are
   switched one by one (max 40 combinations) and the price, SKU and stock shown are read.
Flair: its own Magento option data first, then the above.

Each variant gets its own SKU, price (pricing formula), stock and photo in Shopify. When a product clearly has
options but none could be read, the page is saved as `data/debug/<supplier>_variants_missing_N.html` and the
log says so – send that file to Claude to adjust the reader.

## Supplier notes

Per-supplier rules live in `suppliers\<supplier>.py`. Flair: on each category page every product link is opened,
read and saved straight away, then the next page; filter links are ignored; prices are trade prices ex VAT.
Please check each supplier allows you to copy their images and descriptions to your store.

## If something does not work

| Problem | What to do |
|---|---|
| "Shopify is not set up" / "refused the request" | check `SHOPIFY_STORE`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` in `.env`; run `shopify-check` |
| "MISSING permissions" | add the listed scopes to the app in Shopify and update/reinstall it |
| a row's "Shopify status" says ERROR | it is retried on the next `list` / `upload`; send Claude the message if it repeats |
| "the website rejected the login" | supplier email/password in `.env` is wrong or locked |
| "Cannot open ... Close it in Excel" | close the Excel file and run again |
