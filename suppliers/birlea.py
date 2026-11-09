from core.base import BaseScraper


class BirleaScraper(BaseScraper):
    name = "birlea"
    base_url = "https://birleatradeshop.com/"
    login_url = "https://birleatradeshop.com/profile/login"
    start_urls = ["https://birleatradeshop.com/"]
