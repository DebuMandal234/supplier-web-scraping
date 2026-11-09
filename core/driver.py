from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def make_driver(headless: bool = False):
    """Chrome driver. Selenium 4.6+ downloads the matching chromedriver automatically."""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--lang=en-GB")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver
