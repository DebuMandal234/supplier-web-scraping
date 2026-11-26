# Supplier scraper → Excel → Shopify

Logs in to each supplier site (Beehive, Birlea, Julian Bowen, Flair; GFW needs no login), saves every product to
`Supplier Data.xlsx` (one tab per supplier, saved the moment each product is read) and lists the products in
Shopify. Column **A** of every tab is the **Shopify product ID**.

## The two steps

| Step | Command | When | What it does |
|---|---|---|---|
| 1 – list | `python main.py list all` | now, then **every Sunday** | browses the supplier sites, saves **new** products to Excel and creates them in Shopify as **ACTIVE** and publishes them to the **Online Store**, in batches of 20 – **one API call per batch** (plus one quick duplicate check). The last, smaller batch is uploaded at the end. The Shopify ID goes to column A. |
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
python main.py fix-stock beehive         # set Shopify stock of drafts this program listed, from Excel
python main.py activate beehive          # set drafts this program listed to ACTIVE + Online Store
python main.py schedule                   # create the Windows tasks: list Sunday 02:00, sync daily 06:00
```
Other: `scrape <supplier>` (Excel only, no Shopify), `login <supplier>`, `probe <supplier> <url>`, `cleanup`.
Every start prints `Supplier scraper version ...` – it should say **r36**.

- **Keep the Excel file closed while it runs.** If it is open, data goes to `Supplier Data - backup.xlsx` and is
  copied into the main file on the next run.
- Stop any time with `Ctrl+C`; the products scraped so far are in Excel and the waiting batch is uploaded.
- Scheduled runs write their log to `data\logs\list.log` and `data\logs\sync.log`.

## Shopify set-up (once)

1. Shopify admin → Settings → Apps → Develop apps (or the Dev Dashboard) → create an app for your store.
2. Admin API scopes: `write_products`, `write_inventory`, `read_locations` (without it products are listed
   without stock numbers – or put `SHOPIFY_LOCATION_ID=<number>` in `.env`), and `read_publications` +
   `write_publications` (without them ACTIVE products are created but not shown in the Online Store).
3. Put the credentials in `.env` (never in the code, never on GitHub):
   ```
   SHOPIFY_STORE=yourstore.myshopify.com
   SHOPIFY_CLIENT_ID=...
   SHOPIFY_CLIENT_SECRET=shpss_...
   SHOPIFY_LISTING_STATUS=ACTIVE
   ```
   Every API call uses the token in **`access token.txt`**. When Shopify refuses it (tokens expire after ~24 h),
   a new one is requested with the client ID + secret and written into `access token.txt`.
   Changed the credentials? Empty or delete `access token.txt`.
4. `python main.py shopify-check` must say "Shopify is ready".

**Safety switches in `.env`** – what the program may change in your store:

| Setting | Default | Allows |
|---|---|---|
| (always) | – | creating new products as `SHOPIFY_LISTING_STATUS` (ACTIVE, published to `SHOPIFY_PUBLISH_CHANNELS` = Online Store) |
| `SHOPIFY_ALLOW_SYNC` | false | STEP 2: price/stock updates of the products it listed |
| `SHOPIFY_CREATE_COLLECTIONS` | false | creating one collection per supplier and adding its products |

Product type = what the product is (the most specific category of the supplier's breadcrumb, e.g. "Ottoman & Storage Beds"); the supplier name is the vendor (unless a brand is shown) and a tag. All product photos are listed (column "Image URLs", comma separated).

## Prices and stock (settings in `.env`)

**Shopify price = (X + 20% VAT + delivery charge £40–80) + 35% profit margin, rounded UP to ...9.99** (X = supplier price)
e.g. X = £100, small item: 100 + 20 = 120 → + 40 delivery = 160 → + 35% = 216 → **£219.99**.
Delivery charge by size: sofas, beds, wardrobes, mattresses, dining sets … = £80; otherwise by weight
(≤15 kg £40, ≤25 kg £50, ≤35 kg £60, ≤50 kg £70, heavier £80); no weight known: medium furniture (sideboard, chest,
desk, …) £60, small items £40. Each size/colour gets its own price from its own supplier price.
Compare-at price = the supplier's RRP when it is higher. Cost per item = the supplier price.
Beehive's made-to-order range ("Choose your Colour, Choose your Fabric" – tag `Made to order`) is always
listed as **DRAFT** for manual editing (`SHOPIFY_DRAFT_TAGS`); everything else is ACTIVE.
Titles are written in Proper Case ("Anya 150cm Integrated Storage Ottoman Bed White"). The first photo of the
product (the supplier's main photo) is the product's main photo in Shopify.

| Setting | Meaning | Default |
|---|---|---|
| `PRICE_VAT_PERCENT` | VAT added to the supplier price | 20 |
| `PROFIT_MARGIN_PERCENT` | profit margin | 35 |
| `PRICE_MARGIN_ON` | `total` = margin on (X + VAT + delivery); `supplier_price` = margin on X only | total |
| `DELIVERY_MIN` / `DELIVERY_MEDIUM` / `DELIVERY_MAX` | delivery charge small / medium (no weight) / big | 40 / 60 / 80 |
| `DELIVERY_WEIGHT_STEPS` | delivery charge by weight `kg:charge,…` | 15:40,25:50,35:60,50:70 |
| `BIG_DELIVERY_KEYWORDS` / `NOT_BIG_KEYWORDS` | words that always mean "big" / never "big" | sofa, bed, wardrobe, … / bedside, … |
| `PRICE_ROUND_TO_9_99` | round up to ...9.99 | true |
| `STOCK_IN_STOCK_QTY` | quantity used when the supplier only says "in stock" | 5 |
| `STOCK_BUFFER` | subtracted when the supplier shows a real number ("12 in stock" → 12 − buffer) | 0 |
| `MISSING_RUNS_BEFORE_ZERO` | a product missing on the supplier site this many daily runs in a row gets stock 0 | 2 |
| `SHOPIFY_BATCH_SIZE` | products per upload batch | 20 |
| `SHOPIFY_LOCATION_ID` | stock location (empty = the store's first location) | – |

Each product is listed with: supplier collection (e.g. "Flair"), vendor, product type, tags, description +
specs, images, options/variants with own SKU and price, cost per item (supplier price), compare-at (RRP),
weight, stock tracked with "stop selling when out of stock", and `supplier.*` metafields (name, SKU, source URL).

**Stock per supplier** (each size/colour gets its own number in Shopify):
- Beehive: exact stock number of every product (Stock Feed API, or the product page).
- Flair: exact number when a size/colour is running low ("Only 2 left"); otherwise in stock = `STOCK_IN_STOCK_QTY`;
  a size/colour that is sold out disappears from Flair's page and is set to 0 by the daily sync.
- Birlea: exact stock number of every product (the shop's own product data, e.g. 201).
- Julian Bowen: exact free stock of every finish/size (e.g. 35); "Back Order" = 0.
- GFW: exact "Free Stock" of every product.

**GFW** (`suppliers/gfw.py`): public Salsify customer catalogue (no login), read through its JSON API - about 590
products. Per product: SKU (product code), EAN barcode, trade price (`GFW_PRICE=standard` = Standard Price, default;
`GFW_PRICE=direct` = Direct Fulfilment Price), exact Free Stock, description + USP bullet points, every photo in full
size (main, roomset, cut-out, detail; no instruction PDFs), colour / materials / assembled dimensions / weight /
assembly / number of boxes as specs, product type = GFW's Product Type (e.g. "Ottoman Bed"), Product Group and colour
as tags. Colours and sizes are separate products (own code) at GFW.

**Julian Bowen** (`suppliers/julian_bowen.py` + `suppliers/jb_product.js`, GOb2b trade shop): the product list comes
from every menu category with the shop's own paging (about 875 products, ~3 minutes). Each product is read with
the same data services the shop's product page uses: every finish/size as a Shopify variant with its own SKU,
trade price (ex VAT), exact stock and photos (1200 x 1200), plus the description, dimensions, weight (from the
description), product type = its category (e.g. "Dining Chairs") and the category path as tags.

**Birlea** (`suppliers/birlea.py`, Sana Commerce shop): every page carries its full product data as JSON. The
scraper reads the whole product list from the menu categories (about 655 products), then for each product:
title, SKU, description, ALL photos (large size), exact stock, trade price (ex VAT), barcode (GTIN), weight
(Gross Weight), product type = its category (e.g. "Console Tables"), Range + Colour as tags, and the spec table.
Colours/sizes are separate products with their own SKU on Birlea.

**Safety:** if a supplier's site can't be read (8 products in a row fail, login broken), the daily sync stops
for that supplier and changes nothing else. A product that disappears is set to 0 only after
`MISSING_RUNS_BEFORE_ZERO` runs, never deleted.

## Variants (sizes / colours / options) – all suppliers

Read in this order, the first that works wins (`core/variant_sources.py`):
1. Shopify stores (Beehive): the product's `.js` data – real option names, SKU, price, stock per variant.
2. WooCommerce sites: the variation form, or the radio-button variation list.
3. schema.org `hasVariant` data in the page.
4. Any other site: the option dropdowns / radio buttons next to "Add to basket" are
   switched one by one (max 40 combinations) and the price, SKU and stock shown are read.
Flair: its own option data first (Size first, then Color – e.g. Margot: 2 sizes x 3 colours = 6 variants in
ONE Shopify product), then the above. Flair also has single-colour copies of such products (e.g. "Margot
Ottoman Bed Green Velvet"); they are skipped when their variants are already in the main product, or marked
DUPLICATE in Excel when they were found first (delete that draft in Shopify).

Each variant gets its own SKU, price (pricing formula), stock and photo in Shopify. When a product clearly has
options but none could be read, the page is saved as `data/debug/<supplier>_variants_missing_N.html` and the
log says so – send that file to Claude to adjust the reader.

## Supplier notes

**Beehive** (portal.beehivefurniture.co.uk, a Shopify store): after login the whole catalogue (about 920
products) is read from the portal's product feed – trade price, SKU, all images, description – no category
browsing. Stock: exact numbers from Beehive's Stock Feed API when `BEEHIVE_STOCK_TOKEN` is in `.env` (also RRP
and specs), otherwise from each product page ("14 in stock"). Dimensions come from each page (bundles list
every piece). "Special Order" sofas are made to order (4–6 weeks): one variant, tagged "Made to order".

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
