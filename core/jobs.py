"""The two jobs of the project.

STEP 1  list  (first run now, then weekly on Sunday)
    Browse the supplier site, save every NEW product to its Excel tab, and list it in Shopify as DRAFT.
    Products are uploaded in batches: as soon as SHOPIFY_BATCH_SIZE (20) new products are waiting, that batch
    is created in Shopify and each product's Shopify ID is written to column A of its row. The last, smaller
    batch is uploaded at the end. Rows still without an ID from earlier runs are uploaded first.
    Duplicates are skipped: same product link or same SKU already in the tab, or SKU already in Shopify.

STEP 2  sync  (daily)
    No category browsing: every product that has a Shopify ID is opened directly by its link, its price and
    stock are read and only the changes are sent to Shopify (price, compare-at, cost, stock quantity).
"""
import logging
import re
from datetime import datetime

import config
from core import excel
from core.excel import SHOPIFY_ID_LABEL, STATUS_LABEL, SYNC_LABEL, URL_LABEL, product_from_row
from core.shopify import ShopifyError, money, stock_qty

log = logging.getLogger("jobs")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------------------------- STEP 1
def upload_rows(name, sheet, shop, rows):
    """Create the products of these Excel rows in Shopify and write their IDs to column A."""
    ok = failed = 0
    for r in rows:
        d = sheet.row(r)
        if str(d.get(SHOPIFY_ID_LABEL) or "").strip():
            continue
        title = d.get("Title") or d.get(URL_LABEL)
        try:
            p = product_from_row(d)
            p.supplier = p.supplier or name
            pid, note = shop.create_product(p)
            sheet.set(r, SHOPIFY_ID_LABEL, pid.rsplit("/", 1)[-1])
            sheet.set(r, STATUS_LABEL, f"{note} {_now()}")
            ok += 1
            log.info("[%s]   Shopify: %s  %s", name, note, title)
        except ShopifyError as e:
            failed += 1
            sheet.set(r, STATUS_LABEL, f"ERROR {_now()}: {e}")
            log.error("[%s]   Shopify upload failed for '%s': %s", name, title, e)
            if "refused" in str(e) or "scope" in str(e).lower():
                sheet.flush()
                raise   # wrong token / missing permission: no point trying the next products
    sheet.flush()
    log.info("[%s] Shopify batch done: %d listed, %d failed (failed ones are retried on the next run)",
             name, ok, failed)
    return ok, failed


def upload_waiting(name, sheet, shop):
    waiting = sheet.waiting_rows()
    if not waiting:
        return 0, 0
    log.info("[%s] %d products in Excel are not in Shopify yet - uploading them first", name, len(waiting))
    tot_ok = tot_failed = 0
    for i in range(0, len(waiting), config.SHOPIFY_BATCH_SIZE):
        ok, failed = upload_rows(name, sheet, shop, waiting[i:i + config.SHOPIFY_BATCH_SIZE])
        tot_ok, tot_failed = tot_ok + ok, tot_failed + failed
    return tot_ok, tot_failed


