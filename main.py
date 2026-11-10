"""
Supplier sites -> Excel (one tab per supplier) -> Shopify.

STEP 1 (now, then weekly on Sunday): new products -> Excel + Shopify (DRAFT, batches of 20)
  python main.py list flair                    # one supplier
  python main.py list all                      # every supplier, one after another
  python main.py list flair --limit 5          # small test

STEP 2 (daily): price + stock of listed products, opened directly by their link
  python main.py sync flair
  python main.py sync all

Other
  python main.py shopify-check                 # test the Shopify credentials / permissions
  python main.py upload all                    # only upload Excel rows that have no Shopify ID yet
  python main.py schedule                      # create the Windows tasks (Sunday list + daily sync)
  python main.py login heartlands              # just test the login (opens Chrome)
  python main.py scrape heartlands --limit 5   # Excel only, no Shopify
  python main.py scrape heartlands             # full run (all categories, all pages)
  python main.py scrape beehive flair          # only these suppliers
  python main.py scrape all                    # every supplier, one after another
  python main.py scrape flair --fresh          # delete flair's tab and start over
  python main.py cleanup                       # delete old/unneeded files (old JSON, debug, caches)

Output: C:\\Users\\USER1\\Desktop\\WebScraping\\Supplier Data.xlsx
Crawl report: data\\<supplier>_crawl_report.txt
"""
import argparse
import glob
import logging
import os
import shutil
import sys

import config
from core import excel
from core.driver import make_driver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("urllib3").setLevel(logging.ERROR)  # hide harmless "Retrying..." noise after Ctrl+C
log = logging.getLogger("main")

VERSION = "2026-11-10 r17"  # printed at every start: tells you which version of the code is running


def get_scraper_class(name):
    if name == "beehive":
        from suppliers.beehive import BeehiveScraper as C
    elif name == "birlea":
        from suppliers.birlea import BirleaScraper as C
    elif name == "heartlands":
        from suppliers.heartlands import HeartlandsScraper as C
    elif name == "julian_bowen":
        from suppliers.julian_bowen import JulianBowenScraper as C
    elif name == "flair":
        from suppliers.flair import FlairScraper as C
    else:
        raise SystemExit(f"Unknown supplier '{name}'. Choose from: {', '.join(config.SUPPLIERS)}")
    return C


def suppliers_from(arg):
    """arg is a list like ["beehive", "flair"] or ["all"]."""
    arg = [arg] if isinstance(arg, str) else arg
    return config.SUPPLIERS if "all" in arg else list(dict.fromkeys(arg))


