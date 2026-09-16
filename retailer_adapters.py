"""Retailer adapter contracts and conservative Best Buy public-data support.

Adapters only consume public product/category pages.  They deliberately return
UNKNOWN when a page does not contain enough evidence to make a retailer-direct
availability claim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import unescape
import json
import re
from typing import Iterable
from urllib.parse import urljoin, urlparse

from parsers import parse_target, parse_walmart


INVENTORY_STATES = {"in_stock", "sold_out", "loaded", "unknown", "error", "not_found", "blocked", "marketplace_in_stock"}

PRODUCT_TYPE_RULES = (
    ("ultra_premium_collection", r"ultra[- ]premium|upc"),
    ("premium_collection", r"premium collection"),
    ("elite_trainer_box", r"elite trainer box|\betb\b"),
    ("booster_bundle", r"booster bundle"),
    ("booster_box", r"booster (?:box|display)|display box"),
    ("three_pack_blister", r"3[- ]pack|three[- ]pack|blister"),
    ("sleeved_booster", r"sleeved booster"),
    ("booster_pack", r"booster pack|\bbooster\b"),
    ("mini_tin", r"mini tin"),
    ("tin", r"\btin\b"),
    ("ultra_premium_collection", r"super[- ]premium"),
    ("collection", r"collection|collector chest"),
    ("deck", r"deck|battle deck|starter kit|draft night"),
    ("bundle", r"\bbundle\b"),
)

GAME_RULES = (
    ("Pokemon", r"pok[eé]mon|pokemon tcg|pokemon trading card"),
    ("One Piece", r"one piece (?:card game|tcg|trading card)|\bop-\d+"),
    ("Magic", r"magic:?(?: the)? gathering|\bmtg\b|wizards of the coast"),
)

# Deterministic known-set recognition. Unknown names remain reviewable rather
# than being guessed; discovery never needs a pack-count to be eligible.
SET_RULES = (
    ("Ascended Heroes", r"ascended heroes"),
    ("Prismatic Evolutions", r"prismatic evolutions|\bprismatic\b"),
    ("Phantasmal Flames", r"phantasmal flames|\bphantasmal\b"),
    ("Pitch Black", r"pitch black"),
    ("Perfect Order", r"perfect order"),
    ("30th Anniversary", r"30th (?:anniversary|celebration)"),
    ("Surging Sparks", r"surging sparks"),
    ("Black Bolt", r"black bolt"),
    ("White Flare", r"white flare"),
    ("Destined Rivals", r"destined rivals"),
    ("Chaos Rising", r"chaos rising"),
)


@dataclass(frozen=True)
class NormalizedProduct:
    retailer: str
    retailer_product_id: str
    title: str
    product_url: str
    tcg: str
    set_name: str = ""
    product_type: str = "other_pack_product"
    image_url: str | None = None
    discovery_source: str = "manual"
    first_party_seller: bool = False

    @property
    def canonical_key(self) -> str:
        return f"{self.retailer.lower()}:{self.retailer_product_id}"

    def to_dict(self):
        return asdict(self) | {"canonical_key": self.canonical_key}


@dataclass(frozen=True)
class InventoryObservation:
    status: str = "unknown"
    price: float | None = None
    seller: str | None = None
    first_party_seller: bool = False
    shipping_available: bool | None = None
    pickup_available: bool | None = None
    evidence: str = ""
    retailer_product_id: str | None = None

    def to_dict(self):
        return asdict(self)


def classify_title(title: str) -> tuple[str, str]:
    value = unescape(title or "").lower()
    game = next((name for name, pattern in GAME_RULES if re.search(pattern, value, re.I)), "Other")
    product_type = next((name for name, pattern in PRODUCT_TYPE_RULES if re.search(pattern, value, re.I)), "other_pack_product")
    return game, product_type


def classify_set(title: str) -> str:
    value = unescape(title or "").lower()
    return next((name for name, pattern in SET_RULES if re.search(pattern, value, re.I)), "")


def walmart_product_id(url: str) -> str | None:
    match = re.search(r"/ip/(?:[^/?]+/)?(\d+)", urlparse(url).path, re.I)
    return match.group(1) if match else None


def target_tcin(url: str) -> str | None:
    match = re.search(r"/A-(\d+)", urlparse(url).path, re.I)
    return match.group(1) if match else None


def bestbuy_product_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.lower().endswith("bestbuy.com"):
        return None
    match = re.search(r"/sku/(\d+)(?:[/?]|$)", parsed.path, re.I)
    if match:
        return match.group(1)
    # Best Buy's newer public product URLs can use a stable opaque product key.
    match = re.search(r"/product/[^/]+/([A-Za-z0-9]+)(?:[/?]|$)", parsed.path, re.I)
    return match.group(1) if match else None


def _json_ld_nodes(html_text: str) -> Iterable[dict]:
    for raw in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html_text, re.I | re.S):
        try:
            value = json.loads(unescape(raw).strip())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for node in values:
            if isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if isinstance(graph, list):
                    yield from (item for item in graph if isinstance(item, dict))


def _offer_list(node: dict) -> list[dict]:
    offers = node.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    return [offer for offer in offers if isinstance(offer, dict)]


class RetailerAdapter:
    retailer = "Unknown"
    supports_discovery = False
    supports_inventory = False
    supports_local_inventory = False

    def capabilities(self):
        return {
            "retailer": self.retailer,
            "discovery": self.supports_discovery,
            "inventory": self.supports_inventory,
            "local_inventory": self.supports_local_inventory,
        }

    def discover_products(self, public_html: str, source_url: str) -> list[NormalizedProduct]:
        return []

    def check_inventory(self, public_html: str, product_url: str) -> InventoryObservation:
        return InventoryObservation(evidence="No parser is available for this retailer")

    def get_local_inventory(self, public_html: str, product_url: str, location=None):
        # Local availability frequently depends on selected-store state. A
        # retailer must opt in with a verified public source before returning
        # automated local results; community reports remain separate.
        return {"status": "unsupported", "stores": [], "source": "automated"}


class WalmartAdapter(RetailerAdapter):
    retailer = "Walmart"
    supports_inventory = True

    def check_inventory(self, public_html: str, product_url: str) -> InventoryObservation:
        result = parse_walmart(public_html, walmart_product_id(product_url))
        return InventoryObservation(
            status=result["status"], price=result.get("price"), seller=result.get("seller"),
            first_party_seller=result.get("seller") in {"Walmart", "Walmart.com"},
            evidence=result.get("evidence", ""), retailer_product_id=walmart_product_id(product_url),
        )


class TargetAdapter(RetailerAdapter):
    retailer = "Target"
    supports_inventory = True

    def check_inventory(self, public_html: str, product_url: str) -> InventoryObservation:
        result = parse_target(public_html, target_tcin(product_url))
        return InventoryObservation(
            status=result["status"], price=result.get("price"), seller=result.get("seller"),
            first_party_seller=result.get("seller") == "Target",
            evidence=result.get("evidence", ""), retailer_product_id=target_tcin(product_url),
        )


class BestBuyAdapter(RetailerAdapter):
    retailer = "Best Buy"
    # Search/category pages are public discovery surfaces. This adapter does
    # not call undocumented endpoints and is disabled until explicitly enabled.
    supports_discovery = True

    def discover_products(self, public_html: str, source_url: str) -> list[NormalizedProduct]:
        seen, found = set(), []
        for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", public_html, re.I | re.S):
            url = urljoin(source_url, unescape(href))
            product_id = bestbuy_product_id(url)
            if not product_id or product_id in seen:
                continue
            title = re.sub(r"<[^>]+>", " ", unescape(label))
            title = re.sub(r"\s+", " ", title).strip()
            game, product_type = classify_title(title)
            if game == "Other" or product_type == "other_pack_product":
                continue
            seen.add(product_id)
            found.append(NormalizedProduct(
                retailer=self.retailer,
                retailer_product_id=product_id,
                title=title,
                product_url=url.split("?")[0],
                tcg=game,
                product_type=product_type,
                discovery_source="best_buy_public_search",
            ))
        return found

    def check_inventory(self, public_html: str, product_url: str) -> InventoryObservation:
        expected_id = bestbuy_product_id(product_url)
        page = unescape(public_html)
        explicit_first_party = bool(re.search(r"(?:sold by|ships from)\s+best buy\b", page, re.I))
        # A visible SKU-specific PDP state is more current than stale JSON-LD
        # availability, which can lag after a product sells out.
        if expected_id and re.search(rf"data-testid=[\"']pdp-sold-out-{re.escape(expected_id)}(?:-label)?[\"']", page, re.I):
            price_match = re.search(
                rf'"displayableCustomerPrice"\s*:\s*(\d+(?:\.\d{{1,2}})?).*?"skuId"\s*:\s*"{re.escape(expected_id)}"|'
                rf'"skuId"\s*:\s*"{re.escape(expected_id)}".*?"displayableCustomerPrice"\s*:\s*(\d+(?:\.\d{{1,2}})?)',
                page, re.I | re.S,
            )
            value = next((item for item in (price_match.groups() if price_match else ()) if item), None)
            return InventoryObservation(
                "sold_out", float(value) if value else None, "Best Buy", True,
                evidence="Best Buy public product page displays the SKU-specific Sold Out control",
                retailer_product_id=expected_id,
            )
        for node in _json_ld_nodes(page):
            if str(node.get("@type", "")).lower() != "product":
                continue
            sku = str(node.get("sku") or node.get("productID") or "")
            # Ignore JSON-LD records that are not explicitly tied to this SKU;
            # Best Buy pages can include in-stock related/recommended products.
            if expected_id and sku != expected_id:
                continue
            for offer in _offer_list(node):
                availability = str(offer.get("availability", "")).lower()
                seller_node = offer.get("seller")
                seller = seller_node.get("name") if isinstance(seller_node, dict) else seller_node
                seller = str(seller or "").strip() or None
                first_party = explicit_first_party or bool(seller and seller.lower() == "best buy")
                price = offer.get("price")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                if "instock" in availability:
                    if first_party:
                        return InventoryObservation("in_stock", price, seller or "Best Buy", True, evidence="Best Buy public Product JSON-LD confirms a retailer-direct in-stock offer", retailer_product_id=expected_id)
                    return InventoryObservation("unknown", None, seller, False, evidence="An in-stock offer was present but Best Buy seller identity was not explicit", retailer_product_id=expected_id)
                if "outofstock" in availability:
                    return InventoryObservation("sold_out", price, seller or ("Best Buy" if first_party else None), first_party, evidence="Best Buy public Product JSON-LD reports the offer unavailable", retailer_product_id=expected_id)
        return InventoryObservation("unknown", evidence="Best Buy public product data did not provide an offer tied to a verified first-party seller", retailer_product_id=expected_id)


class UnsupportedRetailerAdapter(RetailerAdapter):
    """Explicit placeholder: no discovery/monitoring until public data is verified."""

    def __init__(self, retailer):
        self.retailer = retailer

    def check_inventory(self, public_html: str, product_url: str) -> InventoryObservation:
        return InventoryObservation(
            evidence=f"{self.retailer} adapter is not verified for automated inventory yet"
        )


ADAPTERS = {
    "Best Buy": BestBuyAdapter(),
    "Walmart": WalmartAdapter(),
    "Target": TargetAdapter(),
    "GameStop": UnsupportedRetailerAdapter("GameStop"),
    "Pokemon Center": UnsupportedRetailerAdapter("Pokemon Center"),
    "Amazon": UnsupportedRetailerAdapter("Amazon"),
    "CVS": UnsupportedRetailerAdapter("CVS"),
    "Walgreens": UnsupportedRetailerAdapter("Walgreens"),
    "Costco": UnsupportedRetailerAdapter("Costco"),
    "Sam's Club": UnsupportedRetailerAdapter("Sam's Club"),
    "Dick's Sporting Goods": UnsupportedRetailerAdapter("Dick's Sporting Goods"),
}


def adapter_for(retailer: str) -> RetailerAdapter | None:
    return ADAPTERS.get(retailer)


def adapter_capabilities():
    return [adapter.capabilities() for adapter in ADAPTERS.values()]
