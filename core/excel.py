"""Excel is the ONLY storage. Every product is appended to the supplier's tab and the file is saved
immediately (atomically), so a crash or Ctrl+C never loses more than the product in progress."""
import json
import logging
import os
import re
import time

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import config
from core.payload import build_payload

log = logging.getLogger("excel")

# (key, header)
FIXED_COLUMNS = [
    ("supplier", "Supplier"), ("sku", "SKU"), ("title", "Title"), ("handle", "Handle"),
    ("brand", "Vendor / Brand"), ("category", "Product type / Category"), ("tags_text", "Tags"),
    ("price", "Price (supplier)"), ("shopify_price", "Price (Shopify, with markup)"), ("rrp", "RRP / Was"),
    ("currency", "Currency"), ("availability", "Availability"), ("ean", "EAN / Barcode"), ("weight", "Weight (kg)"),
    ("variant_count", "Variant count"), ("option_names_text", "Option names"), ("variants_json", "Variants (JSON)"),
    ("short_description", "Short description"), ("description_text", "Description (text)"),
    ("description", "Description (HTML)"), ("meta_description", "Meta description"),
    ("image_count", "Image count"), ("images_text", "Image URLs"), ("options_json", "Dropdown options (JSON)"),
    ("specs_json", "Specs (JSON)"), ("breadcrumbs", "Breadcrumbs"), ("url", "Product URL"),
    ("scraped_at", "Scraped at"), ("payload_json", "Shopify payload (JSON, for POST products.json)"),
]
URL_LABEL = "Product URL"
PAYLOAD_LABEL = "Shopify payload (JSON, for POST products.json)"
CELL_LIMIT = 32000  # Excel max is 32767 characters per cell
HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="305496")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    return str(v)[:CELL_LIMIT]


def _values(p):
    payload = build_payload(p)
    d = p.to_dict()
    pj = json.dumps(payload, ensure_ascii=False)
    d.update({
        "tags_text": payload["product"]["tags"],
        "shopify_price": payload["product"]["variants"][0].get("price", ""),
        "variant_count": len(payload["product"]["variants"]),
        "option_names_text": ", ".join(p.option_names),
        "variants_json": json.dumps(p.variants, ensure_ascii=False) if len(p.variants) > 1 else "",
        "image_count": len(p.images),
        "images_text": " | ".join(p.images),
        "options_json": json.dumps(p.options, ensure_ascii=False) if p.options else "",
        "specs_json": json.dumps(p.specs, ensure_ascii=False) if p.specs else "",
        "payload_json": pj if len(pj) <= CELL_LIMIT else "TOO LONG FOR ONE EXCEL CELL (description/images too large)",
    })
    return d


def _width(label):
    wide = label in ("Title", "Handle", "Tags", "Breadcrumbs") or label.startswith("Spec:")
    small = label in ("Supplier", "SKU", "Currency", "Weight (kg)", "Variant count", "Image count") \
        or label.startswith("Price")
    return 40 if wide else (14 if small else 25)


def _last_data_row(ws):
    """Last row holding a real value (0 = sheet is empty). Ignores rows that only keep formatting."""
    for r in range(ws.max_row, 0, -1):
        if any(c.value not in (None, "") for c in ws[r]):
            return r
    return 0