def write_report(name, scraper, total):
    s = scraper.stats
    os.makedirs(config.DATA_DIR, exist_ok=True)
    lines = [
        f"Supplier: {name}",
        f"Pages visited: {s['pages']}  (listing/category pages: {s['listing_pages']})",
        f"URLs found in sitemap: {s['sitemap_urls']}",
        f"New products this run: {s['products']}   Total products in Excel tab: {total}",
        f"Failed pages: {len(s['failed'])}",
        "",
    ] + s["failed"]
    with open(os.path.join(config.DATA_DIR, f"{name}_crawl_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("[%s] pages=%d listing=%d sitemap=%d new=%d failed=%d total=%d", name, s["pages"],
             s["listing_pages"], s["sitemap_urls"], s["products"], len(s["failed"]), total)


def cmd_login(args):
    log.info("Supplier scraper version %s", VERSION)
    driver = make_driver(config.HEADLESS)
    try:
        scraper = get_scraper_class(args.supplier)(driver)
        scraper.login()
        scraper.dump_debug("after_login")
        log.info("Login OK. Screenshot saved in %s", os.path.join(config.DATA_DIR, "debug"))
    except Exception as e:
        log.error("%s", e)
    finally:
        driver.quit()


def cmd_probe(args):
    """Open ONE page after login and show how the scraper reads it (for testing / fixing a supplier)."""
    log.info("Supplier scraper version %s", VERSION)
    driver = make_driver(config.HEADLESS)
    try:
        scraper = get_scraper_class(args.supplier)(driver)
        scraper.login()
        driver.get(args.url)
        scraper.pause()
        scraper.dismiss_cookies()
        is_prod = scraper.is_product_page()
        print("\nPAGE:", driver.current_url)
        print("SIGNALS:", getattr(scraper, "last_signals", {}))
        print("RECOGNISED AS PRODUCT PAGE:", is_prod)
        if is_prod:
            p = scraper.extract_product()
            for k in ("title", "brand", "sku", "price", "rrp", "category", "breadcrumbs", "availability", "weight"):
                print(f"  {k}: {getattr(p, k)}")
            print("  images:", len(p.images), "| option names:", p.option_names, "| variants:", len(p.variants))
            for v in p.variants[:3]:
                print("   variant:", v)
            print("  specs:", dict(list(p.specs.items())[:8]))
            if args.save:
                sheet = excel.SupplierSheet(args.supplier)
                sheet.start()
                sheet.add(p)
                sheet.close()
                print(f"  -> saved to tab '{args.supplier}' of {config.EXCEL_PATH}")
        scraper.dump_debug("probe")
        print("Screenshot + HTML saved in", os.path.join(config.DATA_DIR, "debug"), "(files *_probe.*)")
    except Exception as e:
        log.error("%s", e)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def cmd_scrape(args):
    log.info("Supplier scraper version %s", VERSION)
    for name in suppliers_from(args.supplier):
        try:
            sheet = excel.SupplierSheet(name, fresh=args.fresh)
        except PermissionError as e:
            log.error("%s", e)
            return
        log.info("[%s] every product is saved to tab '%s' of %s the moment it is scraped (SAVE_EVERY=%d)",
                 name, name, config.EXCEL_PATH, config.SAVE_EVERY)
        if sheet.count:
            log.info("[%s] %d products already in Excel - they will be skipped", name, sheet.count)
        driver = make_driver(config.HEADLESS)
        scraper = None
        try:
            scraper = get_scraper_class(name)(driver)
            scraper.login()
            sheet.start()  # the tab appears in Excel now, not only after the first product
            for p in scraper.crawl(limit=args.limit, known_urls=sheet.known_urls):
                why = sheet.is_known(p)
                if why:
                    log.info("[%s]   already in Excel (%s) - skipped: %s", name, why, p.title)
                    continue
                sheet.add(p)  # written + saved to Excel right now
        except KeyboardInterrupt:
            log.warning("[%s] stopped by you - %d products are in Excel (tab '%s')", name, sheet.count, name)
        except Exception as e:  # keep going with the next supplier
            log.error("[%s] failed: %s", name, e)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
            sheet.close()
            if sheet.in_backup:
                log.error("[%s] DATA IS IN THE BACKUP FILE: %s  (Excel was open). Close Excel and run the "
                          "same command again - it will copy everything into the main file.", name, sheet.backup)
            elif sheet.count:
                log.info("[%s] DONE: %d products are in tab '%s' of %s", name, sheet.count, name, config.EXCEL_PATH)
            if scraper:
                write_report(name, scraper, sheet.count)


def _shop():
    from core.shopify import ShopifyClient, ShopifyError
    try:
        return ShopifyClient()
    except ShopifyError as e:
        log.error("Shopify is not set up: %s", e)
        return None


def cmd_shopify_check(args):
    from core.shopify import ShopifyError
    log.info("Supplier scraper version %s  (Shopify API %s)", VERSION, config.SHOPIFY_API_VERSION)
    shop = _shop()
    if not shop:
        return
    try:
        info, scopes, missing, loc = shop.check()
    except ShopifyError as e:
        log.error("Shopify check failed: %s", e)
        return
    log.info("Connected to %s (%s), currency %s", info["name"], info["myshopifyDomain"], info["currencyCode"])
    log.info("App permissions: %s", ", ".join(scopes))
    log.info("Stock location: %s", loc or "NOT AVAILABLE - products are listed without stock numbers "
             "(add the read_locations scope or set SHOPIFY_LOCATION_ID in .env)")
    if missing:
        log.error("MISSING permissions: %s - add them to the app in Shopify, then install/update it", ", ".join(missing))
    else:
        log.info("Shopify is ready. New products will be created as %s. Stock/price sync: %s. "
                 "Collections: %s.", config.SHOPIFY_PRODUCT_STATUS,
                 "ON" if config.SHOPIFY_ALLOW_SYNC else "OFF", "ON" if config.SHOPIFY_CREATE_COLLECTIONS else "OFF")


def cmd_list(args):
    """STEP 1: new products -> Excel -> Shopify (batches)."""
    from core import jobs
    log.info("Supplier scraper version %s - STEP 1 (list new products)", VERSION)
    shop = _shop()
    if not shop:
        return
    for name in suppliers_from(args.supplier):
        try:
            scraper, sheet = jobs.run_list(name, get_scraper_class(name), make_driver, shop, limit=args.limit)
        except PermissionError as e:
            log.error("%s", e)
            return
        except KeyboardInterrupt:
            log.warning("stopped by you")
            return
        if scraper:
            write_report(name, scraper, sheet.count)


def cmd_upload(args):
    """Only upload Excel rows that have no Shopify ID yet (no browsing)."""
    from core import jobs
    from core.shopify import ShopifyError
    log.info("Supplier scraper version %s - upload waiting rows", VERSION)
    shop = _shop()
    if not shop:
        return
    for name in suppliers_from(args.supplier):
        try:
            sheet = excel.SupplierSheet(name)
        except PermissionError as e:
            log.error("%s", e)
            return
        try:
            ok, failed = jobs.upload_waiting(name, sheet, shop)
            log.info("[%s] upload done: %d listed, %d failed", name, ok, failed)
        except (ShopifyError, KeyboardInterrupt) as e:
            log.error("[%s] upload stopped: %s", name, e)
        finally:
            sheet.close()


def cmd_sync(args):
    """STEP 2: price + stock of listed products -> Shopify."""
    from core import jobs
    log.info("Supplier scraper version %s - STEP 2 (daily price + stock sync)", VERSION)
    if not config.SHOPIFY_ALLOW_SYNC:
        log.error("STEP 2 (stock/price updates in Shopify) is switched OFF. To allow it, set "
                  "SHOPIFY_ALLOW_SYNC=true in .env.")
        return
    shop = _shop()
    if not shop:
        return
    for name in suppliers_from(args.supplier):
        try:
            jobs.run_sync(name, get_scraper_class(name), make_driver, shop, limit=args.limit)
        except PermissionError as e:
            log.error("%s", e)
            return
        except KeyboardInterrupt:
            log.warning("stopped by you")
            return


def cmd_schedule(args):
    """Create (or replace) the two Windows scheduled tasks."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    py = os.path.join(here, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    main_py = os.path.join(here, "main.py")
    logs = os.path.join(config.DATA_DIR, "logs")
    os.makedirs(logs, exist_ok=True)
    tasks = [
        ("Supplier scraper - STEP 1 weekly list", f'cmd /c ""{py}" "{main_py}" list all >> "{logs}\\list.log" 2>&1"',
         ["/sc", "weekly", "/d", "SUN", "/st", args.list_time]),
        ("Supplier scraper - STEP 2 daily sync", f'cmd /c ""{py}" "{main_py}" sync all >> "{logs}\\sync.log" 2>&1"',
         ["/sc", "daily", "/st", args.sync_time]),
    ]
    for name, cmd, when in tasks:
        full = ["schtasks", "/create", "/f", "/tn", name, "/tr", cmd] + when
        try:
            r = subprocess.run(full, capture_output=True, text=True)
            if r.returncode == 0:
                log.info("Scheduled: %s (%s)", name, " ".join(when[1:]))
            else:
                log.error("Could not create '%s': %s", name, (r.stderr or r.stdout).strip())
        except FileNotFoundError:
            log.error("schtasks not found - this command works on Windows only")
            return
    log.info("Logs of scheduled runs go to %s. Change or delete the tasks in Windows Task Scheduler.", logs)


def cmd_cleanup(args):
    """Delete files the program no longer needs: old JSON data, debug screenshots, test files, caches."""
    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(here, config.DATA_DIR)
    targets = (glob.glob(os.path.join(data, "*.json")) + [os.path.join(data, "shopify_payloads"),
               os.path.join(data, "debug"), os.path.join(here, "test_flair.bat")]
               + glob.glob(os.path.join(here, "**", "__pycache__"), recursive=True))
    removed = 0
    for t in targets:
        if ".venv" in t or not os.path.exists(t):
            continue
        try:
            shutil.rmtree(t) if os.path.isdir(t) else os.remove(t)
            removed += 1
            log.info("deleted %s", os.path.relpath(t, here))
        except OSError as e:
            log.warning("could not delete %s (%s)", t, e)
    log.info("cleanup done: %d items deleted. Your Excel file, code and .env were not touched.", removed)


def main():
    ap = argparse.ArgumentParser(description="Supplier scraper -> Excel -> Shopify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    choices = config.SUPPLIERS + ["all"]

    s = sub.add_parser("login", help="test the login only")
    s.add_argument("supplier", choices=config.SUPPLIERS)
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("probe", help="test ONE page: how is it read? (add --save to put it in Excel)")
    s.add_argument("supplier", choices=config.SUPPLIERS)
    s.add_argument("url")
    s.add_argument("--save", action="store_true")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("scrape", help="log in and scrape products into the Excel file")
    s.add_argument("supplier", nargs="+", choices=choices, help="one or more suppliers, or: all")
    s.add_argument("--limit", type=int, default=None, help="stop after N new products (for testing)")
    s.add_argument("--fresh", action="store_true", help="delete this supplier's Excel tab and start over")
    s.set_defaults(fn=cmd_scrape)

    s = sub.add_parser("list", help="STEP 1: new products -> Excel -> Shopify (DRAFT, batches of 20)")
    s.add_argument("supplier", nargs="+", choices=choices, help="one or more suppliers, or: all")
    s.add_argument("--limit", type=int, default=None, help="stop after N new products (for testing)")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("sync", help="STEP 2: price + stock of listed products -> Shopify")
    s.add_argument("supplier", nargs="+", choices=choices, help="one or more suppliers, or: all")
    s.add_argument("--limit", type=int, default=None, help="only the first N products (for testing)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("upload", help="upload Excel rows that have no Shopify ID yet (no browsing)")
    s.add_argument("supplier", nargs="+", choices=choices, help="one or more suppliers, or: all")
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("shopify-check", help="test the Shopify connection and permissions")
    s.set_defaults(fn=cmd_shopify_check)

    s = sub.add_parser("schedule", help="create the Windows tasks: list every Sunday, sync every day")
    s.add_argument("--list-time", default="02:00", help="Sunday start time for STEP 1 (default 02:00)")
    s.add_argument("--sync-time", default="06:00", help="daily start time for STEP 2 (default 06:00)")
    s.set_defaults(fn=cmd_schedule)

    s = sub.add_parser("cleanup", help="delete old JSON / debug / test files")
    s.set_defaults(fn=cmd_cleanup)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