def run_list(name, scraper_cls, make_driver, shop, limit=None):
    """STEP 1 for one supplier. Returns the scraper (for the crawl report) and the sheet."""
    sheet = excel.SupplierSheet(name)
    log.info("[%s] STEP 1: new products -> Excel tab '%s' -> Shopify (%s, batches of %d)", name, name,
             config.SHOPIFY_PRODUCT_STATUS, config.SHOPIFY_BATCH_SIZE)
    upload_waiting(name, sheet, shop)

    driver = make_driver(config.HEADLESS)
    scraper, pending, new = None, [], 0
    try:
        scraper = scraper_cls(driver)
        scraper.login()
        sheet.start()
        for p in scraper.crawl(limit=limit, known_urls=sheet.known_urls):
            why = sheet.is_known(p)
            if why:
                log.info("[%s]   already in Excel (%s) - skipped: %s", name, why, p.title)
                continue
            pending.append(sheet.add(p))   # saved to Excel now
            new += 1
            if len(pending) >= config.SHOPIFY_BATCH_SIZE:
                upload_rows(name, sheet, shop, pending)
                pending = []
    except KeyboardInterrupt:
        log.warning("[%s] stopped by you", name)
    except ShopifyError as e:
        log.error("[%s] Shopify problem - stopping this supplier: %s", name, e)
    except Exception as e:
        log.error("[%s] failed: %s", name, e)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        if pending:
            log.info("[%s] uploading the last %d new products to Shopify", name, len(pending))
            try:
                upload_rows(name, sheet, shop, pending)
            except (ShopifyError, KeyboardInterrupt) as e:
                log.error("[%s] last batch not uploaded (%s) - it is uploaded on the next run", name, e)
        sheet.close()
        listed = sum(1 for _, d in sheet.rows() if str(d.get(SHOPIFY_ID_LABEL) or "").strip())
        log.info("[%s] STEP 1 DONE: %d new products this run; %d of %d products in the tab are in Shopify",
                 name, new, listed, sheet.count)
    return scraper, sheet


# ---------------------------------------------------------------------------------------------- STEP 2
def _missing_runs(status):
    m = re.search(r"missing on supplier site \((\d+)", str(status or ""))
    return int(m.group(1)) if m else 0


def _match(shop_variants, supplier_variants):
    """Pair Shopify variants with the supplier's variants: by SKU, else by position."""
    by_sku = {str(v.get("sku") or "").lower(): v for v in supplier_variants if v.get("sku")}
    pairs = []
    for i, sv in enumerate(shop_variants):
        v = by_sku.get(str(sv.get("sku") or "").lower())
        if v is None and len(shop_variants) == len(supplier_variants):
            v = supplier_variants[i]
        if v is None and len(supplier_variants) == 1:
            v = supplier_variants[0]
        pairs.append((sv, v))
    return pairs


def sync_product(name, shop, pid, p):
    """Compare one product with Shopify and send only the changes. Returns (price_changes, stock_changes)."""
    gid = pid if str(pid).startswith("gid://") else f"gid://shopify/Product/{pid}"
    sp = shop.product_variants(gid)
    if not sp:
        raise ShopifyError("product not found in Shopify (deleted there?)")
    mk = config.markup(name)
    updates, stock, n_price = [], [], 0
    for sv, v in _match(sp["variants"]["nodes"], p.variants or []):
        if v is None:
            continue
        item = sv["inventoryItem"]
        upd = {"id": sv["id"]}
        cost = money(v.get("price") or p.price)
        if cost is not None and float(cost) > 0:
            price = f"{float(cost) * mk:.2f}"
            if abs(float(sv["price"] or 0) - float(price)) > 0.004:
                upd["price"] = price
            old_cost = ((item.get("unitCost") or {}).get("amount"))
            if old_cost is None or abs(float(old_cost) - float(cost)) > 0.004:
                upd.setdefault("inventoryItem", {})["cost"] = cost
            rrp = money(v.get("compare_at_price") or (p.rrp if len(p.variants or []) <= 1 else ""))
            new_cmp = rrp if (rrp and float(rrp) > float(price)) else None
            old_cmp = sv.get("compareAtPrice")
            if (new_cmp or None) != (money(old_cmp) if old_cmp else None):
                upd["compareAtPrice"] = new_cmp
        if not item.get("tracked"):
            upd.setdefault("inventoryItem", {})["tracked"] = True
        if len(upd) > 1:
            updates.append(upd)
            if "price" in upd or "compareAtPrice" in upd:
                n_price += 1
        qty = stock_qty(v.get("in_stock"), p.availability)
        level = item.get("inventoryLevel")
        if level is None:
            shop.activate_inventory(item["id"])
            cur = 0
        else:
            cur = (level.get("quantities") or [{}])[0].get("quantity")
        if cur != qty:
            stock.append((item["id"], qty, cur))
    shop.update_variants(gid, updates)
    shop.set_quantities(stock, note=f"{name}/{datetime.now():%Y-%m-%d}")
    return n_price, len(stock)


