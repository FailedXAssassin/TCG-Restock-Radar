"""Conservative retailer parsers for public product-page structured data."""

import html
import json
import re
from datetime import date


PARSER_VERSION = "2026.09.13.1"


def _next_data(text):
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        text,
        re.I | re.S,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (TypeError, json.JSONDecodeError):
        return None


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _price(offer):
    price_info = offer.get("priceInfo") or {}
    current = price_info.get("currentPrice") or {}
    value = current.get("price")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _available(offer):
    states = [offer.get("availabilityStatus"), offer.get("itemPageAvailabilityStatus")]
    states.extend(x.get("availabilityStatus") for x in (offer.get("fulfillmentOptions") or []) if isinstance(x, dict))
    states = {str(x).upper() for x in states if x}
    if states & {"IN_STOCK", "AVAILABLE"}:
        return True
    if states & {"OUT_OF_STOCK", "NOT_AVAILABLE", "UNAVAILABLE"}:
        return False
    return None


def _is_walmart(offer):
    names = {str(offer.get(k, "")).strip().lower() for k in ("sellerName", "sellerDisplayName")}
    return bool(names & {"walmart", "walmart.com"})


def _target_price(value):
    for node in _walk(value):
        if not isinstance(node, dict):
            continue
        for key in ("current_retail", "formatted_current_price"):
            if key not in node:
                continue
            try:
                return float(str(node[key]).replace("$", ""))
            except (TypeError, ValueError):
                pass
    return None


def parse_walmart(text, expected_item_id=None):
    data = _next_data(text)
    try:
        product = data["props"]["pageProps"]["initialData"]["data"]["product"]
    except (TypeError, KeyError):
        return {"status": "unknown", "price": None, "seller": None,
                "evidence": "Walmart structured product data was unavailable", "parser_version": PARSER_VERSION}

    item_id = str(product.get("usItemId", ""))
    if expected_item_id and item_id != str(expected_item_id):
        return {"status": "unknown", "price": None, "seller": None,
                "evidence": "Walmart structured data did not match the requested item", "parser_version": PARSER_VERSION}

    offers = [product] + [x for x in (product.get("secondaryOffers") or []) if isinstance(x, dict)]
    retail = [x for x in offers if _is_walmart(x)]
    retail_live = [x for x in retail if _available(x) is True]
    if retail_live:
        offer = retail_live[0]
        return {"status": "in_stock", "price": _price(offer), "seller": "Walmart.com",
                "evidence": "Structured offer is sold by Walmart and available", "parser_version": PARSER_VERSION}
    if retail and all(_available(x) is False for x in retail):
        return {"status": "sold_out", "price": _price(retail[0]), "seller": "Walmart.com",
                "evidence": "Structured Walmart-owned offer is unavailable", "parser_version": PARSER_VERSION}

    marketplace = [x for x in offers if not _is_walmart(x) and _available(x) is True]
    if marketplace:
        offer = marketplace[0]
        seller = offer.get("sellerDisplayName") or offer.get("sellerName") or "Marketplace seller"
        return {"status": "marketplace_in_stock", "price": _price(offer), "seller": seller,
                "evidence": "Only a third-party Marketplace offer is confidently available", "parser_version": PARSER_VERSION}
    return {"status": "unknown", "price": None, "seller": None,
            "evidence": "No associated Walmart-owned offer could be classified", "parser_version": PARSER_VERSION}


def parse_target(text, expected_tcin=None, today=None):
    data = _next_data(text)
    if not data:
        return {"status": "unknown", "price": None, "seller": "Target",
                "evidence": "Target structured product data was unavailable", "parser_version": PARSER_VERSION}
    tcin = str(expected_tcin or "")
    products, fulfillment_sections = [], []
    for node in _walk(data):
        if not isinstance(node, dict) or (tcin and str(node.get("tcin", "")) != tcin):
            continue
        if "item" in node:
            products.append(node)
        if "three_up_sections" in node:
            fulfillment_sections.append(node.get("three_up_sections") or [])
    if not products:
        return {"status": "not_found", "price": None, "seller": "Target",
                "evidence": "No matching Target product record was found", "parser_version": PARSER_VERSION}

    product, item = products[0], products[0].get("item") or {}
    street = (item.get("mmbv_content") or {}).get("street_date")
    if street:
        try:
            if date.fromisoformat(street) > (today or date.today()):
                return {"status": "loaded", "price": None, "seller": "Target", "street_date": street,
                        "evidence": f"Target product record is loaded for {street}", "parser_version": PARSER_VERSION}
        except ValueError:
            pass

    available_words = {"IN_STOCK", "AVAILABLE", "LIMITED_STOCK"}
    unavailable_words = {"OUT_OF_STOCK", "NOT_AVAILABLE", "UNAVAILABLE"}
    status_values = []
    for node in _walk(fulfillment_sections):
        if isinstance(node, dict):
            status_values.extend(str(node.get(k, "")).upper() for k in ("availability_status", "availabilityStatus") if node.get(k))
    # Search only within the matching product and fulfillment objects so a
    # recommended item's price can never be attached to this TCIN.
    price = _target_price([product, fulfillment_sections])
    if any(x in available_words for x in status_values):
        return {"status": "in_stock", "price": price, "seller": "Target",
                "evidence": "Target fulfillment data reports availability", "parser_version": PARSER_VERSION}
    if status_values and all(x in unavailable_words for x in status_values):
        return {"status": "sold_out", "price": price, "seller": "Target",
                "evidence": "Target fulfillment data reports unavailable", "parser_version": PARSER_VERSION}
    if fulfillment_sections and all(not section for section in fulfillment_sections):
        return {"status": "sold_out", "price": price, "seller": "Target",
                "evidence": "Target product exists but has no available fulfillment section", "parser_version": PARSER_VERSION}
    title = html.unescape((item.get("product_description") or {}).get("title", ""))
    return {"status": "loaded" if title else "unknown", "price": price, "seller": "Target",
            "evidence": "Target product record exists but availability is ambiguous", "parser_version": PARSER_VERSION}
