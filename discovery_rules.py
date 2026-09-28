"""Safety/quality gates for products found by TCG Radar discovery.

Discovery is intentionally broader than monitoring. Candidates can be stored for
review, but only clean, supported TCG products with trustworthy first-party
inventory evidence may be promoted into the fast monitor.
"""
from __future__ import annotations

import re

SUPPORTED_GAMES = {"Pokemon", "One Piece", "Magic"}
SUSPICIOUS_TITLE_PATTERNS = (
    r"\brepack(?:ed)?\b",
    r"\bmystery\s+(?:pack|box|lot)\b",
    r"\bproxy\b",
    r"\bcustom\s+(?:pack|box|lot)\b",
    r"\bresealed\b",
    r"\bused\b",
)
NON_ENGLISH_MARKERS = (
    r"\bjapanese\b",
    r"\bjapan(?:ese)?\s+version\b",
    r"\bjp\s+version\b",
    r"\bchinese\b",
    r"\bkorean\b",
)


def discovery_quality(candidate: dict, observation=None) -> dict:
    """Return a deterministic promotion decision and human-readable reasons."""
    title = str(candidate.get("title") or candidate.get("product") or "").strip()
    game = str(candidate.get("tcg") or candidate.get("game") or "").strip()
    product_type = str(candidate.get("product_type") or "other_pack_product").strip()
    reasons = []

    if not title:
        reasons.append("missing_title")
    if game not in SUPPORTED_GAMES:
        reasons.append("unsupported_game")
    if product_type == "other_pack_product":
        reasons.append("unclassified_product")
    if any(re.search(pattern, title, re.I) for pattern in SUSPICIOUS_TITLE_PATTERNS):
        reasons.append("suspicious_or_repacked")
    if any(re.search(pattern, title, re.I) for pattern in NON_ENGLISH_MARKERS):
        reasons.append("non_english_listing")

    if observation is not None:
        status = getattr(observation, "status", None) if not isinstance(observation, dict) else observation.get("status")
        first_party = getattr(observation, "first_party_seller", None) if not isinstance(observation, dict) else observation.get("first_party_seller")
        shipping = getattr(observation, "shipping_available", None) if not isinstance(observation, dict) else observation.get("shipping_available")
        pickup = getattr(observation, "pickup_available", None) if not isinstance(observation, dict) else observation.get("pickup_available")
        if first_party is not True:
            reasons.append("seller_not_verified_first_party")
        if status not in {"in_stock", "sold_out", "loaded"}:
            reasons.append("inventory_not_verified")
        if status == "in_stock" and shipping is False and pickup is False:
            reasons.append("not_orderable")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "language": "English" if not any(r == "non_english_listing" for r in reasons) else "Non-English",
        "condition": "Sealed" if not any(r == "suspicious_or_repacked" for r in reasons) else "Rejected",
    }
