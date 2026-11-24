"""Product titles in Proper Case:  "ADA TEDDY ACCENT CHAIR CREAM" / "ada teddy accent chair" -> "Ada Teddy Accent Chair Cream".

Kept readable:
  * sizes with numbers stay lower case:      150CM -> 150cm,  4FT6 -> 4ft6,  2X -> 2x,  3-DRAWER -> 3-Drawer
  * known short codes stay upper case:       TV, LED, MDF, USB, UK, XL, 3D ...
  * product codes (letters + digits) upper:  ANY150WHT stays ANY150WHT (only when it has no vowel pattern)
  * words after - / ( & are capitalised too: "high-gloss" -> "High-Gloss", "(set of 2)" -> "(Set Of 2)"
"""
import re

UPPER = {"TV", "LED", "MDF", "USB", "UK", "EU", "XL", "XXL", "UV", "PU", "PVC", "DIY", "3D", "HD", "II", "III", "IV",
         "N/A", "CBM", "USB-C", "UPF", "MFC", "OSB", "RGB"}
LOWER_UNITS = r"(?:cm|mm|m|ft|in|kg|g|x|l|ml|pc|pcs|pk|seater|ft\d+|st|nd|rd|th)"


def _word(w):
    if not w:
        return w
    up = w.upper()
    if up in UPPER:
        return up
    if re.fullmatch(r"\d+(?:[.,/]\d+)?" + LOWER_UNITS + r"?", w, re.I) or re.fullmatch(r"\d+(?:ft)\d+", w, re.I):
        return w.lower()                                   # 150CM -> 150cm, 4FT6 -> 4ft6, 2ND -> 2nd
    if re.fullmatch(r"\d+(?:[.,]\d+)?[x×]\d+(?:[.,]\d+)?(?:cm|mm|m)?", w, re.I):
        return w.lower()                                   # 90X190CM -> 90x190cm
    if re.search(r"\d", w) and re.fullmatch(r"[A-Za-z0-9]+", w) and not re.search(r"[aeiou]{2}", w, re.I) \
            and len(w) >= 5 and re.search(r"[A-Za-z]\d|\d[A-Za-z]{2,}", w):
        return up                                          # product code like ANY150WHT
    return w[:1].upper() + w[1:].lower()


def proper_case(title):
    """Proper Case for a product title (idempotent)."""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        return t
    # split on spaces but also capitalise after - / ( & + so "high-gloss" -> "High-Gloss"
    parts = re.split(r"(\s+|[-/(&+])", t)
    return "".join(p if re.fullmatch(r"\s+|[-/(&+]", p or " ") else _word(p) for p in parts)
