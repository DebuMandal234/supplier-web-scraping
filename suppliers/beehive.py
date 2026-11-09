from core.base import BaseScraper


class BeehiveScraper(BaseScraper):
    name = "beehive"
    base_url = "https://portal.beehivefurniture.co.uk/"
    login_url = "https://portal.beehivefurniture.co.uk/"
    start_urls = ["https://portal.beehivefurniture.co.uk/"]
    # Tune after the first run if needed, e.g.:
    # selectors = {"title": "h1", "price": ".price", "sku": ".sku", "images": ".gallery img"}
