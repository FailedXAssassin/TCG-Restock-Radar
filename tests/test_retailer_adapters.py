import unittest

from retailer_adapters import BestBuyAdapter, bestbuy_product_id, classify_title


class AdapterFoundationTests(unittest.TestCase):
    def test_classifies_supported_pack_products_without_pack_count(self):
        self.assertEqual(
            classify_title("Pokemon TCG Mega Evolution Ascended Heroes Elite Trainer Box"),
            ("Pokemon", "elite_trainer_box"),
        )
        self.assertEqual(
            classify_title("Bandai One Piece Card Game OP-15 Sleeved Booster Pack"),
            ("One Piece", "sleeved_booster"),
        )

    def test_extracts_legacy_and_current_best_buy_product_ids(self):
        self.assertEqual(
            bestbuy_product_id("https://www.bestbuy.com/product/example/ABC123/sku/6678102"),
            "6678102",
        )
        self.assertEqual(
            bestbuy_product_id("https://www.bestbuy.com/product/example/C3747QS8ZY"),
            "C3747QS8ZY",
        )

    def test_discovery_only_accepts_relevant_product_links(self):
        html = '''
        <a href="/product/pokemon-tcg-ascended-heroes-etb/AA11">Pokemon TCG Ascended Heroes Elite Trainer Box</a>
        <a href="/product/pikachu-sleeves/BB22">Pokemon card sleeves</a>
        <a href="/product/mtg-booster-box/CC33">Magic: The Gathering Foundations Booster Box</a>
        '''
        products = BestBuyAdapter().discover_products(html, "https://www.bestbuy.com/site/searchpage.jsp?st=tcg")
        self.assertEqual([p.retailer_product_id for p in products], ["AA11", "CC33"])
        self.assertEqual(products[0].product_type, "elite_trainer_box")

    def test_inventory_requires_explicit_first_party_seller(self):
        html = '''
        <script type="application/ld+json">
        {"@type":"Product","sku":"6678102","offers":{"price":"49.99","availability":"https://schema.org/InStock","seller":{"name":"Best Buy"}}}
        </script>
        '''
        result = BestBuyAdapter().check_inventory(html, "https://www.bestbuy.com/product/example/X/sku/6678102")
        self.assertEqual(result.status, "in_stock")
        self.assertTrue(result.first_party_seller)

    def test_sku_specific_sold_out_state_beats_stale_structured_availability(self):
        html = '''
        <script type="application/ld+json">
        {"@type":"Product","sku":"6678102","offers":{"price":"41.99","availability":"https://schema.org/InStock","seller":{"name":"Best Buy"}}}
        </script>
        <button data-testid="pdp-sold-out-6678102">Sold Out</button>
        <script>window.data={"skuId":"6678102","displayableCustomerPrice":41.99}</script>
        '''
        result = BestBuyAdapter().check_inventory(html, "https://www.bestbuy.com/product/example/X/sku/6678102")
        self.assertEqual(result.status, "sold_out")
        self.assertEqual(result.price, 41.99)


    def test_marketplace_or_ambiguous_offer_stays_unknown(self):
        html = '''
        <script type="application/ld+json">
        {"@type":"Product","sku":"6678102","offers":{"price":"49.99","availability":"https://schema.org/InStock","seller":{"name":"A Random Seller"}}}
        </script>
        '''
        result = BestBuyAdapter().check_inventory(html, "https://www.bestbuy.com/product/example/X/sku/6678102")
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.first_party_seller)


if __name__ == "__main__":
    unittest.main()
