import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sources = json.loads(
    (ROOT / "sources.json").read_text(encoding="utf-8")
)

results = []

USER_AGENT = (
    "Mozilla/5.0 "
    "(compatible; TCG-Restock-Radar/2.0)"
)


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:
        return response.read().decode(
            "utf-8",
            errors="ignore"
        )


def find_price(text):
    patterns = [
        r'"price"\s*:\s*"?(?P<p>\d{1,5}(?:\.\d{2})?)',
        r'"currentPrice"\s*:\s*\{[^{}]*"price"\s*:\s*(?P<p>\d{1,5}(?:\.\d{2})?)',
        r'\$(?P<p>\d{1,5}\.\d{2})',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.I | re.S
        )

        if match:
            try:
                return float(match.group("p"))
            except ValueError:
                pass

    return None


def appears_in_stock(text):
    page = text.lower()

    negative_words = [
        "out of stock",
        "sold out",
        "currently unavailable",
        "not available",
    ]

    positive_words = [
        '"availability":"http://schema.org/instock"',
        '"availability":"https://schema.org/instock"',
        "add to cart",
        "in stock",
        "pickup",
    ]

    has_negative = any(
        word in page
        for word in negative_words
    )

    has_positive = any(
        word in page
        for word in positive_words
    )

    if has_negative and not has_positive:
        return False

    return has_positive


for source in sources:

    if not source.get("enabled", True):
        continue

    if not source.get("url"):
        continue

    try:

        html = fetch(source["url"])

        price = find_price(html)

        in_stock = appears_in_stock(html)

        msrp = float(source["msrp"])

        if in_stock and price is not None:

            if msrp > 0:
                markup = (
                    (price - msrp) / msrp
                ) * 100
            else:
                markup = 999

            max_markup = float(
                source.get(
                    "max_markup",
                    80
                )
            )

            if markup <= max_markup:

                checked = datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )

                results.append(
                    {
                        "game": source["game"],
                        "area": source["area"],
                        "store": source["store"],
                        "product": source["product"],
                        "price": round(price, 2),
                        "msrp": msrp,
                        "evidence": (
                            "Automated check: "
                            "page appears in stock; "
                            f"checked {checked}"
                        ),
                        "url": source["url"],
                    }
                )

    except Exception as error:

        print(
            "Skipped",
            source.get(
                "product",
                "Unknown product"
            ),
            ":",
            error,
        )

    time.sleep(1)


payload = {
    "generated_at": datetime.now(
        timezone.utc
    ).isoformat(),
    "items": results,
}

(ROOT / "restocks.json").write_text(
    json.dumps(
        payload,
        indent=2
    ),
    encoding="utf-8",
)

print(
    f"Wrote {len(results)} results"
)
