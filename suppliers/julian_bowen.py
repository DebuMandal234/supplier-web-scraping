from core.base import BaseScraper


class JulianBowenScraper(BaseScraper):
    """Trade portal lives on its own sub-domain: trade.julian-bowen.co.uk"""
    name = "julian_bowen"
    base_url = "https://trade.julian-bowen.co.uk/"
    login_url = "https://trade.julian-bowen.co.uk/login/"
    start_urls = ["https://trade.julian-bowen.co.uk/"]
