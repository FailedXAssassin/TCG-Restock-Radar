import json
import unittest
from datetime import date

from parsers import parse_target, parse_walmart


def page(data):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"


def walmart(offer, secondary=None):
    offer = {"usItemId": "123", **offer, "secondaryOffers": secondary or []}
    return page({"props": {"pageProps": {"initialData": {"data": {"product": offer}}}}})


def target(item, sections):
    data = {"props": {"dehydratedState": {"queries": [{"state": {"data": {
        "product": {"tcin": "456", "item": item},
        "module": {"tcin": "456", "three_up_sections": sections},
    }}}]}}}
    return page(data)


def target_with_unrelated_price(item, sections):
    data = {"props": {"dehydratedState": {"queries": [{"state": {"data": {
        "product": {"tcin": "456", "item": item},
        "module": {"tcin": "456", "three_up_sections": sections},
        "recommendation": {"tcin": "999", "price": {"current_retail": 99.99}},
    }}}]}}}
    return page(data)


class WalmartParserTests(unittest.TestCase):
    def test_retail_offer_is_in_stock(self):
        result = parse_walmart(walmart({"sellerName": "Walmart.com", "availabilityStatus": "IN_STOCK",
                                        "priceInfo": {"currentPrice": {"price": 49.99}}}), "123")
        self.assertEqual(result["status"], "in_stock")
        self.assertEqual(result["price"], 49.99)

    def test_fulfilled_marketplace_is_not_retail(self):
        result = parse_walmart(walmart({"sellerName": "Rares Market", "sellerType": "EXTERNAL", "wfsEnabled": True,
                                        "availabilityStatus": "IN_STOCK", "priceInfo": {"currentPrice": {"price": 160}}}), "123")
        self.assertEqual(result["status"], "marketplace_in_stock")
        self.assertEqual(result["price"], 160)

    def test_mismatched_item_is_unknown(self):
        result = parse_walmart(walmart({"sellerName": "Walmart.com", "availabilityStatus": "IN_STOCK"}), "999")
        self.assertEqual(result["status"], "unknown")


class TargetParserTests(unittest.TestCase):
    def test_future_release_is_loaded(self):
        result = parse_target(target({"mmbv_content": {"street_date": "2026-10-30"}}, []), "456", date(2026, 9, 13))
        self.assertEqual(result["status"], "loaded")

    def test_empty_fulfillment_remains_loaded(self):
        result = parse_target(target({"product_description": {"title": "Trading cards"}}, []), "456", date(2026, 9, 13))
        self.assertEqual(result["status"], "loaded")

    def test_available_fulfillment_is_in_stock(self):
        result = parse_target(target({}, [{"availability_status": "IN_STOCK"}]), "456", date(2026, 9, 13))
        self.assertEqual(result["status"], "in_stock")

    def test_unrelated_price_is_not_attached(self):
        result = parse_target(target_with_unrelated_price({}, [{"availability_status": "IN_STOCK"}]), "456")
        self.assertIsNone(result["price"])


if __name__ == "__main__":
    unittest.main()
