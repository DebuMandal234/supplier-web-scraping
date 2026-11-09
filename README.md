# Supplier scraper (scrape -> Excel)

Selenium opens Chrome, logs in to each supplier, finds ALL products (every category, every page, and the
site's sitemap) and writes them into ONE Excel file, one tab per supplier:

`C:\Users\USER1\Desktop\WebScraping\Supplier Data.xlsx`

**Every product is written to Excel the moment it is scraped** (no JSON files). If you stop the program,
the PC crashes or the power goes, everything scraped so far is already in the file. The file is saved
safely (a complete new file is written first, then swapped in), so it is never left half-written.

Run the same command again later: products already in the Excel tab are skipped and scraping continues.

## Run (VS Code -> Terminal -> New Terminal)

```
.venv\Scripts\activate
python main.py login heartlands              # test one login
python main.py scrape heartlands --limit 5   # small test (5 products)
python main.py scrape heartlands             # FULL run: all categories, all pages
python main.py scrape beehive flair          # only these suppliers
python main.py scrape all                    # all five, one after another
python main.py scrape flair --fresh          # delete flair's tab and start that supplier over
python main.py cleanup                       # delete old JSON / debug / test files (not Excel, not code)
```
Supplier names: `beehive`, `birlea`, `heartlands`, `julian_bowen`, `flair`.

- **Keep the Excel file CLOSED while scraping.** If it is open, the program keeps working and saves to
  `Supplier Data - backup.xlsx` next to it; when you close Excel, the next save goes to the main file again.
- Stop any time with `Ctrl+C`.
- Chrome stays visible. If a captcha/2FA appears, solve it by hand (2 minutes).
- `PAGE_DELAY` in `.env` = seconds between pages (lower = faster, higher = gentler on the site).
- `SAVE_EVERY=1` in `.env` = save after every product. With many thousands of rows each save takes longer;
  if that slows things down, set it to 5 or 10 (you then risk losing at most that many products).

## Test ONE page (finding problems fast)

```
python main.py probe flair https://www.flairbeds.co.uk/flair-wizard-single-bed-frame
python main.py probe flair https://www.flairbeds.co.uk/flair-wizard-single-bed-frame --save
```
Logs in, opens that single page and prints: is it recognised as a product, and the title, brand, SKU, price,
variants, images and specs it read. `--save` also writes it into the supplier's Excel tab.
A screenshot and the page HTML go to `data\debug` (files `*_probe.*`) - send them to Claude if a field is wrong.
Every start prints `Supplier scraper version ...`; if that line is missing, your files are out of date.

## What is in each row

Supplier, SKU, title, handle, vendor/brand, product type/category, tags, supplier price, Shopify price
(with `PRICE_MARKUP`), RRP, availability, EAN, weight, variants (colours/sizes with own price/SKU),
descriptions (text + HTML), meta description, image URLs, dropdown options, every spec as its own
`Spec: ...` column, breadcrumbs, product URL, and **Shopify payload (JSON)** = the ready request body for
`POST /admin/api/<version>/products.json`.
Set `PRICE_MARKUP` in `.env` (e.g. 1.8) BEFORE scraping; Part 2 can also recompute from "Price (supplier)".

## How it finds every product

1. Starts from the home/shop pages and follows every category and menu link.
2. On each listing it follows all pagination links (page numbers + "next"), scrolls down and clicks "Load more".
3. Reads `robots.txt` / `sitemap.xml` and visits every product URL listed there.
4. Re-logs-in automatically if the session expires; skips duplicates.

After a run, open `data\<supplier>_crawl_report.txt`: pages visited, products found, failed URLs.

## If something does not work

| Problem | What to do |
|---|---|
| "the website rejected the login" | Email/password in `.env` is wrong or the account is locked. Log in by hand in Chrome; fix `.env`. Don't retry many times. |
| "could not find the login form" / "login did not complete" | Send Claude the newest `.png` and `.html` in `data\debug`. |
| 0 products or wrong/empty columns | Send Claude one example product page link from that supplier. |
| Fewer products than the website shows | Send Claude the crawl report and a category page link. |
| "Cannot open ... Close it in Excel" | Close the Excel file and run again. |

Per-supplier tuning lives in `suppliers\<supplier>.py`.

### Flair notes (version r8)
Flair (flairbeds.co.uk): on each category page every product link is opened, read and saved to Excel
straight away, then the next category page is opened. The sitemap is used afterwards as a safety net. Category filter links
(`?cat=..&ck_f_colour=..`) are ignored, `?p=2` pages are followed. Product pages / login state are read from
the page's own markers, and sizes/colours come with their own price. Prices are trade prices ex VAT.
Old-format tabs from earlier versions are kept and renamed to `<supplier>_old`.
Please check each supplier allows you to copy their images and descriptions to your store.