class SupplierSheet:
    """Appends products of one supplier to its own tab of the Excel file."""

    def __init__(self, supplier, fresh=False):
        self.supplier = supplier
        self.path = config.EXCEL_PATH
        self.pending = 0
        self.count = 0
        self.known_urls = set()
        self.ws = None
        self.cols = {}
        self._last_warn = 0
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

        self.backup = os.path.splitext(self.path)[0] + " - backup.xlsx"
        self.in_backup = False
        if os.path.exists(self.path):
            try:
                with open(self.path, "ab"):   # fails on Windows while the file is open in Excel
                    pass
            except PermissionError:
                raise PermissionError(f"'{self.path}' is open in Excel (or another program). "
                                      f"CLOSE it, then run the command again.")
        src = self.path if os.path.exists(self.path) else None
        if os.path.exists(self.backup) and (src is None or os.path.getmtime(self.backup) > os.path.getmtime(src)):
            src = self.backup
            self.pending = 1   # make sure the main file gets rewritten from the backup
            log.warning("Found a newer backup file (saved while Excel was open) - continuing from it: %s", self.backup)
        self.wb = self._open_workbook(src)
        self.new_tab = False
        self.next_row = 2
        log.info("Excel file: %s   (tabs now: %s)", self.path, ", ".join(self.wb.sheetnames) or "none")
        if supplier in self.wb.sheetnames:
            ws = self.wb[supplier]
            last = _last_data_row(ws)
            header = [c.value for c in ws[1]] if last >= 1 else []
            idx = self.wb.sheetnames.index(supplier)
            if fresh or last <= 1:
                # deleted / header-only / empty tab: nothing to keep -> recreate it clean, same position
                del self.wb[supplier]
                self._ensure_sheet(index=idx)
            elif PAYLOAD_LABEL not in header:
                # tab with data written by an older version of this program: keep it, start a clean tab
                old = f"{supplier}_old"[:31]
                if old in self.wb.sheetnames:
                    del self.wb[old]
                ws.title = old
                log.warning("[%s] old-format tab renamed to '%s'; starting a fresh tab", supplier, old)
                self._ensure_sheet(index=idx + 1)
            else:
                self._adopt(ws, last)

    def _open_workbook(self, src):
        if src is None:
            return Workbook()
        if os.path.getsize(src) == 0:
            log.warning("Excel file is empty (0 bytes) - starting a new workbook in it")
            return Workbook()
        try:
            return load_workbook(src)
        except PermissionError:
            raise
        except Exception as ex:   # not a valid .xlsx
            bad = os.path.splitext(src)[0] + " - unreadable.xlsx"
            try:
                os.replace(src, bad)
            except OSError:
                pass
            log.error("Excel file could not be read (%s). Moved it to '%s' and started a new one.", ex, bad)
            return Workbook()

    # ---- sheet setup
    def _activate(self):
        for sh in self.wb.worksheets:
            sh.sheet_view.tabSelected = (sh is self.ws)
        self.wb.active = self.wb.worksheets.index(self.ws)

    def _adopt(self, ws, last):
        self.ws = ws
        self.next_row = last + 1
        header = [c.value for c in ws[1]]
        self.cols = {h: i + 1 for i, h in enumerate(header) if h}
        for _, label in FIXED_COLUMNS:
            self._ensure_col(label)
        if URL_LABEL in self.cols:
            col = self.cols[URL_LABEL]
            for (u,) in ws.iter_rows(min_row=2, min_col=col, max_col=col, values_only=True):
                if u:
                    self.known_urls.add(u)
        self.count = len(self.known_urls)
        self._activate()

    def _ensure_sheet(self, index=None):
        if self.ws is None:
            ws = self.wb.create_sheet(self.supplier, index)
            self.ws = ws
            self.cols = {}
            self.next_row = 2
            self.new_tab = True
            for _, label in FIXED_COLUMNS:
                self._ensure_col(label)
            ws.freeze_panes = "D2"
            for sh in list(self.wb.worksheets):   # drop the empty "Sheet1" Excel/openpyxl start with
                if sh is not ws and re.fullmatch(r"Sheet\d*", sh.title) and _last_data_row(sh) == 0:
                    del self.wb[sh.title]
            self._activate()

    def _ensure_col(self, label):
        if label in self.cols:
            return
        idx = len(self.cols) + 1
        self.cols[label] = idx
        c = self.ws.cell(row=1, column=idx, value=label)
        c.font, c.fill = HEAD_FONT, HEAD_FILL
        self.ws.column_dimensions[get_column_letter(idx)].width = _width(label)

    # ---- writing
    def start(self):
        """Make sure the supplier's tab (with headers) exists in the file right now, even before product #1."""
        self._ensure_sheet()
        if self.new_tab:
            self.pending = max(self.pending, 1)
            if self.flush():
                self.new_tab = False

    def add(self, product):
        """Append one product row and save right away (every SAVE_EVERY products)."""
        self._ensure_sheet()
        for k in product.specs:
            self._ensure_col(f"Spec: {k}")
        d = _values(product)
        row = [""] * len(self.cols)
        for key, label in FIXED_COLUMNS:
            row[self.cols[label] - 1] = _cell(d.get(key))
        for k, v in product.specs.items():
            row[self.cols[f"Spec: {k}"] - 1] = _cell(v)
        for i, v in enumerate(row, start=1):
            self.ws.cell(row=self.next_row, column=i, value=v)
        self.next_row += 1
        self.known_urls.add(product.url)
        self.count += 1
        self.pending += 1
        if self.pending >= config.SAVE_EVERY:
            self.flush()

    def flush(self):
        if self.pending == 0 or not self.wb.sheetnames:
            return True
        if self.ws is not None:
            self.ws.auto_filter.ref = self.ws.dimensions
        tmp = self.path + ".tmp"
        self.wb.save(tmp)  # write a complete new file first ...
        try:
            os.replace(tmp, self.path)  # ... then swap it in: the real file is never half-written
        except PermissionError:
            try:
                os.replace(tmp, self.backup)
            except OSError:
                pass
            self.in_backup = True
            now = time.time()
            if now - self._last_warn > 20:
                self._last_warn = now
                log.error("EXCEL FILE IS OPEN - CLOSE IT! Your data is being saved to '%s' instead. "
                          "It goes into the main file again as soon as you close Excel.", self.backup)
            return False
        self.pending = 0
        if self.in_backup or os.path.exists(self.backup):
            self.in_backup = False
            try:
                os.remove(self.backup)
            except OSError:
                pass
            log.info("Excel file is writable again - all data is in the main file.")
        log.info("Saved to Excel: tab '%s' now has %d products  (%s)", self.supplier, self.count, self.path)
        return True

    def close(self):
        try:
            self.flush()
        except Exception as e:
            log.error("final save failed: %s", e)
