from discovery_rules import discovery_quality
from retailer_adapters import InventoryObservation


def test_clean_english_first_party_candidate_is_eligible():
    candidate = {"title": "One Piece OP-17 Booster Box", "tcg": "One Piece", "product_type": "booster_box"}
    obs = InventoryObservation(status="in_stock", price=139.99, seller="Best Buy", first_party_seller=True)
    result = discovery_quality(candidate, obs)
    assert result["eligible"] is True
    assert result["language"] == "English"
    assert result["condition"] == "Sealed"


def test_japanese_listing_is_not_promoted():
    candidate = {"title": "One Piece OP-17 Booster Box Japanese Version", "tcg": "One Piece", "product_type": "booster_box"}
    obs = InventoryObservation(status="in_stock", seller="Best Buy", first_party_seller=True)
    result = discovery_quality(candidate, obs)
    assert result["eligible"] is False
    assert "non_english_listing" in result["reasons"]


def test_repack_is_not_promoted():
    candidate = {"title": "Pokemon Mystery Repack Box", "tcg": "Pokemon", "product_type": "booster_box"}
    obs = InventoryObservation(status="in_stock", seller="Best Buy", first_party_seller=True)
    result = discovery_quality(candidate, obs)
    assert result["eligible"] is False
    assert "suspicious_or_repacked" in result["reasons"]


def test_marketplace_or_unverified_seller_is_not_promoted():
    candidate = {"title": "Magic Collector Booster Box", "tcg": "Magic", "product_type": "booster_box"}
    obs = InventoryObservation(status="in_stock", seller="Third Party", first_party_seller=False)
    result = discovery_quality(candidate, obs)
    assert result["eligible"] is False
    assert "seller_not_verified_first_party" in result["reasons"]
