# Supplier scraper → Excel → Shopify

Logs in to each supplier site (Beehive, Birlea, Heartlands, Julian Bowen, Flair), saves every product to
`Supplier Data.xlsx` (one tab per supplier, saved the moment each product is read) and lists the products in
Shopify. Column **A** of every tab is the **Shopify product ID**.

## The two steps

| Step | Command | When | What it does |
|---|---|---|---|
| 1 – list | `python main.py list all` | now, then **every Sunday** | browses the supplier sites, saves **new** products to Excel and creates them in Shopify as **DRAFT**, in batches of 20 (the last, smaller batch is uploaded at the end). The Shopify ID goes to column A. |
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
Every start prints `Supplier scraper version ...` – it should say **r9**.

- **Keep the Excel file closed while it runs.** If it is open, data goes to `Supplier Data - backup.xlsx` and is
  copied into the main file on the next run.
- Stop any time with `Ctrl+C`; the products scraped so far are in Excel and the waiting batch is uploaded.
- Scheduled runs write their log to `data\logs\list.log` and `data\logs\sync.log`.

## Shopify set-up (once)

1. Shopify admin → Settings → Apps → Develop apps (or the Dev Dashboard) → create an app for your store.
2. Admin API scopes: `read_products`, `write_products`, `read_inventory`, `write_inventory`, `read_locations`.
3. Install it and put the credentials in `.env` (never in the code, never on GitHub):
   ```
   SHOPIFY_STORE=yourstore.myshopify.com
   SHOPIFY_ACCESS_TOKEN=shpat_...          # or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
   SHOPIFY_PRODUCT_STATUS=DRAFT
   ```
4. `python main.py shopify-check` must say "Shopify is ready".

## Prices and stock (settings in `.env`)

| Setting | Meaning | Default |
|---|---|---|
| `PRICE_MARKUP` / `FLAIR_MARKUP`, `BEEHIVE_MARKUP`… | Shopify price = supplier trade price × markup (per supplier overrides the general one) | 1.0 |
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

## Supplier notes

Per-supplier rules live in `suppliers\<supplier>.py`. Flair: on each category page every product link is opened,
read and saved straight away, then the next page; filter links are ignored; prices are trade prices ex VAT.
Please check each supplier allows you to copy their images and descriptions to your store.

## If something does not work

| Problem | What to do |
|---|---|
| "Shopify is not set up" / "refused the request" | check `SHOPIFY_STORE` + token in `.env`; run `shopify-check` |
| "MISSING permissions" | add the listed scopes to the app in Shopify and update/reinstall it |
| a row's "Shopify status" says ERROR | it is retried on the next `list` / `upload`; send Claude the message if it repeats |
| "the website rejected the login" | supplier email/password in `.env` is wrong or locked |
| "Cannot open ... Close it in Excel" | close the Excel file and run again |
