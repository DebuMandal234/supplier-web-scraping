from core.base import BaseScraper


class HeartlandsScraper(BaseScraper):
    """WooCommerce site: standard My Account login form, variations read from the product form."""
    name = "heartlands"
    base_url = "https://www.heartlandsfurniture.co.uk/"
    login_url = "https://www.heartlandsfurniture.co.uk/my-account/"
    start_urls = ["https://www.heartlandsfurniture.co.uk/", "https://www.heartlandsfurniture.co.uk/shop/"]
    user_selector = "#username"
    pass_selector = "#password"
    submit_selector = "button[name=login]"
    product_url_regex = r"/product/"
    next_selectors = ["a.next.page-numbers"]
    selectors = {
        "title": "h1.product_title",
        "price": "p.price",
        "sku": ".sku",
        "images": ".woocommerce-product-gallery img",
        "description": "#tab-description, .woocommerce-product-details__short-description",
        "short_description": ".woocommerce-product-details__short-description",
    }
