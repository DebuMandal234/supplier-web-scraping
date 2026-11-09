from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


@dataclass
class Product:
    supplier: str
    url: str
    title: str = ""
    handle: str = ""
    sku: str = ""
    ean: str = ""
    brand: str = ""
    category: str = ""
    breadcrumbs: str = ""
    tags: list = field(default_factory=list)
    price: str = ""             # supplier price as shown on the site (no markup)
    rrp: str = ""               # "was" / RRP price if shown
    currency: str = "GBP"
    availability: str = ""
    weight: str = ""            # kg
    short_description: str = ""
    description: str = ""       # HTML
    description_text: str = ""  # plain text
    meta_description: str = ""
    images: list = field(default_factory=list)
    options: dict = field(default_factory=dict)   # dropdowns seen on page: {"Size": ["3ft", "4ft6"]}
    specs: dict = field(default_factory=dict)     # spec tables / "Key: value" lists
    option_names: list = field(default_factory=list)  # Shopify option names (max 3), e.g. ["Colour", "Size"]
    # each variant: {"options": [...], "sku", "price", "compare_at_price", "barcode", "weight", "image", "in_stock"}
    variants: list = field(default_factory=list)
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