def zero_stock(name, shop, pid):
    gid = pid if str(pid).startswith("gid://") else f"gid://shopify/Product/{pid}"
    sp = shop.product_variants(gid)
    if not sp:
        return 0
    items = []
    for sv in sp["variants"]["nodes"]:
        level = sv["inventoryItem"].get("inventoryLevel")
        cur = (level.get("quantities") or [{}])[0].get("quantity") if level else None
        if level and cur != 0:
            items.append((sv["inventoryItem"]["id"], 0, cur))
    shop.set_quantities(items, note=f"{name}/missing")
    return len(items)


def run_sync(name, scraper_cls, make_driver, shop, limit=None):
    """STEP 2 for one supplier: open every listed product by its link and update Shopify."""
    sheet = excel.SupplierSheet(name)
    todo = [(r, d) for r, d in sheet.rows() if str(d.get(SHOPIFY_ID_LABEL) or "").strip() and d.get(URL_LABEL)]
    if limit:
        todo = todo[:limit]
    if not todo:
        log.warning("[%s] STEP 2: no products with a Shopify ID in tab '%s' - run STEP 1 (list) first", name, name)
        sheet.close()
        return None, sheet
    log.info("[%s] STEP 2: checking price + stock of %d listed products (direct links, no browsing)",
             name, len(todo))
    driver = make_driver(config.HEADLESS)
    scraper = None
    done = changed = missing = errors = bad_in_row = 0
    try:
        scraper = scraper_cls(driver)
        scraper.login()
        for r, d in todo:
            url, pid = d[URL_LABEL], str(d[SHOPIFY_ID_LABEL]).strip()
            p = scraper.read_product(url)
            if p is None:
                missing += 1
                bad_in_row += 1
                runs = _missing_runs(d.get(STATUS_LABEL)) + 1
                note = f"missing on supplier site ({runs} runs) {_now()}"
                if runs >= config.MISSING_RUNS_BEFORE_ZERO:
                    try:
                        zero_stock(name, shop, pid)
                        note += " - stock set to 0"
                    except ShopifyError as e:
                        note += f" - stock NOT set to 0: {e}"
                sheet.set(r, STATUS_LABEL, note, save=True)
                log.warning("[%s]   %s: %s", name, note, url)
                if bad_in_row >= 8:
                    log.error("[%s] 8 products in a row could not be read - the site changed or the login "
                              "stopped working. Stopping this supplier; nothing else is changed.", name)
                    break
                continue
            bad_in_row = 0
            try:
                n_price, n_stock = sync_product(name, shop, pid, p)
                done += 1
                changed += 1 if (n_price or n_stock) else 0
                sheet.set(r, "Price (supplier)", p.price)
                sheet.set(r, "RRP / Was", p.rrp)
                sheet.set(r, "Availability", p.availability)
                if p.variants and len(p.variants) > 1:
                    import json
                    sheet.set(r, "Variants (JSON)", json.dumps(p.variants, ensure_ascii=False))
                sheet.set(r, SYNC_LABEL, _now())
                sheet.set(r, STATUS_LABEL, f"synced {_now()}: {n_price} price, {n_stock} stock changes", save=True)
                log.info("[%s]   %s  -> %d price, %d stock changes", name, p.title, n_price, n_stock)
            except ShopifyError as e:
                errors += 1
                sheet.set(r, STATUS_LABEL, f"SYNC ERROR {_now()}: {e}", save=True)
                log.error("[%s]   Shopify update failed for %s: %s", name, p.title, e)
                if "refused" in str(e):
                    break
    except KeyboardInterrupt:
        log.warning("[%s] stopped by you", name)
    except Exception as e:
        log.error("[%s] failed: %s", name, e)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        sheet.close()
        log.info("[%s] STEP 2 DONE: %d checked, %d changed in Shopify, %d missing on supplier site, %d errors",
                 name, done, changed, missing, errors)
    return scraper, sheet
