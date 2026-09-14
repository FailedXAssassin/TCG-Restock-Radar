import asyncio
import hashlib
import json
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from parsers import PARSER_VERSION, parse_target, parse_walmart


ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Mobile Safari/537.36 "
    "TCG-Radar/3.0"
)

# Product state kept in memory for this first deployment.
# We will replace this with persistent storage after Railway is confirmed working.
products = {}
retailer_health = {}

scheduler_task = None
http_client = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def source_id(source):
    raw = (
        f"{source.get('store', '')}|"
        f"{source.get('product', '')}|"
        f"{source.get('url', '')}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def retailer_name(url, fallback="Unknown"):
    try:
        host = urlparse(url).netloc.lower()

        if "walmart" in host:
            return "Walmart"
        if "target" in host:
            return "Target"
        if "bestbuy" in host:
            return "Best Buy"
        if "gamestop" in host:
            return "GameStop"
        if "amazon" in host:
            return "Amazon"

    except Exception:
        pass

    return fallback or "Unknown"


def interval_for(source):
    """
    High priority:
        30-60 seconds
    Normal:
        60-120 seconds
    Low:
        180-300 seconds

    Products intentionally receive slightly different timings
    so one retailer is not hit all at once.
    """

    priority = str(
        source.get("priority", "normal")
    ).lower()

    if priority == "high":
        low, high = 30, 60
    elif priority == "low":
        low, high = 180, 300
    else:
        low, high = 60, 120

    seed = source_id(source)
    rng = random.Random(seed)

    return rng.randint(low, high)


def load_sources():
    if not SOURCES_FILE.exists():
        return []

    try:
        raw = json.loads(
            SOURCES_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        print("Could not read sources.json:", exc)
        return []

    if not isinstance(raw, list):
        return []

    valid = []

    for source in raw:
        if not isinstance(source, dict):
            continue

        if not source.get("enabled", True):
            continue

        url = str(source.get("url", "")).strip()

        if not url.startswith(("http://", "https://")):
            continue

        valid.append(source)

    return valid


def extract_price(text):
    patterns = [
        r'"price"\s*:\s*"?(?P<p>\d{1,5}(?:\.\d{2})?)',
        r'"currentPrice"\s*:\s*\{[^{}]*"price"\s*:\s*(?P<p>\d{1,5}(?:\.\d{2})?)',
        r'\$(?P<p>\d{1,5}\.\d{2})',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.I | re.S,
        )

        if match:
            try:
                return float(match.group("p"))
            except ValueError:
                pass

    return None
def detect_quantity(text):
    """
    Quantity is only returned when a clear public quantity
    signal is present. We never invent stock counts.
    """

    patterns = [
        r'"quantity"\s*:\s*(\d{1,4})',
        r'"availableQuantity"\s*:\s*(\d{1,4})',
        r'"inventoryQuantity"\s*:\s*(\d{1,4})',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.I,
        )

        if match:
            try:
                qty = int(match.group(1))

                if 0 <= qty <= 9999:
                    return qty

            except ValueError:
                pass

    return None


def detect_status(status_code, text, store=None):
    page = text.lower()

    if status_code == 404:
        return "not_found"

    if status_code in (403, 429):
        return "blocked"

    if status_code >= 500 and len(text) < 1000:
        return "error"

        # Walmart: generic "add to cart" text can belong to
    # unrelated marketplace offers, so require stronger signals.
    if store and store.lower() == "walmart":
        walmart_in_stock = (
            '"availability":"http://schema.org/instock"' in page
            or '"availability":"https://schema.org/instock"' in page
        )

        walmart_sold_out = (
            '"availability":"http://schema.org/outofstock"' in page
            or '"availability":"https://schema.org/outofstock"' in page
        )

        if walmart_in_stock and not walmart_sold_out:
            return "in_stock"

        if walmart_sold_out and not walmart_in_stock:
            return "sold_out"

            return "unknown"
        
    in_stock_signals = [
        '"availability":"http://schema.org/instock"',
        '"availability":"https://schema.org/instock"',
        '"availability": "http://schema.org/instock"',
        '"availability": "https://schema.org/instock"',
        "add to cart",
        "add-to-cart",
    ]

    sold_out_signals = [
        "out of stock",
        "sold out",
        "currently unavailable",
        "temporarily unavailable",
        '"availability":"http://schema.org/outofstock"',
        '"availability":"https://schema.org/outofstock"',
    ]

    coming_soon_signals = [
        "coming soon",
        "not yet available",
        "release date",
        "preorder",
        "pre-order",
    ]

    has_in_stock = any(
        signal in page
        for signal in in_stock_signals
    )

    has_sold_out = any(
        signal in page
        for signal in sold_out_signals
    )

    has_coming_soon = any(
        signal in page
        for signal in coming_soon_signals
    )

    if has_in_stock and not has_sold_out:
        return "in_stock"

    if has_coming_soon:
        return "loaded"

    if has_sold_out:
        return "sold_out"

    # A real page exists, but we cannot safely classify
    # whether it is purchasable yet.
    if status_code == 200 and len(text) > 1000:
        return "loaded"

    return "unknown"


def get_retailer_health(name):
    if name not in retailer_health:
        retailer_health[name] = {
            "retailer": name,
            "status": "healthy",
            "backoff_multiplier": 1.0,
            "successful_checks": 0,
            "failed_checks": 0,
            "rate_limits": 0,
            "blocked_checks": 0,
            "last_check": None,
            "last_success": None,
            "last_failure": None,
            "latest_http_status": None,
            "latency_ms": None,
            "consecutive_failures": 0,
            "next_eligible_check": None,
            "last_error": None,
        }

    return retailer_health[name]


def mark_success(name, status_code=None, latency_ms=None):
    health = get_retailer_health(name)

    health["successful_checks"] += 1
    health["last_check"] = now_iso()
    health["last_success"] = health["last_check"]
    health["latest_http_status"] = status_code
    health["latency_ms"] = latency_ms
    health["consecutive_failures"] = 0
    health["last_error"] = None

    if health["backoff_multiplier"] > 1:
        health["backoff_multiplier"] = max(
            1.0,
            health["backoff_multiplier"] * 0.8,
        )

    health["status"] = (
        "healthy"
        if health["backoff_multiplier"] <= 1.25
        else "slowed"
    )


def mark_failure(name, reason, status_code=None, latency_ms=None):
    health = get_retailer_health(name)

    health["failed_checks"] += 1
    health["last_check"] = now_iso()
    health["last_failure"] = health["last_check"]
    health["latest_http_status"] = status_code
    health["latency_ms"] = latency_ms
    health["consecutive_failures"] += 1
    health["last_error"] = reason

    if status_code == 429:
        health["rate_limits"] += 1

    if status_code == 403:
        health["blocked_checks"] += 1

    health["backoff_multiplier"] = min(
        8.0,
        max(
            1.5,
            health["backoff_multiplier"] * 1.75,
        ),
    )

    if status_code in (403, 429):
        health["status"] = "blocked"
    elif health["consecutive_failures"] >= 3:
        health["status"] = "backing_off"
    else:
        health["status"] = "slowed"


async def check_product(source):
    url = source["url"]

    store = retailer_name(
        url,
        source.get("store", "Unknown"),
    )

    health = get_retailer_health(store)

    product_key = source_id(source)

    previous = products.get(
        product_key,
        {},
    )

    started = time.monotonic()

    try:
        response = await http_client.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,"
                    "application/xhtml+xml,"
                    "application/json"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=20,
        )

        elapsed_ms = round(
            (time.monotonic() - started) * 1000
        )

        parser_result = None

        if store.lower() == "walmart":
            item_match = re.search(r"/ip/(?:[^/?]+/)?(\d+)", url)
            parser_result = parse_walmart(
                response.text,
                item_match.group(1) if item_match else None,
            )
            status = parser_result["status"]
            price = parser_result["price"]

        elif store.lower() == "target":
            tcin_match = re.search(r"/A-(\d+)", url)
            parser_result = parse_target(
                response.text,
                tcin_match.group(1) if tcin_match else None,
            )
            status = parser_result["status"]
            price = parser_result["price"]

        else:
            status = detect_status(
                response.status_code,
                response.text,
                store,
            )

            price = extract_price(
                response.text
            )

        quantity = detect_quantity(
            response.text
        )

        if status == "blocked":
            mark_failure(
                store,
                f"HTTP {response.status_code}",
                response.status_code,
                elapsed_ms,
            )
        elif response.status_code >= 400:
            mark_failure(
                store,
                f"HTTP {response.status_code}",
                response.status_code,
                elapsed_ms,
            )
        else:
            mark_success(store, response.status_code, elapsed_ms)

        msrp = safe_float(
            source.get("msrp")
        )

        markup = None

        if (
            price is not None
            and msrp is not None
            and msrp > 0
        ):
            markup = round(
                ((price - msrp) / msrp) * 100,
                1,
            )

        old_status = previous.get("status")

        changed = (
            old_status is not None
            and old_status != status
        )

        products[product_key] = {
            "id": product_key,
            "game": source.get(
                "game",
                source.get("category", "Other"),
            ),
            "category": source.get(
                "category",
                source.get("game", "Other"),
            ),
            "area": source.get(
                "area",
                "Online",
            ),
            "store": store,
            "product": source.get(
                "product",
                "Unnamed product",
            ),
            "url": url,
            "status": status,
            "previous_status": old_status,
            "status_changed": changed,
            "price": price,
            "msrp": msrp,
            "markup": markup,
            "quantity": quantity,
            "priority": source.get(
                "priority",
                "normal",
            ),
            "base_interval_seconds": interval_for(
                source
            ),
            "checked_at": now_iso(),
            "response_ms": elapsed_ms,
            "http_status": response.status_code,
            "seller": parser_result.get("seller") if parser_result else store,
            "parser_version": PARSER_VERSION if parser_result else "generic-1",
            "evidence": (
                f"{parser_result.get('evidence') if parser_result else 'Public product page checked'}; "
                f"checked {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            ),
        }

    except Exception as exc:
        mark_failure(
            store,
            str(exc),
        )

        products[product_key] = {
            "id": product_key,
            "game": source.get(
                "game",
                source.get("category", "Other"),
            ),
            "category": source.get(
                "category",
                source.get("game", "Other"),
            ),
            "area": source.get(
                "area",
                "Online",
            ),
            "store": store,
            "product": source.get(
                "product",
                "Unnamed product",
            ),
            "url": url,
            "status": "error",
            "previous_status": previous.get(
                "status"
            ),
            "status_changed": False,
            "price": previous.get("price"),
            "msrp": safe_float(
                source.get("msrp")
            ),
            "markup": previous.get("markup"),
            "quantity": None,
            "priority": source.get(
                "priority",
                "normal",
            ),
            "base_interval_seconds": interval_for(
                source
            ),
            "checked_at": now_iso(),
            "response_ms": None,
            "http_status": None,
            "evidence": f"Check failed: {exc}",
        }


async def scheduler():
    print("TCG Radar scheduler started")

    next_checks = {}
    last_priority_pulse = None

    while True:
        sources = load_sources()

        if not sources:
            await asyncio.sleep(5)
            continue

        current = time.monotonic()

        wall_clock = datetime.now(timezone.utc)
        pulse = f"{wall_clock:%Y-%m-%dT%H}:{wall_clock.minute // 15}"

        if (
            wall_clock.minute % 15 == 0
            and wall_clock.second < 8
            and pulse != last_priority_pulse
        ):
            high_priority = [
                source for source in sources
                if str(source.get("priority", "normal")).lower() == "high"
            ]
            random.shuffle(high_priority)
            for index, source in enumerate(high_priority):
                next_checks[source_id(source)] = current + 2 + (index * 3) + random.uniform(0, 2)
            last_priority_pulse = pulse

        for source in sources:
            key = source_id(source)

            if key not in next_checks:
                # Initial staggering prevents all products
                # from firing simultaneously after startup.
                next_checks[key] = (
                    current
                    + random.uniform(1, 15)
                )

            if current < next_checks[key]:
                continue

            store = retailer_name(
                source["url"],
                source.get("store", "Unknown"),
            )

            health = get_retailer_health(
                store
            )

            await check_product(source)

            base = interval_for(source)

            multiplier = health[
                "backoff_multiplier"
            ]

            next_interval = (
                base * multiplier
            )

            # Small scheduling spread.
            jitter = random.uniform(
                0.95,
                1.05,
            )

            next_checks[key] = (
                time.monotonic()
                + next_interval * jitter
            )

            health["next_eligible_check"] = datetime.fromtimestamp(
                time.time() + next_interval * jitter,
                timezone.utc,
            ).isoformat()

            # Never hammer multiple products at the exact
            # same instant.
            await asyncio.sleep(1)

        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_task
    global http_client

    http_client = httpx.AsyncClient(
        follow_redirects=True,
    )

    scheduler_task = asyncio.create_task(
        scheduler()
    )

    yield

    if scheduler_task:
        scheduler_task.cancel()

    if http_client:
        await http_client.aclose()


app = FastAPI(
    title="TCG Radar API",
    version="3.1.0",
    lifespan=lifespan,
)


# Open during initial testing.
# We will restrict this to the GitHub Pages origin later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "name": "TCG Radar",
        "version": "3.1.0",
        "status": "online",
        "message": "TCG Radar backend is running.",
    }


@app.get("/health")
async def health():
    sources = load_sources()

    return {
        "status": "online",
        "version": "3.1.0",
        "time": now_iso(),
        "configured_products": len(
            sources
        ),
        "tracked_products": len(
            products
        ),
        "retailers": len(
            retailer_health
        ),
    }


@app.get("/api/feed")
async def feed():
    items = list(
        products.values()
    )

    items.sort(
        key=lambda item: item.get(
            "checked_at",
            "",
        ),
        reverse=True,
    )

    counts = {
        "total": len(items),
        "in_stock": sum(
            1
            for item in items
            if item.get("status") == "in_stock"
        ),
        "loaded": sum(
            1
            for item in items
            if item.get("status") == "loaded"
        ),
        "sold_out": sum(
            1
            for item in items
            if item.get("status") == "sold_out"
        ),
        "marketplace_in_stock": sum(
            1
            for item in items
            if item.get("status") == "marketplace_in_stock"
        ),
        "errors": sum(
            1
            for item in items
            if item.get("status")
            in (
                "error",
                "blocked",
            )
        ),
    }

    return {
        "generated_at": now_iso(),
        "counts": counts,
        "items": items,
    }


@app.get("/api/retailer-health")
async def retailer_status():
    sources = load_sources()
    counts = {}
    for source in sources:
        store = retailer_name(source["url"], source.get("store", "Unknown"))
        counts[store] = counts.get(store, 0) + 1
    for store, health in retailer_health.items():
        health["monitored_product_count"] = counts.get(store, 0)
    return {
        "generated_at": now_iso(),
        "retailers": list(
            retailer_health.values()
        ),
  }
