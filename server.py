import asyncio
import hashlib
import math
import html
import json
import os
import secrets
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from parsers import PARSER_VERSION, parse_target, parse_walmart
from retailer_adapters import adapter_capabilities, adapter_for, bestbuy_product_id


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES_FILE = ROOT / "sources.json"
DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ROOT))
SOURCES_FILE = Path(os.environ.get("TCG_RADAR_DATA_PATH", DATA_DIR / "sources.json"))
PUSH_SUBSCRIPTIONS_FILE = Path(os.environ.get("TCG_RADAR_PUSH_SUBSCRIPTIONS_PATH", DATA_DIR / "push_subscriptions.json"))
MODERATORS_FILE = Path(os.environ.get("TCG_RADAR_MODERATORS_PATH", DATA_DIR / "moderators.json"))
OWNER_PIN_FILE = Path(os.environ.get("TCG_RADAR_OWNER_PIN_PATH", DATA_DIR / "owner_pin.json"))
ALERT_HISTORY_FILE = Path(os.environ.get("TCG_RADAR_ALERT_HISTORY_PATH", DATA_DIR / "alert_history.json"))
AUTHORIZED_INTAKE_FILE = Path(os.environ.get("TCG_RADAR_AUTHORIZED_INTAKE_PATH", DATA_DIR / "authorized_intake_sources.json"))
PRIORITY_AUTOMATION_FILE = Path(os.environ.get("TCG_RADAR_PRIORITY_AUTOMATION_PATH", DATA_DIR / "priority_automation.json"))
ADMIN_SECRET = os.environ.get("TCG_RADAR_ADMIN_SECRET", "")
VAPID_PUBLIC_KEY = os.environ.get("TCG_RADAR_VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("TCG_RADAR_VAPID_PRIVATE_KEY", "")
VAPID_CONTACT = os.environ.get("TCG_RADAR_VAPID_CONTACT", "mailto:owner@example.com")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", os.environ.get("TCG_RADAR_GOOGLE_CLIENT_ID", ""))
OWNER_EMAIL = os.environ.get("TCG_RADAR_OWNER_EMAIL", "").strip().lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BEST_BUY_DISCOVERY_ENABLED = os.environ.get("TCG_RADAR_BEST_BUY_DISCOVERY_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
DISCOVERY_INTERVAL_SECONDS = max(3600, int(os.environ.get("TCG_RADAR_DISCOVERY_INTERVAL_SECONDS", "21600")))
DISCOVERY_MAX_PUBLIC_VERIFICATIONS = max(1, min(20, int(os.environ.get("TCG_RADAR_DISCOVERY_MAX_PUBLIC_VERIFICATIONS", "12"))))
LOCAL_STORE_SEARCH_URL = "https://overpass-api.de/api/interpreter"
ZIP_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
LOCAL_SUPPORTED_RETAILERS = {
    "walmart": "Walmart",
    "target": "Target",
    "best buy": "Best Buy",
    "cvs": "CVS",
    "walgreens": "Walgreens",
    "gamestop": "GameStop",
    "barnes & noble": "Barnes & Noble",
}
BEST_BUY_DISCOVERY_URLS = (
    "https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=pokemon+tcg",
    "https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=magic+the+gathering",
    "https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=one+piece+card+game",
)

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
DEFAULT_PRIORITY_AUTOMATION = {
    "auto_high_priority": False,
    "top_limit": 20,
    "trend_source": "not_configured",
    "auto_selected_product_ids": [],
    "updated_at": None,
}
priority_automation = dict(DEFAULT_PRIORITY_AUTOMATION)

# A restock must be observed twice after the product has first been seen
# in a non-purchasable state. This avoids a single bad page response becoming
# an alert, and avoids alerting merely because the service restarted.
RESTOCK_ARMING_STATUSES = {"loaded", "sold_out", "not_found"}
DIRECT_SELLER_NAMES = {
    "walmart": {"walmart", "walmart.com"},
    "target": {"target"},
}


scheduler_task = None
http_client = None
last_discovery_attempt = {}
local_zip_searches = {}
local_zip_sessions = {}
local_scan_cooldowns = {}
zip_geocode_lock = asyncio.Lock()
last_zip_geocode_at = 0.0


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


VERIFIED_PRODUCT_HOSTS = {
    "walmart.com",
    "target.com",
    "bestbuy.com",
    "gamestop.com",
    "amazon.com",
    "costco.com",
    "samsclub.com",
    "cvs.com",
    "walgreens.com",
}

VERIFIED_PRODUCT_PATHS = {
    "walmart.com": re.compile(r"/ip/(?:[^/?]+/)?\d+", re.I),
    "target.com": re.compile(r"/(?:p/)?(?:[^/?]+/)?-?/A-\d+", re.I),
    "bestbuy.com": re.compile(r"/(?:site|product)/", re.I),
    "gamestop.com": re.compile(r"/(?:products?|p)/", re.I),
    "amazon.com": re.compile(r"/(?:dp|gp/product)/", re.I),
    "costco.com": re.compile(r"/(?:p/|[^/]*\.product\.\d+\.html)", re.I),
    "samsclub.com": re.compile(r"/(?:s|ip)/", re.I),
    "cvs.com": re.compile(r"/shop/(?:p/)?[^/]*prodid-\d+", re.I),
    "walgreens.com": re.compile(r"/store/c/", re.I),
}


def canonical_product_url(url):
    """Remove affiliate/tracking data while preserving the official product page."""
    try:
        parsed = urlparse(str(url).strip())
        host = parsed.netloc.lower().split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or "/"

        # Target's "moo" URL is an affiliate-facing variant of the same A-number page.
        if host == "target.com":
            target_match = re.search(r"/A-(\d+)$", path, re.I)
            if target_match:
                path = f"/p/-/A-{target_match.group(1)}"

        # Retailer product pages do not need query strings; these commonly carry
        # affiliate, click, campaign, and session parameters.
        return f"https://www.{host}{path}" if host else ""
    except Exception:
        return ""


def verified_product_url(url):
    """Accept only stable official retailer product pages."""
    try:
        parsed = urlparse(str(url).strip())
        host = parsed.netloc.lower().split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if parsed.scheme != "https" or host not in VERIFIED_PRODUCT_HOSTS:
            return False
        if not parsed.path or not VERIFIED_PRODUCT_PATHS[host].search(parsed.path):
            return False
        return True
    except Exception:
        return False


def priority_for(source):
    """Manual priority now; auto-selected IDs become high when a source is configured."""
    source_priority = str(source.get("priority", "normal")).lower()
    if source_priority not in {"high", "normal", "low"}:
        source_priority = "normal"
    auto_ids = set(priority_automation.get("auto_selected_product_ids") or [])
    if priority_automation.get("auto_high_priority") and source_id(source) in auto_ids:
        return "high"
    return source_priority


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

    priority = priority_for(source)

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

    raw = _all_sources()
    valid = []

    for source in raw:
        if not isinstance(source, dict):
            continue

        if not source.get("enabled", True) or not source.get("published", True):
            continue

        url = str(source.get("url", "")).strip()

        if not verified_product_url(url):
            continue

        valid.append(source)

    return valid





def database_enabled():
    return bool(DATABASE_URL)


def _database_connection():
    import psycopg
    return psycopg.connect(DATABASE_URL)


def ensure_database():
    if not database_enabled():
        return
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_products (id TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS push_subscriptions (endpoint TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_moderators (id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_hash TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_reports (id TEXT PRIMARY KEY, product_id TEXT NOT NULL, reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), resolved BOOLEAN NOT NULL DEFAULT FALSE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_alert_events (id TEXT PRIMARY KEY, product_id TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_support_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, client_id TEXT NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_support_bans (client_id TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT 'Spam', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_users (google_sub TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', nickname TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("ALTER TABLE radar_users ADD COLUMN IF NOT EXISTS nickname TEXT")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS radar_users_nickname_unique ON radar_users (LOWER(nickname)) WHERE nickname IS NOT NULL")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_purchases (id TEXT PRIMARY KEY, google_sub TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_visitors (client_id TEXT PRIMARY KEY, first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_retailer_link_clicks (retailer TEXT PRIMARY KEY, clicks BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_local_zip_searches (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_local_zip_sessions (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, zip_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL)")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_local_scan_cooldowns (client_id TEXT PRIMARY KEY, last_scanned TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_catalog_products (canonical_key TEXT PRIMARY KEY, retailer TEXT NOT NULL, retailer_product_id TEXT NOT NULL, payload JSONB NOT NULL, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_monitor_state (product_id TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_inventory_observations (id TEXT PRIMARY KEY, product_id TEXT NOT NULL, payload JSONB NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE INDEX IF NOT EXISTS radar_inventory_observations_product_time ON radar_inventory_observations (product_id, observed_at DESC)")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_discovery_runs (id TEXT PRIMARY KEY, retailer TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_adapter_health (retailer TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_settings (key TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            # Add new checked-in starter products without overwriting products
            # added by the owner or moderators.
            for source in _file_sources():
                cursor.execute(
                    "INSERT INTO radar_products (id, payload) VALUES (%s, %s::jsonb) ON CONFLICT (id) DO NOTHING",
                    (source_id(source), json.dumps(source)),
                )
                catalog_payload = dict(source)
                catalog_payload.update({
                    "canonical_key": canonical_product_key(source),
                    "retailer": retailer_name(str(source.get("url", "")), str(source.get("store", ""))),
                    "retailer_product_id": bestbuy_product_id(str(source.get("url", ""))) or source_id(source),
                    "discovery_source": "manual",
                    "discovery_status": "active",
                })
                cursor.execute(
                    "INSERT INTO radar_catalog_products (canonical_key, retailer, retailer_product_id, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (canonical_key) DO UPDATE SET last_seen_at = NOW(), payload = EXCLUDED.payload",
                    (catalog_payload["canonical_key"], catalog_payload["retailer"], catalog_payload["retailer_product_id"], json.dumps(catalog_payload)),
                )
        connection.commit()



def canonical_product_key(source):
    """Stable dedupe key that leaves legacy source IDs untouched."""
    url = str(source.get("url", ""))
    store = retailer_name(url, str(source.get("store", "")))
    if store == "Best Buy":
        retailer_product_id = bestbuy_product_id(url)
        if retailer_product_id:
            return f"best buy:{retailer_product_id}"
    return f"url:{hashlib.sha256(url.split('?')[0].encode('utf-8')).hexdigest()[:24]}"


def persist_monitor_state(product_id, state):
    """Persist alert arming/dedupe state when PostgreSQL is configured."""
    if not database_enabled():
        return
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO radar_monitor_state (product_id, payload) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (product_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
                (product_id, json.dumps(state)),
            )
        connection.commit()


def load_monitor_state(product_id):
    if not database_enabled():
        return {}
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM radar_monitor_state WHERE product_id = %s", (product_id,))
            row = cursor.fetchone()
            return row[0] if row else {}


def upsert_catalog_product(product):
    """Store a normalized product candidate without changing the manual monitor list."""
    if not database_enabled():
        return False
    payload = product.to_dict() if hasattr(product, "to_dict") else dict(product)
    key = str(payload["canonical_key"])
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO radar_catalog_products (canonical_key, retailer, retailer_product_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (canonical_key) DO UPDATE SET last_seen_at = NOW(), payload = EXCLUDED.payload",
                (key, payload["retailer"], payload["retailer_product_id"], json.dumps(payload)),
            )
        connection.commit()
    return True


def persist_inventory_observation(item, previous):
    """Store only a change or price/seller update, never every polling result."""
    if not database_enabled():
        return
    meaningful = (
        previous.get("status") != item.get("status")
        or previous.get("price") != item.get("price")
        or previous.get("seller") != item.get("seller")
        or previous.get("official_seller_verified") != item.get("official_seller_verified")
    )
    if not meaningful:
        return
    observation_id = hashlib.sha256(
        f"{item.get('id')}:{item.get('checked_at')}:{item.get('status')}:{item.get('price')}:{item.get('seller')}".encode("utf-8")
    ).hexdigest()[:24]
    payload = {
        "status": item.get("status"),
        "previous_status": previous.get("status"),
        "price": item.get("price"),
        "seller": item.get("seller"),
        "first_party_seller": item.get("official_seller_verified"),
        "checked_at": item.get("checked_at"),
        "evidence": item.get("evidence"),
    }
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO radar_inventory_observations (id, product_id, payload) VALUES (%s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING",
                (observation_id, item.get("id"), json.dumps(payload)),
            )
        connection.commit()


def _write_discovery_run(run):
    if not database_enabled():
        return
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO radar_discovery_runs (id, retailer, payload) VALUES (%s, %s, %s::jsonb)",
                (run["id"], run["retailer"], json.dumps(run)),
            )
        connection.commit()


async def _verify_and_begin_monitoring(candidate, adapter):
    """Use one ordinary public product-page check before monitoring a discovery."""
    product_url = str(candidate.get("product_url", "")).strip()
    if not verified_product_url(product_url):
        return None
    response = await http_client.get(product_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    observation = adapter.check_inventory(response.text, product_url)
    # A public listing alone is not enough. The seller must be explicitly
    # identified as the retailer before it can enter the monitored source list.
    if observation.first_party_seller is not True:
        return None
    source = _clean_source({
        "product": candidate.get("title"),
        "url": product_url,
        "game": candidate.get("tcg", "Pokemon"),
        "store": candidate.get("retailer", "Best Buy"),
        "set_name": candidate.get("set_name", ""),
        "catalog_key": candidate.get("canonical_key", ""),
        "product_type": candidate.get("product_type", "other_pack_product"),
        "priority": "normal",
        "area": "Online",
        "published": True,
    })
    candidate.update({
        "discovery_status": "monitoring",
        "public_status": observation.status,
        "public_price": observation.price,
        "public_seller": observation.seller,
        "first_party_seller": True,
        "verified_at": now_iso(),
    })
    return source


async def discover_best_buy_products(allow_disabled=False):
    """Discover public listings, then verify first-party evidence before monitoring."""
    run = {
        "id": secrets.token_urlsafe(12),
        "retailer": "Best Buy",
        "started_at": now_iso(),
        "discovered": 0,
        "stored": 0,
        "verified": 0,
        "monitoring_started": 0,
        "status": "skipped",
        "errors": [],
    }
    if not BEST_BUY_DISCOVERY_ENABLED and not allow_disabled:
        run["reason"] = "TCG_RADAR_BEST_BUY_DISCOVERY_ENABLED is not enabled"
        return run
    if not database_enabled():
        run["reason"] = "PostgreSQL is required for restart-safe discovery"
        return run
    adapter = adapter_for("Best Buy")
    if not adapter or not adapter.supports_discovery:
        run["reason"] = "Best Buy discovery adapter is unavailable"
        return run
    run["status"] = "success"
    candidates_by_key = {}
    for source_url in BEST_BUY_DISCOVERY_URLS:
        try:
            response = await http_client.get(source_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, timeout=20)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            candidates = adapter.discover_products(response.text, source_url)
            run["discovered"] += len(candidates)
            for candidate in candidates:
                payload = candidate.to_dict() | {"discovery_status": "pending_verification"}
                candidates_by_key[payload["canonical_key"]] = payload
                if upsert_catalog_product(payload):
                    run["stored"] += 1
            mark_success("Best Buy", response.status_code)
        except Exception as exc:
            run["status"] = "partial" if run["stored"] else "error"
            run["errors"].append(f"{source_url}: {type(exc).__name__}: {exc}")
            mark_failure("Best Buy", str(exc))
        await asyncio.sleep(2)

    sources = _all_sources()
    known = {canonical_product_key(source) for source in sources}
    for candidate in list(candidates_by_key.values())[:DISCOVERY_MAX_PUBLIC_VERIFICATIONS]:
        try:
            source = await _verify_and_begin_monitoring(candidate, adapter)
            if source:
                run["verified"] += 1
                if canonical_product_key(source) not in known:
                    sources.append(source)
                    known.add(canonical_product_key(source))
                    run["monitoring_started"] += 1
                upsert_catalog_product(candidate)
            await asyncio.sleep(1.5)
        except Exception as exc:
            candidate["discovery_status"] = "pending_verification"
            candidate["verification_error"] = f"{type(exc).__name__}: {exc}"
            upsert_catalog_product(candidate)
            run["errors"].append(f"{candidate.get('title', 'candidate')}: {type(exc).__name__}: {exc}")
    if run["monitoring_started"]:
        _write_sources(sources)
    run["completed_at"] = now_iso()
    _write_discovery_run(run)
    return run

async def maybe_run_discovery():
    if not BEST_BUY_DISCOVERY_ENABLED:
        return
    now = time.monotonic()
    last = last_discovery_attempt.get("Best Buy", 0)
    if now - last < DISCOVERY_INTERVAL_SECONDS:
        return
    last_discovery_attempt["Best Buy"] = now
    await discover_best_buy_products()


def _file_sources():
    if not SOURCES_FILE.exists():
        return []
    try:
        raw = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def ensure_data_files():
    """Seed a newly attached empty volume from the checked-in starter list."""
    if not SOURCES_FILE.exists() and DEFAULT_SOURCES_FILE.exists():
        SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SOURCES_FILE.write_text(DEFAULT_SOURCES_FILE.read_text(encoding="utf-8"), encoding="utf-8")


def _all_sources():
    if not database_enabled():
        return _file_sources()
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM radar_products ORDER BY updated_at, id")
            return [row[0] for row in cursor.fetchall()]


def _write_sources(sources):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM radar_products")
                for source in sources:
                    cursor.execute("INSERT INTO radar_products (id, payload) VALUES (%s, %s::jsonb)", (source_id(source), json.dumps(source)))
            connection.commit()
        return
    temporary = SOURCES_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SOURCES_FILE)


def _normalize_priority_automation(payload):
    source = payload if isinstance(payload, dict) else {}
    selected = [
        str(item).strip()
        for item in source.get("auto_selected_product_ids", [])
        if str(item).strip()
    ][:20]
    return {
        "auto_high_priority": bool(source.get("auto_high_priority", False)),
        "top_limit": 20,
        "trend_source": str(source.get("trend_source") or "not_configured")[:80],
        "auto_selected_product_ids": selected,
        "updated_at": source.get("updated_at"),
    }


def load_priority_automation():
    global priority_automation
    payload = None
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM radar_settings WHERE key = %s", ("priority_automation",))
                row = cursor.fetchone()
                payload = row[0] if row else None
    elif PRIORITY_AUTOMATION_FILE.exists():
        try:
            payload = json.loads(PRIORITY_AUTOMATION_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
    priority_automation = _normalize_priority_automation(payload or DEFAULT_PRIORITY_AUTOMATION)
    return dict(priority_automation)


def write_priority_automation(payload):
    global priority_automation
    priority_automation = _normalize_priority_automation(payload)
    priority_automation["updated_at"] = now_iso()
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO radar_settings (key, payload) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
                    ("priority_automation", json.dumps(priority_automation)),
                )
            connection.commit()
    else:
        PRIORITY_AUTOMATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PRIORITY_AUTOMATION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(priority_automation, indent=2) + "\n", encoding="utf-8")
        temporary.replace(PRIORITY_AUTOMATION_FILE)
    return dict(priority_automation)


CATALOG_PRODUCT_TYPES = {
    "elite_trainer_box",
    "booster_bundle",
    "booster_box",
    "booster_pack",
    "sleeved_booster",
    "two_pack",
    "three_pack_blister",
    "checklane_blister",
    "collection",
    "poster_collection",
    "ex_box",
    "knockout_collection",
    "super_premium_collection",
    "tech_sticker_collection",
    "figure_collection",
    "premium_collection",
    "ultra_premium_collection",
    "tin",
    "build_and_battle",
    "deck",
    "other_pack_product",
}


def _clean_estimate_timestamp(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _stock_estimate_fields(source):
    estimate = str(source.get("stock_estimate") or "").strip()[:60]
    if not estimate:
        return {
            "stock_estimate": None,
            "stock_estimate_reported_at": None,
            "stock_estimate_expires_at": None,
        }
    expires_at = _clean_estimate_timestamp(source.get("stock_estimate_expires_at"))
    if expires_at:
        expires = datetime.fromisoformat(expires_at)
        if expires <= datetime.now(timezone.utc):
            return {
                "stock_estimate": None,
                "stock_estimate_reported_at": None,
                "stock_estimate_expires_at": None,
            }
    return {
        "stock_estimate": estimate,
        "stock_estimate_reported_at": _clean_estimate_timestamp(source.get("stock_estimate_reported_at")),
        "stock_estimate_expires_at": expires_at,
    }


def _clean_source(payload):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Product data must be an object")
    url = canonical_product_url(payload.get("url", ""))
    if not verified_product_url(url):
        raise HTTPException(
            status_code=422,
            detail="Use a stable HTTPS product page from an approved official retailer",
        )
    product = str(payload.get("product", "")).strip()
    if not product:
        raise HTTPException(status_code=422, detail="Product name is required")
    msrp = safe_float(payload.get("msrp"))
    if msrp is not None and msrp <= 0:
        raise HTTPException(status_code=422, detail="MSRP must be greater than zero")
    priority = str(payload.get("priority", "normal")).lower()
    if priority not in {"high", "normal", "low"}:
        raise HTTPException(status_code=422, detail="Priority must be high, normal, or low")
    max_markup = safe_float(payload.get("max_markup"), 80)
    if max_markup is None or max_markup < 0 or max_markup > 1000:
        raise HTTPException(status_code=422, detail="Maximum markup must be between 0 and 1000 percent")
    set_name = str(payload.get("set_name", "")).strip()[:100]
    product_type = str(payload.get("product_type", "other_pack_product")).strip().lower()
    if product_type not in CATALOG_PRODUCT_TYPES:
        raise HTTPException(status_code=422, detail="Choose a supported pack-containing product type")
    packs = payload.get("packs")
    if packs in ("", None):
        packs = None
    else:
        try:
            packs = int(packs)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Pack count must be a whole number")
        if not 1 <= packs <= 1000:
            raise HTTPException(status_code=422, detail="Pack count must be between 1 and 1000")
    return {
        "enabled": bool(payload.get("enabled", True)),
        # New manager entries are staged until an owner explicitly publishes them.
        "published": bool(payload.get("published", False)),
        "added_at": str(payload.get("added_at") or now_iso()),
        "game": str(payload.get("game", "Other")).strip() or "Other",
        "area": str(payload.get("area", "Online")).strip() or "Online",
        "store": retailer_name(url, str(payload.get("store", "")).strip()),
        "product": product,
        "url": url,
        "msrp": msrp,
        "max_markup": max_markup,
        "priority": priority,
        "set_name": set_name,
        "catalog_key": str(payload.get("catalog_key", "")).strip().lower()[:160],
        "product_type": product_type,
        "packs": packs,
        "image_url": clean_image_url(payload.get("image_url", "")),
        **_stock_estimate_fields(payload),
        "official_seller_only": True,
    }


def monitored_product_key(source):
    """Prevent duplicate product names at the same retailer, even with a new URL."""
    retailer = retailer_name(str(source.get("url", "")), str(source.get("store", ""))).casefold()
    title = re.sub(r"[^a-z0-9]+", " ", str(source.get("product", "")).casefold()).strip()
    return f"{retailer}|{title}"


def clean_image_url(value):
    image_url = str(value or "").strip()
    if not image_url:
        return ""
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Image override must be a secure HTTPS URL")
    return image_url


PIN_PATTERN = re.compile(r"^\d{4,20}$")
NICKNAME_PATTERN = re.compile(r"^[A-Za-z0-9 _-]{2,24}$")


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _owner_pin_hash():
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM radar_settings WHERE key = %s", ("owner_pin",))
                row = cursor.fetchone()
        if not row:
            return ""
        payload = row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        return str((payload or {}).get("hash", ""))
    if not OWNER_PIN_FILE.exists():
        return ""
    try:
        payload = json.loads(OWNER_PIN_FILE.read_text(encoding="utf-8"))
        return str((payload or {}).get("hash", ""))
    except (OSError, json.JSONDecodeError):
        return ""


def _write_owner_pin_hash(pin_hash):
    payload = {"hash": pin_hash, "updated_at": now_iso()}
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO radar_settings (key, payload) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
                    ("owner_pin", json.dumps(payload)),
                )
            connection.commit()
        return
    temporary = OWNER_PIN_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OWNER_PIN_FILE)


def _nickname(value):
    nickname = str(value or "").strip()
    if not NICKNAME_PATTERN.fullmatch(nickname):
        raise HTTPException(status_code=422, detail="Nickname must be 2–24 characters: letters, numbers, spaces, _ or -")
    return nickname


def _nickname_taken(nickname, exclude_google_sub="", exclude_moderator_id=""):
    folded = nickname.casefold()
    for moderator in _moderators():
        if moderator.get("id") != exclude_moderator_id and str(moderator.get("name", "")).casefold() == folded:
            return True
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM radar_users WHERE LOWER(nickname) = LOWER(%s) AND google_sub <> %s", (nickname, exclude_google_sub))
                return cursor.fetchone() is not None
    return False


def _owner_nickname():
    if database_enabled() and OWNER_EMAIL:
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT nickname FROM radar_users WHERE LOWER(email) = LOWER(%s)", (OWNER_EMAIL,))
                row = cursor.fetchone()
        if row and row[0]:
            return str(row[0])
    return "Owner"


def _is_google_owner(authorization):
    try:
        return bool(google_user(authorization).get("is_owner"))
    except HTTPException:
        return False


def _is_owner_pin(token):
    stored_hash = _owner_pin_hash()
    return bool(token and stored_hash and secrets.compare_digest(_token_hash(token), stored_hash))


def require_admin(authorization=Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if ADMIN_SECRET and token and secrets.compare_digest(token, ADMIN_SECRET):
        return "owner"
    if _is_owner_pin(token):
        return "owner"
    if _is_google_owner(authorization):
        return "owner"
    raise HTTPException(status_code=401, detail="Incorrect PIN")


def _moderators():
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, name, secret_hash, created_at FROM radar_moderators ORDER BY created_at, id")
                return [{"id": row[0], "name": row[1], "secret_hash": row[2], "created_at": row[3].isoformat()} for row in cursor.fetchall()]
    if not MODERATORS_FILE.exists():
        return []
    try:
        raw = json.loads(MODERATORS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_moderators(moderators):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM radar_moderators")
                for moderator in moderators:
                    cursor.execute("INSERT INTO radar_moderators (id, name, secret_hash) VALUES (%s, %s, %s)", (moderator["id"], moderator["name"], moderator["secret_hash"]))
            connection.commit()
        return
    temporary = MODERATORS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(moderators, indent=2) + "\n", encoding="utf-8")
    temporary.replace(MODERATORS_FILE)


def manager_role(authorization=Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if ADMIN_SECRET and token and secrets.compare_digest(token, ADMIN_SECRET):
        return "owner"
    if _is_owner_pin(token):
        return "owner"
    if _is_google_owner(authorization):
        return "owner"
    hashed = _token_hash(token) if token else ""
    for moderator in _moderators():
        if hashed and secrets.compare_digest(hashed, moderator.get("secret_hash", "")):
            return "moderator"
    raise HTTPException(status_code=401, detail="Incorrect PIN")


def require_product_manager(authorization=Header(default="")):
    return manager_role(authorization)


def optional_manager_role(authorization=""):
    """Return a manager role when supplied, but keep the public feed public."""
    try:
        return manager_role(authorization)
    except HTTPException:
        return None


def owner_feed_access(authorization=""):
    """Allow the Google account configured as owner to see owner-only feed data."""
    role = optional_manager_role(authorization)
    if role:
        return role
    try:
        user = google_user(authorization)
        return "google_owner" if user.get("is_owner") else None
    except HTTPException:
        return None



def _reports():
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, product_id, reason, details, created_at, resolved FROM radar_reports ORDER BY created_at DESC")
                return [{"id": row[0], "product_id": row[1], "reason": row[2], "details": row[3], "created_at": row[4].isoformat(), "resolved": row[5]} for row in cursor.fetchall()]
    return []


def _write_report(report):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO radar_reports (id, product_id, reason, details) VALUES (%s, %s, %s, %s)", (report["id"], report["product_id"], report["reason"], report["details"]))
            connection.commit()


def _subscriptions():
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM push_subscriptions ORDER BY updated_at, endpoint")
                return [row[0] for row in cursor.fetchall()]
    if not PUSH_SUBSCRIPTIONS_FILE.exists():
        return []
    try:
        raw = json.loads(PUSH_SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_subscriptions(subscriptions):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM push_subscriptions")
                for subscription in subscriptions:
                    cursor.execute("INSERT INTO push_subscriptions (endpoint, payload) VALUES (%s, %s::jsonb)", (subscription["endpoint"], json.dumps(subscription)))
            connection.commit()
        return
    temporary = PUSH_SUBSCRIPTIONS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(subscriptions, indent=2) + "\n", encoding="utf-8")
    temporary.replace(PUSH_SUBSCRIPTIONS_FILE)


def push_ready():
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _valid_subscription(subscription):
    if not isinstance(subscription, dict) or not str(subscription.get("endpoint", "")).startswith("https://"):
        return False
    keys = subscription.get("keys")
    return isinstance(keys, dict) and bool(keys.get("p256dh") and keys.get("auth"))


def _clean_push_preferences(payload):
    payload = payload if isinstance(payload, dict) else {}
    max_markup = safe_float(payload.get("max_markup"), 80)
    if max_markup is None or max_markup < 0 or max_markup > 999:
        raise HTTPException(status_code=422, detail="Alert markup must be between 0 and 999 percent")
    allowed_games = {"Pokemon", "One Piece", "Magic", "Other"}
    allowed_stores = {"Walmart", "Target", "Amazon", "Best Buy", "GameStop", "Costco", "Sam's Club", "CVS", "Walgreens"}
    games = [str(item) for item in payload.get("games", []) if str(item) in allowed_games]
    stores = [str(item) for item in payload.get("stores", []) if str(item) in allowed_stores]
    allow_third_party = bool(payload.get("allow_third_party", False))
    quiet_hours = payload.get("quiet_hours", {})
    quiet_hours = quiet_hours if isinstance(quiet_hours, dict) else {}
    quiet_enabled = bool(quiet_hours.get("enabled", False))
    quiet_start = int(safe_float(quiet_hours.get("start_minute"), 1320) or 1320) % 1440
    quiet_end = int(safe_float(quiet_hours.get("end_minute"), 480) or 480) % 1440
    quiet_offset = int(safe_float(quiet_hours.get("timezone_offset"), 0) or 0)
    if quiet_offset < -840 or quiet_offset > 840:
        quiet_offset = 0
    muted_product_ids = [str(item)[:200] for item in payload.get("muted_product_ids", []) if str(item).strip()][:500]
    raw_store_mutes = payload.get("muted_stores_until", {})
    muted_stores_until = {}
    if isinstance(raw_store_mutes, dict):
        for store, until in raw_store_mutes.items():
            timestamp = safe_float(until)
            if str(store) in allowed_stores and timestamp is not None and timestamp > time.time():
                muted_stores_until[str(store)] = int(timestamp)
    return {"max_markup": max_markup, "games": games, "stores": stores, "allow_third_party": allow_third_party, "quiet_hours": {"enabled": quiet_enabled, "start_minute": quiet_start, "end_minute": quiet_end, "timezone_offset": quiet_offset}, "muted_product_ids": muted_product_ids, "muted_stores_until": muted_stores_until}


def _alert_history():
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM radar_alert_events ORDER BY created_at DESC LIMIT 100")
                return [row[0] for row in cursor.fetchall()]
    if not ALERT_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(ALERT_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _authorized_intake_sources():
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM radar_settings WHERE key = %s", ("authorized_intake_sources",))
                row = cursor.fetchone()
                return row[0] if row and isinstance(row[0], list) else []
    try:
        data = json.loads(AUTHORIZED_INTAKE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_authorized_intake_sources(items):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO radar_settings (key, payload) VALUES (%s, %s::jsonb) ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
                    ("authorized_intake_sources", json.dumps(items)),
                )
            connection.commit()
        return
    AUTHORIZED_INTAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTHORIZED_INTAKE_FILE.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


def _public_intake_source(item):
    return {key: item.get(key) for key in ("id", "label", "created_at")}


def _write_alert_event(event):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO radar_alert_events (id, product_id, payload) VALUES (%s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING", (event["id"], event["product_id"], json.dumps(event)))
            connection.commit()
        return
    history = [event] + [item for item in _alert_history() if item.get("id") != event.get("id")]
    ALERT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_HISTORY_FILE.write_text(json.dumps(history[:100], indent=2) + "\n", encoding="utf-8")


def _support_messages(thread_id=None, limit=100):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                if thread_id:
                    cursor.execute("SELECT id, thread_id, client_id, sender, body, created_at FROM radar_support_messages WHERE thread_id = %s ORDER BY created_at ASC LIMIT %s", (thread_id, limit))
                else:
                    cursor.execute("SELECT id, thread_id, client_id, sender, body, created_at FROM radar_support_messages ORDER BY created_at DESC LIMIT %s", (limit,))
                return [{"id": r[0], "thread_id": r[1], "client_id": r[2], "sender": r[3], "body": r[4], "created_at": r[5].isoformat()} for r in cursor.fetchall()]
    return []


def _write_support_message(message):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO radar_support_messages (id, thread_id, client_id, sender, body) VALUES (%s, %s, %s, %s, %s)", (message["id"], message["thread_id"], message["client_id"], message["sender"], message["body"]))
            connection.commit()


def google_user(authorization):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured yet")
    token = str(authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception:
        raise HTTPException(status_code=401, detail="That Google sign-in could not be verified")
    if not claims.get("sub") or not claims.get("email"):
        raise HTTPException(status_code=401, detail="Google did not provide a usable account")
    user = {"google_sub": str(claims["sub"]), "email": str(claims["email"]), "name": str(claims.get("name", "")), "is_owner": bool(OWNER_EMAIL and str(claims["email"]).strip().lower() == OWNER_EMAIL)}
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO radar_users (google_sub, email, name) VALUES (%s, %s, %s) ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name, updated_at = NOW()", (user["google_sub"], user["email"], user["name"]))
                cursor.execute("SELECT nickname FROM radar_users WHERE google_sub = %s", (user["google_sub"],))
                row = cursor.fetchone()
            connection.commit()
        user["nickname"] = str(row[0] or "") if row else ""
    else:
        user["nickname"] = ""
    return user


def _user_purchases(google_sub):
    if not database_enabled():
        return []
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM radar_purchases WHERE google_sub = %s ORDER BY created_at DESC", (google_sub,))
            return [row[0] for row in cursor.fetchall()]


def _is_support_banned(client_id):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM radar_support_bans WHERE client_id = %s", (client_id,))
                return cursor.fetchone() is not None
    return False


def _ban_support_client(client_id, reason="Spam"):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO radar_support_bans (client_id, reason) VALUES (%s, %s) ON CONFLICT (client_id) DO UPDATE SET reason = EXCLUDED.reason", (client_id, reason))
            connection.commit()


def _send_web_push(subscription, payload):
    from pywebpush import WebPushException, webpush
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CONTACT},
        )
        return False
    except WebPushException as error:
        return getattr(error.response, "status_code", None) in {404, 410}
    except Exception as error:
        print("Push delivery failed:", type(error).__name__)
        return False


async def notify_transition(item):
    # Availability always needs two confirmations. Retailer-direct stock is
    # eligible by default; a marketplace offer is eligible only for subscribers
    # who explicitly opt in below.
    if not item.get("restock_confirmed"):
        return
    previous, current = item.get("previous_status"), item.get("status")
    direct_restock = current == "in_stock" and item.get("official_seller_verified")
    marketplace_restock = current == "marketplace_in_stock"
    if not (direct_restock or marketplace_restock):
        return
    # One deterministic event per product/restock session and offer type.
    event_id_source = f"{item.get('id')}:{int(item.get('restock_session') or 0)}:{'direct' if direct_restock else 'marketplace'}"
    event = {
        "id": hashlib.sha256(event_id_source.encode("utf-8")).hexdigest()[:24],
        "product_id": item.get("id"),
        "product": item.get("product"),
        "store": item.get("store"),
        "url": item.get("url"),
        "previous_status": previous,
        "status": current,
        "offer_type": "retailer_direct" if direct_restock else "third_party",
        "price": item.get("price"),
        "msrp": item.get("msrp"),
        "created_at": now_iso(),
    }
    _write_alert_event(event)
    if not push_ready():
        return
    markup = safe_float(item.get("markup"))
    payload = {
        "title": "TCG Radar alert",
        "body": f"{item.get('product')} has an invitation request at {item.get('store')}" if current == "invitation" else f"{item.get('product')} is now {current.replace('_', ' ')} at {item.get('store')}",
        "url": item.get("url"),
        "tag": item.get("id"),
    }
    expired = []
    for subscription in _subscriptions():
        preference = _clean_push_preferences(subscription.get("preferences"))
        if not _matches_push_preferences(subscription, item):
            continue
        if marketplace_restock and not preference["allow_third_party"]:
            continue
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


def official_seller_verified(source, store, parser_result, text):
    """Require retailer-direct evidence before a source can trigger a restock."""
    if not source.get("official_seller_only", True):
        return True

    store_key = store.strip().lower()
    seller = str((parser_result or {}).get("seller") or "").strip().lower()
    if (parser_result or {}).get("first_party_seller") is True:
        return True
    if store_key in DIRECT_SELLER_NAMES:
        return seller in DIRECT_SELLER_NAMES[store_key]

    direct_phrases = {
        "best buy": ("sold by best buy", "ships from best buy"),
        "costco": ("sold by costco", "ships from costco", "costco wholesale"),
        "sam's club": ("sold and shipped by sam's club", "sold by sam's club"),
        "cvs": ("sold by cvs", "shipped by cvs"),
        "walgreens": ("sold by walgreens", "shipped by walgreens"),
        "amazon": ("ships from amazon.com", "sold by amazon.com"),
        "gamestop": ("sold by gamestop", "shipped by gamestop"),
    }
    page = text.lower()
    return any(phrase in page for phrase in direct_phrases.get(store_key, ()))


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
def extract_image_url(text):
    """Return only a public image URL explicitly supplied by the page."""
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        r'["\']image["\']\s*:\s*["\'](https?://[^"\']+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            candidate = html.unescape(match.group(1)).strip()
            if candidate.startswith("https://"):
                return candidate
    return None


def extract_page_title(text):
    patterns = [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<title[^>]*>(.*?)</title>',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            title = re.sub(r"<[^>]+>", "", html.unescape(match.group(1))).strip()
            if title:
                return title[:180]
    return None


async def fetch_official_product_image(url):
    """Retrieve one public product-page image during an owner/moderator add."""
    try:
        response = await http_client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=12,
        )
        if response.status_code == 200:
            return extract_image_url(response.text)
    except (httpx.HTTPError, RuntimeError):
        pass
    return None


def _image_search_tokens(product_name):
    ignored = {"pokemon", "pokémon", "trading", "card", "game", "tcg", "collection", "series", "the", "and"}
    return [word for word in re.findall(r"[a-z0-9]+", str(product_name or "").lower()) if len(word) > 2 and word not in ignored][:8]


def _is_pokemon_product(source):
    return "pokemon" in str(source.get("game", "")).lower() or "pokemon" in str(source.get("product", "")).lower()


async def fetch_pokemon_center_image(product_name):
    """Use Pokémon Center only as a product-art fallback, never for availability."""
    tokens = _image_search_tokens(product_name)
    if len(tokens) < 2:
        return None
    try:
        search_url = "https://www.pokemoncenter.com/search?q=" + quote_plus(str(product_name or "")[:140])
        search = await http_client.get(
            search_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=12,
        )
        if search.status_code != 200:
            return None
        paths = re.findall(r'href=["\\'](/product/[^"\\'#?]+)', search.text, re.I)
        seen = set()
        for path in paths[:8]:
            if path in seen:
                continue
            seen.add(path)
            page = await http_client.get(
                "https://www.pokemoncenter.com" + path,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=12,
            )
            if page.status_code != 200:
                continue
            candidate_title = extract_title(page.text) or ""
            candidate_words = set(_image_search_tokens(candidate_title))
            if len(set(tokens).intersection(candidate_words)) < min(2, len(tokens)):
                continue
            image_url = extract_image_url(page.text)
            if image_url:
                return image_url
    except (httpx.HTTPError, RuntimeError):
        pass
    return None


async def fetch_best_product_image(source):
    image_url = await fetch_official_product_image(source.get("url", ""))
    if image_url:
        return image_url
    if _is_pokemon_product(source):
        return await fetch_pokemon_center_image(source.get("product", ""))
    return None


async def fill_missing_product_images(limit=3):
    """Persist a few missing product images at a time without touching stock checks."""
    sources = _all_sources()
    refreshed = []
    checked = 0
    for source in sources:
        if checked >= max(1, int(limit)):
            break
        if str(source.get("image_url", "")).strip():
            continue
        checked += 1
        image_url = await fetch_best_product_image(source)
        if image_url:
            source["image_url"] = image_url
            refreshed.append({"id": source_id(source), "product": source.get("product", "")})
        await asyncio.sleep(0.3)
    if refreshed:
        _write_sources(sources)
    return {
        "checked": checked,
        "refreshed": len(refreshed),
        "remaining": sum(1 for source in sources if not str(source.get("image_url", "")).strip()),
    }


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


def invitation_signal(text):
    """Use only explicit invitation language; generic marketing copy is ignored."""
    page = text.lower()
    patterns = (
        r"request[\s_-]*(?:an?\s+)?invitation",
        r"invitation[\s_-]*(?:request|required|only)",
        r"invite[\s_-]*only",
        r"request[\s_-]*(?:an?\s+)?invite",
        r"join[\s_-]*(?:the\s+)?waitlist",
        r"purchase[\s_-]*only[\s_-]*(?:if|with)[\s_-]*(?:you[\s_-]*)?(?:are[\s_-]*)?invited",
    )
    return any(re.search(pattern, page) for pattern in patterns)

def detect_status(status_code, text, store=None):
    page = text.lower()
    if any(token in page for token in ("captcha", "verify you are human", "unusual traffic", "access denied")):
        return "blocked"
    if invitation_signal(page):
        return "invitation"

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
        
    # A visible "Add to cart" button alone is not proof of live stock: it can
    # be part of an invite, preorder, or disabled purchase flow. Only structured
    # availability data can promote a generic listing to an actual restock.
    schema_in_stock_signals = [
        '"availability":"http://schema.org/instock"',
        '"availability":"https://schema.org/instock"',
        '"availability": "http://schema.org/instock"',
        '"availability": "https://schema.org/instock"',
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

    has_schema_in_stock = any(signal in page for signal in schema_in_stock_signals)
    has_sold_out = any(signal in page for signal in sold_out_signals)
    has_coming_soon = any(signal in page for signal in coming_soon_signals)
    has_cart_button = "add to cart" in page or "add-to-cart" in page

    # Preorder and release-date language always wins over a cart control.
    if has_coming_soon:
        return "loaded"
    if has_sold_out and not has_schema_in_stock:
        return "sold_out"
    if has_schema_in_stock and not has_sold_out:
        return "in_stock"
    if has_cart_button:
        return "loaded"

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

    previous = products.get(product_key) or load_monitor_state(product_key)

    started = time.monotonic()

    try:
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            response = await http_client.get(url, headers=request_headers, timeout=20)
        except (httpx.TimeoutException, httpx.TransportError):
            # One delayed Best Buy retry handles transient Railway-to-retailer
            # connection failures. Other retailers retain their existing
            # one-request pacing, and normal health backoff still applies.
            if store != "Best Buy":
                raise
            await asyncio.sleep(2)
            response = await http_client.get(url, headers=request_headers, timeout=25)

        elapsed_ms = round(
            (time.monotonic() - started) * 1000
        )

        parser_result = None

        if adapter_for(store) and adapter_for(store).supports_inventory:
            # A supported adapter may promote public structured data to an
            # inventory state. It must return unknown when seller evidence is
            # missing; generic page scraping must not override it.
            parser_result = adapter_for(store).check_inventory(response.text, url).to_dict()
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

        if invitation_signal(response.text):
            # Invitation-only is not purchasable stock. Keep the listing quiet
            # and unconfirmed so it cannot generate a false restock push.
            status = "unknown"
            price = None
        retailer_direct = official_seller_verified(source, store, parser_result, response.text)
        if status == "in_stock" and not retailer_direct:
            status = "unknown"
        quantity = detect_quantity(
            response.text
        )
        image_url = str(source.get("image_url", "")).strip() or extract_image_url(response.text)

        if status == "blocked":
            # A challenge page can return HTTP 200; represent it honestly as a
            # retailer block so managers see it instead of a silent miss.
            block_reason = "Retailer challenge/CAPTCHA detected" if response.status_code == 200 else f"HTTP {response.status_code}"
            mark_failure(
                store,
                block_reason,
                403 if response.status_code == 200 else response.status_code,
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

        # A new sellout/loaded session re-arms exactly one future alert.
        # Persisting the session prevents a Railway restart from replaying it.
        previous_purchasable = previous and previous.get("status") in {"in_stock", "marketplace_in_stock"}
        new_restock_session = bool(previous_purchasable and status in RESTOCK_ARMING_STATUSES)
        restock_session = int(previous.get("restock_session") or 0) + (1 if new_restock_session else 0)
        live_alerted = False if new_restock_session else bool(previous.get("live_alerted"))
        marketplace_alerted = False if new_restock_session else bool(previous.get("marketplace_alerted"))
        restock_armed = bool(previous.get("restock_armed")) or bool(previous and status in RESTOCK_ARMING_STATUSES)
        in_stock_streak = int(previous.get("in_stock_streak") or 0) + 1 if status == "in_stock" and retailer_direct else 0
        marketplace_streak = int(previous.get("marketplace_streak") or 0) + 1 if status == "marketplace_in_stock" else 0
        direct_restock_confirmed = bool(restock_armed and status == "in_stock" and retailer_direct and in_stock_streak >= 2 and not live_alerted)
        marketplace_restock_confirmed = bool(restock_armed and status == "marketplace_in_stock" and marketplace_streak >= 2 and not marketplace_alerted)
        restock_confirmed = direct_restock_confirmed or marketplace_restock_confirmed

        checked_at = now_iso()
        notification_at = previous.get("notification_at")
        if restock_confirmed:
            notification_at = checked_at
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
            "set_name": source.get("set_name", ""),
            "catalog_key": source.get("catalog_key", ""),
            "product_type": source.get("product_type", "other_pack_product"),
            "packs": source.get("packs"),
            **_stock_estimate_fields(source),
            "store": store,
            "product": source.get(
                "product",
                "Unnamed product",
            ),
            "url": url,
            "status": status,
            "previous_status": old_status,
            "status_changed": changed,
            "restock_armed": restock_armed,
            "restock_session": restock_session,
            "in_stock_streak": in_stock_streak,
            "marketplace_streak": marketplace_streak,
            "live_alerted": live_alerted or direct_restock_confirmed,
            "marketplace_alerted": marketplace_alerted or marketplace_restock_confirmed,
            "restock_confirmed": restock_confirmed,
            "official_seller_verified": retailer_direct,
            "price": price,
            "msrp": msrp,
            "markup": markup,
            "quantity": quantity,
            "image_url": image_url,
            "priority": source.get(
                "priority",
                "normal",
            ),
            "max_markup": safe_float(source.get("max_markup"), 80),
            "base_interval_seconds": interval_for(
                source
            ),
            "checked_at": checked_at,
            "first_seen_at": previous.get("first_seen_at") or checked_at,
            "notification_at": notification_at,
            "response_ms": elapsed_ms,
            "http_status": response.status_code,
            "seller": parser_result.get("seller") if parser_result else store,
            "parser_version": PARSER_VERSION if parser_result else "generic-1",
            "evidence": (
                f"{parser_result.get('evidence') if parser_result else 'Public product page checked'}; "
                f"checked {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            ),
        }
        # Keep only monitor state needed to survive a Railway restart. Product
        # cards remain in memory; PostgreSQL is the durable dedupe authority.
        persist_monitor_state(product_key, products[product_key])
        persist_inventory_observation(products[product_key], previous)
        asyncio.create_task(notify_transition(products[product_key]))

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
            "set_name": source.get("set_name", ""),
            "catalog_key": source.get("catalog_key", ""),
            "product_type": source.get("product_type", "other_pack_product"),
            "packs": source.get("packs"),
            **_stock_estimate_fields(source),
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
            "image_url": previous.get("image_url"),
            "priority": source.get(
                "priority",
                "normal",
            ),
            "max_markup": safe_float(source.get("max_markup"), 80),
            "base_interval_seconds": interval_for(
                source
            ),
            "checked_at": now_iso(),
            "first_seen_at": previous.get("first_seen_at") or now_iso(),
            "notification_at": previous.get("notification_at"),
            "response_ms": None,
            "http_status": None,
            "evidence": f"Check failed: {type(exc).__name__}: {exc}",
        }


async def _run_scheduled_check(source, next_checks):
    """Run one retailer check and schedule its next eligible pass."""
    key = source_id(source)
    store = retailer_name(source["url"], source.get("store", "Unknown"))
    health = get_retailer_health(store)

    await check_product(source)

    base = interval_for(source)
    multiplier = health["backoff_multiplier"]
    next_interval = base * multiplier
    jitter = random.uniform(0.95, 1.05)
    next_checks[key] = time.monotonic() + next_interval * jitter
    health["next_eligible_check"] = datetime.fromtimestamp(
        time.time() + next_interval * jitter,
        timezone.utc,
    ).isoformat()


async def scheduler():
    print("TCG Radar scheduler started")

    next_checks = {}
    retailer_ready_at = {}
    last_priority_pulse = None
    last_image_fill = 0.0
    startup_seeded = False
    max_parallel_retailers = 8

    while True:
        await maybe_run_discovery()
        sources = load_sources()

        if not sources:
            await asyncio.sleep(5)
            continue

        current = time.monotonic()
        # Image backfill is deliberately slow and independent of stock monitoring.
        if current - last_image_fill >= 1200:
            await fill_missing_product_images(limit=3)
            last_image_fill = current
            sources = load_sources()
        wall_clock = datetime.now(timezone.utc)
        pulse = f"{wall_clock:%Y-%m-%dT%H}:{wall_clock.minute // 15}"

        if not startup_seeded:
            # After a restart, scan critical links first. We still stagger by
            # retailer so a single store is never flooded during warm-up.
            per_retailer_slot = {}
            priority_rank = {"high": 0, "normal": 1, "low": 2}
            for source in sorted(sources, key=lambda item: priority_rank.get(priority_for(item), 1)):
                store = retailer_name(source["url"], source.get("store", "Unknown"))
                slot = per_retailer_slot.get(store, 0)
                if priority_for(source) == "high":
                    delay = 1 + (slot * 3) + random.uniform(0, 0.8)
                elif priority_for(source) == "normal":
                    delay = 30 + (slot * 2) + random.uniform(0, 8)
                else:
                    delay = 75 + (slot * 2) + random.uniform(0, 12)
                next_checks[source_id(source)] = current + delay
                per_retailer_slot[store] = slot + 1
            startup_seeded = True

        if (
            wall_clock.minute % 15 == 0
            and wall_clock.second < 8
            and pulse != last_priority_pulse
        ):
            high_priority = [
                source for source in sources
                if priority_for(source) == "high"
            ]
            random.shuffle(high_priority)
            for index, source in enumerate(high_priority):
                next_checks[source_id(source)] = current + 2 + (index * 3) + random.uniform(0, 2)
            last_priority_pulse = pulse

        due_by_retailer = {}
        priority_rank = {"high": 0, "normal": 1, "low": 2}
        for source in sources:
            key = source_id(source)
            if key not in next_checks:
                # Newly published high-priority links join the front of the
                # queue; other new links stay gently staggered.
                if priority_for(source) == "high":
                    next_checks[key] = current + random.uniform(1, 4)
                elif priority_for(source) == "normal":
                    next_checks[key] = current + random.uniform(12, 25)
                else:
                    next_checks[key] = current + random.uniform(30, 50)

            if current < next_checks[key]:
                continue

            store = retailer_name(source["url"], source.get("store", "Unknown"))
            if current < retailer_ready_at.get(store, 0):
                continue
            due_by_retailer.setdefault(store, []).append(source)

        selected = []
        for store in sorted(due_by_retailer):
            if len(selected) >= max_parallel_retailers:
                break
            source = min(
                due_by_retailer[store],
                key=lambda item: priority_rank.get(
                    priority_for(item), 1
                ),
            )
            selected.append((store, source))
            # Never overlap two requests to the same retailer. Different
            # retailers may run together, which makes a first scan much faster.
            retailer_ready_at[store] = current + 1

        if selected:
            results = await asyncio.gather(
                *[_run_scheduled_check(source, next_checks) for _, source in selected],
                return_exceptions=True,
            )
            for (store, _), result in zip(selected, results):
                if isinstance(result, Exception):
                    mark_failure(store, str(result))
            await asyncio.sleep(0.25)
        else:
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_task
    global http_client

    ensure_data_files()
    ensure_database()
    load_priority_automation()
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
    version="3.6.1",
    lifespan=lifespan,
)


# Open during initial testing.
# We will restrict this to the GitHub Pages origin later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "name": "TCG Radar",
        "version": "3.6.1",
        "status": "online",
        "message": "TCG Radar backend is running.",
    }


@app.get("/health")
async def health():
    sources = load_sources()

    return {
        "status": "online",
        "version": "3.6.1",
        "time": now_iso(),
        "configured_products": len(
            sources
        ),
        "tracked_products": len(sources),
        "checked_products": len(products),
        "retailers": len(
            retailer_health
        ),
        "persistent_storage": "postgres" if database_enabled() else ("volume" if "RAILWAY_VOLUME_MOUNT_PATH" in os.environ else "ephemeral"),
        "adapter_capabilities": adapter_capabilities(),
    }


def _local_retailer(name):
    normalized = str(name or "").strip().lower()
    for needle, retailer in LOCAL_SUPPORTED_RETAILERS.items():
        if needle in normalized:
            return retailer
    return None


def _distance_miles(latitude_a, longitude_a, latitude_b, longitude_b):
    radius_miles = 3958.7613
    lat_a, lon_a, lat_b, lon_b = map(math.radians, (latitude_a, longitude_a, latitude_b, longitude_b))
    delta_lat, delta_lon = lat_b - lat_a, lon_b - lon_a
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    return radius_miles * 2 * math.asin(min(1, math.sqrt(value)))


def _zip_hash(zip_code):
    return hashlib.sha256(zip_code.encode("utf-8")).hexdigest()


def _manager_role_or_none(authorization):
    try:
        return manager_role(authorization)
    except HTTPException:
        return None


def _valid_local_zip_session(client_id, zip_code, session_id):
    if not client_id or not session_id:
        return False
    zip_hash = _zip_hash(zip_code)
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM radar_local_zip_sessions WHERE id = %s AND client_id = %s AND zip_hash = %s AND expires_at > NOW()",
                    (session_id, client_id, zip_hash),
                )
                return cursor.fetchone() is not None
    session = local_zip_sessions.get(session_id)
    return bool(session and session["client_id"] == client_id and session["zip_hash"] == zip_hash and session["expires_at"] > time.time())


def _start_local_zip_session(client_id, zip_code, is_manager):
    if is_manager:
        return None, None
    if not client_id:
        raise HTTPException(status_code=422, detail="This device needs a local search ID; refresh and try again")
    session_id = secrets.token_urlsafe(18)
    zip_hash = _zip_hash(zip_code)
    expires_at = datetime.fromtimestamp(time.time() + 3 * 60 * 60, timezone.utc)
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM radar_local_zip_searches WHERE client_id = %s AND searched_at > NOW() - INTERVAL '3 hours'", (client_id,))
                used = cursor.fetchone()[0]
                if used >= 2:
                    raise HTTPException(status_code=429, detail="You have used both ZIP searches. Try again after the three-hour window.")
                cursor.execute("INSERT INTO radar_local_zip_searches (id, client_id) VALUES (%s, %s)", (secrets.token_urlsafe(12), client_id))
                cursor.execute("INSERT INTO radar_local_zip_sessions (id, client_id, zip_hash, expires_at) VALUES (%s, %s, %s, %s)", (session_id, client_id, zip_hash, expires_at))
            connection.commit()
        return session_id, 1 - used
    now = time.time()
    recent = [stamp for stamp in local_zip_searches.get(client_id, []) if stamp > now - 3 * 60 * 60]
    if len(recent) >= 2:
        raise HTTPException(status_code=429, detail="You have used both ZIP searches. Try again after the three-hour window.")
    recent.append(now)
    local_zip_searches[client_id] = recent
    local_zip_sessions[session_id] = {"client_id": client_id, "zip_hash": zip_hash, "expires_at": now + 3 * 60 * 60}
    return session_id, 2 - len(recent)


def _enforce_local_scan_cooldown(client_id):
    if not client_id:
        raise HTTPException(status_code=422, detail="This device needs a local search ID; refresh and try again")
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO radar_local_scan_cooldowns (client_id, last_scanned)
                    VALUES (%s, NOW())
                    ON CONFLICT (client_id) DO UPDATE SET last_scanned = EXCLUDED.last_scanned
                    WHERE radar_local_scan_cooldowns.last_scanned <= NOW() - INTERVAL '30 seconds'
                    RETURNING last_scanned""",
                    (client_id,),
                )
                allowed = cursor.fetchone() is not None
            connection.commit()
        if not allowed:
            raise HTTPException(status_code=429, detail="Nearby scans can be refreshed every 30 seconds", headers={"Retry-After": "30"})
        return
    now = time.monotonic()
    if now - local_scan_cooldowns.get(client_id, 0) < 30:
        raise HTTPException(status_code=429, detail="Nearby scans can be refreshed every 30 seconds", headers={"Retry-After": "30"})
    local_scan_cooldowns[client_id] = now


async def _coordinates_for_zip(zip_code):
    global last_zip_geocode_at
    async with zip_geocode_lock:
        delay = 1.05 - (time.monotonic() - last_zip_geocode_at)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            response = await http_client.get(
                ZIP_GEOCODE_URL,
                params={"q": f"{zip_code}, USA", "format": "jsonv2", "limit": 1, "countrycodes": "us"},
                headers={"User-Agent": "TCG-Radar Local Radar/3.5 (public store finder)"},
                timeout=12,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=503, detail="ZIP lookup is temporarily unavailable; please try again")
        finally:
            last_zip_geocode_at = time.monotonic()
    if not result:
        raise HTTPException(status_code=422, detail="That ZIP code could not be found")
    latitude, longitude = safe_float(result[0].get("lat")), safe_float(result[0].get("lon"))
    if latitude is None or longitude is None:
        raise HTTPException(status_code=422, detail="That ZIP code did not return a usable location")
    return latitude, longitude


@app.post("/api/local/scan")
async def local_scan(payload: dict, authorization: str = Header(default="")):
    # ZIP codes and derived coordinates are used only for this response. The
    # durable rate-limit data stores a device ID, timestamp and ZIP hash only.
    zip_code = str(payload.get("zip_code", "")).strip()
    client_id = str(payload.get("client_id", "")).strip()[:80]
    session_id = str(payload.get("scan_session", "")).strip()[:120]
    radius_miles = safe_float(payload.get("radius_miles"))
    if not re.fullmatch(r"\d{5}", zip_code):
        raise HTTPException(status_code=422, detail="Enter a valid five-digit ZIP code")
    if radius_miles not in {5.0, 10.0, 20.0, 50.0}:
        raise HTTPException(status_code=422, detail="Choose a 5, 10, 20, or 50 mile radius")
    if http_client is None:
        raise HTTPException(status_code=503, detail="Local Radar is starting; try again shortly")
    _enforce_local_scan_cooldown(client_id)

    role = _manager_role_or_none(authorization)
    is_manager = role in {"owner", "moderator", "google_owner"}
    remaining = None
    if not is_manager and not _valid_local_zip_session(client_id, zip_code, session_id):
        session_id, remaining = _start_local_zip_session(client_id, zip_code, False)

    latitude, longitude = await _coordinates_for_zip(zip_code)
    radius_meters = int(radius_miles * 1609.344)
    query = f"""[out:json][timeout:12];
(
  nwr["name"~"Walmart|Target|Best Buy|CVS|Walgreens|GameStop|Barnes & Noble",i](around:{radius_meters},{latitude},{longitude});
);
out center tags;"""
    try:
        response = await http_client.post(LOCAL_STORE_SEARCH_URL, data={"data": query}, timeout=18)
        response.raise_for_status()
        elements = response.json().get("elements", [])
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Store discovery is temporarily unavailable; please try again")

    stores, seen = [], set()
    for element in elements:
        tags = element.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        retailer = _local_retailer(name)
        point = element.get("center") or element
        store_lat, store_lon = safe_float(point.get("lat")), safe_float(point.get("lon"))
        if not retailer or store_lat is None or store_lon is None:
            continue
        distance = _distance_miles(latitude, longitude, store_lat, store_lon)
        if distance > radius_miles + 0.1:
            continue
        key = f"{retailer}:{round(store_lat, 5)}:{round(store_lon, 5)}"
        if key in seen:
            continue
        seen.add(key)
        address = " ".join(str(tags.get(key, "")).strip() for key in ("addr:housenumber", "addr:street") if tags.get(key)).strip()
        if tags.get("addr:city"):
            address = f"{address}, {tags['addr:city']}".strip(", ")
        stores.append({
            "id": key, "retailer": retailer, "name": name, "address": address or None,
            "latitude": store_lat, "longitude": store_lon, "distance_miles": round(distance, 1),
            "inventory_status": "unknown", "inventory_source": "automated",
            "inventory_note": "Store found. Automated store-specific inventory is not supported for this retailer yet.",
            "items": [], "checked_at": now_iso(),
        })
    stores.sort(key=lambda store: (store["distance_miles"], store["name"].lower()))
    return {
        "generated_at": now_iso(), "radius_miles": int(radius_miles), "stores": stores[:80],
        "inventory_scan": {"attempted": len(stores[:80]), "verified_local_inventory": 0},
        "scan_session": session_id, "zip_searches_remaining": remaining, "manager_bypass": is_manager,
    }


def _feed_item_for_source(source: dict) -> dict:
    """Expose every enabled tracker before its first scheduled check completes."""
    product_key = source_id(source)
    current = products.get(product_key)
    if current:
        return current

    store = retailer_name(source["url"], source.get("store", "Unknown"))
    return {
        "id": product_key,
        "game": source.get("game", source.get("category", "Other")),
        "category": source.get("category", source.get("game", "Other")),
        "area": source.get("area", "Online"),
        "set_name": source.get("set_name", ""),
        "catalog_key": source.get("catalog_key", ""),
        "product_type": source.get("product_type", "other_pack_product"),
        "packs": source.get("packs"),
        **_stock_estimate_fields(source),
        "store": store,
        "product": source.get("product", "Unnamed product"),
        "url": source["url"],
        "status": "unknown",
        "previous_status": None,
        "status_changed": False,
        "restock_armed": False,
        "restock_session": 0,
        "in_stock_streak": 0,
        "marketplace_streak": 0,
        "live_alerted": False,
        "marketplace_alerted": False,
        "restock_confirmed": False,
        "official_seller_verified": False,
        "price": None,
        "msrp": safe_float(source.get("msrp")),
        "markup": None,
        "quantity": None,
        "image_url": str(source.get("image_url", "")).strip(),
        "priority": source.get("priority", "normal"),
        "max_markup": safe_float(source.get("max_markup"), 80),
        "base_interval_seconds": interval_for(source),
        "checked_at": None,
        "first_seen_at": None,
        "notification_at": None,
        "response_ms": None,
        "http_status": None,
        "seller": "",
        "parser_version": None,
        "evidence": "Tracker is queued for its first safe retailer check.",
    }


@app.get("/api/feed")
async def feed(authorization: str = Header(default="")):
    manager = owner_feed_access(authorization)
    all_items = [
        _feed_item_for_source(source)
        for source in load_sources()
    ]
    # The full tracking catalog is an internal tool. Public members only receive
    # confirmed live-drop cards from the last 30 minutes.
    if manager:
        items = all_items
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        def public_live_drop(item):
            raw = item.get("notification_at")
            if not raw:
                return False
            try:
                stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return stamp >= cutoff
            except (TypeError, ValueError):
                return False
        items = [item for item in all_items if public_live_drop(item)]

    # Keep recent activity first within each inventory class, then place verified
    # retailer-direct offers ahead of marketplace offers. Marketplace ordering
    # does not affect the user-specific alert preference.
    items.sort(key=lambda item: (item.get("notification_at") or "", item.get("first_seen_at") or ""), reverse=True)
    def offer_priority(item: dict) -> int:
        if item.get("status") == "in_stock" and item.get("official_seller_verified"):
            return 0
        if item.get("official_seller_verified"):
            return 1
        if item.get("status") == "marketplace_in_stock":
            return 3
        return 2
    items.sort(key=offer_priority)

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
        "can_manage_feed": bool(manager),
        "tracked_total": len(all_items) if manager else None,
    }


@app.get("/api/retailer-health")
async def retailer_status(authorization: str = Header(default="")):
    manager_role(authorization)
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


@app.get("/api/alerts")
async def alert_history():
    return {"items": _alert_history()}


@app.get("/api/recently-missed")
async def recently_missed():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    current = {item.get("id"): _feed_item_for_source(item) for item in load_sources()}
    result, seen = [], set()
    for event in _alert_history():
        product_id = event.get("product_id")
        if product_id in seen:
            continue
        try:
            created = datetime.fromisoformat(str(event.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        live = current.get(product_id, {})
        if created >= cutoff and live.get("status") == "sold_out":
            seen.add(product_id)
            result.append({**event, "sold_out_at": live.get("checked_at") or now_iso()})
    result.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"items": result[:12]}


@app.get("/api/auth/config")
async def auth_config():
    return {"enabled": bool(GOOGLE_CLIENT_ID), "client_id": GOOGLE_CLIENT_ID or None}

@app.post("/api/visitors/heartbeat")
async def visitor_heartbeat(payload: dict):
    client_id = str(payload.get("client_id", "")).strip()[:80]
    if not client_id or not database_enabled(): return {"counted": False}
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO radar_visitors (client_id) VALUES (%s) ON CONFLICT (client_id) DO UPDATE SET last_seen = NOW()", (client_id,))
        connection.commit()
    return {"counted": True}


@app.get("/api/auth/me")
async def auth_me(authorization: str = Header(default="")):
    return google_user(authorization)


@app.get("/api/purchases")
async def list_purchases(authorization: str = Header(default="")):
    user = google_user(authorization)
    return {"items": _user_purchases(user["google_sub"])}


@app.post("/api/purchases", status_code=201)
async def add_purchase(payload: dict, authorization: str = Header(default="")):
    user = google_user(authorization)
    name = str(payload.get("name", "")).strip()[:160]
    quantity = int(payload.get("quantity", 1))
    paid = safe_float(payload.get("paid"))
    if not name or quantity < 1 or paid is None or paid < 0:
        raise HTTPException(status_code=422, detail="Product, quantity, and a valid amount paid are required")
    purchase = {"id": secrets.token_urlsafe(12), "name": name, "quantity": quantity, "paid": round(paid, 2), "retailer": str(payload.get("retailer", "")).strip()[:80], "date": str(payload.get("date", "")).strip()[:30], "notes": str(payload.get("notes", "")).strip()[:300]}
    if not database_enabled():
        raise HTTPException(status_code=503, detail="Persistent purchase storage is not available")
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO radar_purchases (id, google_sub, payload) VALUES (%s, %s, %s::jsonb)", (purchase["id"], user["google_sub"], json.dumps(purchase)))
        connection.commit()
    return purchase


@app.delete("/api/purchases/{purchase_id}")
async def delete_purchase(purchase_id: str, authorization: str = Header(default="")):
    user = google_user(authorization)
    if not database_enabled():
        raise HTTPException(status_code=503, detail="Persistent purchase storage is not available")
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM radar_purchases WHERE id = %s AND google_sub = %s", (purchase_id, user["google_sub"]))
        connection.commit()
    return {"deleted": True}


@app.get("/api/help/messages")
async def help_messages(thread_id: str = ""):
    thread_id = str(thread_id).strip()[:80]
    if not thread_id:
        raise HTTPException(status_code=422, detail="A help conversation ID is required")
    return {"items": _support_messages(thread_id)}


@app.post("/api/help/messages", status_code=201)
async def create_help_message(payload: dict):
    thread_id = str(payload.get("thread_id", "")).strip()[:80]
    client_id = str(payload.get("client_id", "")).strip()[:80]
    body = str(payload.get("body", "")).strip()[:1000]
    if not thread_id or not client_id or not body:
        raise HTTPException(status_code=422, detail="Conversation ID and message are required")
    if _is_support_banned(client_id):
        raise HTTPException(status_code=403, detail="This help account has been blocked from sending messages")
    message = {"id": secrets.token_urlsafe(12), "thread_id": thread_id, "client_id": client_id, "sender": "user", "body": body, "created_at": now_iso()}
    _write_support_message(message)
    return message


@app.get("/api/admin/status")
async def admin_status(authorization: str = Header(default="")):
    require_admin(authorization)
    return {"configured": True, "storage": str(SOURCES_FILE.name), "persistent_volume_required": "RAILWAY_VOLUME_MOUNT_PATH" not in os.environ}

@app.post("/api/admin/discovery/run")
async def run_public_discovery(authorization: str = Header(default="")):
    require_admin(authorization)
    return await discover_best_buy_products(allow_disabled=True)


@app.get("/api/admin/discovery")
async def admin_discovery(authorization: str = Header(default="")):
    require_product_manager(authorization)
    response = {
        "enabled": BEST_BUY_DISCOVERY_ENABLED,
        "interval_seconds": DISCOVERY_INTERVAL_SECONDS,
        "retailer": "Best Buy",
        "catalog_products": 0,
        "pending_verification": 0,
        "runs": [],
    }
    if not database_enabled():
        response["storage"] = "unavailable"
        return response
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM radar_catalog_products")
            response["catalog_products"] = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM radar_catalog_products WHERE payload->>'discovery_status' = 'pending_verification'")
            response["pending_verification"] = cursor.fetchone()[0]
            cursor.execute("SELECT payload FROM radar_discovery_runs WHERE retailer = %s ORDER BY created_at DESC LIMIT 20", ("Best Buy",))
            response["runs"] = [row[0] for row in cursor.fetchall()]
    response["storage"] = "postgres"
    return response


@app.get("/api/admin/usage")
async def admin_usage(authorization: str = Header(default="")):
    require_admin(authorization)
    if not database_enabled(): return {"anonymous_devices": 0, "google_accounts": 0, "push_devices": 0, "help_devices": 0}
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM radar_visitors"); anonymous = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM radar_users"); accounts = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM push_subscriptions"); push = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(DISTINCT client_id) FROM radar_support_messages"); help_users = cursor.fetchone()[0]
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT retailer, clicks FROM radar_retailer_link_clicks ORDER BY clicks DESC, retailer")
            link_clicks = [{"retailer": row[0], "clicks": row[1]} for row in cursor.fetchall()]
    return {"anonymous_devices": anonymous, "google_accounts": accounts, "push_devices": push, "help_devices": help_users, "link_clicks": link_clicks, "link_click_total": sum(item["clicks"] for item in link_clicks)}


@app.post("/api/analytics/link-click")
async def record_retailer_link_click(payload: dict):
    retailer = str(payload.get("retailer", "")).strip()[:80]
    if not retailer or not database_enabled():
        return {"counted": False}
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO radar_retailer_link_clicks (retailer, clicks) VALUES (%s, 1) ON CONFLICT (retailer) DO UPDATE SET clicks = radar_retailer_link_clicks.clicks + 1, updated_at = NOW()", (retailer,))
        connection.commit()
    return {"counted": True}


@app.get("/api/admin/help")
async def admin_help(authorization: str = Header(default="")):
    require_admin(authorization)
    return {"items": _support_messages(limit=100)}


@app.post("/api/admin/help/{thread_id}/reply", status_code=201)
async def reply_help(thread_id: str, payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    body = str(payload.get("body", "")).strip()[:1000]
    if not body:
        raise HTTPException(status_code=422, detail="Reply cannot be empty")
    existing = _support_messages(thread_id, limit=1)
    client_id = existing[0]["client_id"] if existing else "unknown"
    message = {"id": secrets.token_urlsafe(12), "thread_id": thread_id, "client_id": client_id, "sender": "owner", "body": body, "created_at": now_iso()}
    _write_support_message(message)
    return message


@app.post("/api/admin/help/{client_id}/ban")
async def ban_help_client(client_id: str, authorization: str = Header(default="")):
    require_admin(authorization)
    client_id = str(client_id).strip()[:80]
    if not client_id:
        raise HTTPException(status_code=422, detail="A client ID is required")
    _ban_support_client(client_id)
    return {"banned": True, "client_id": client_id}


@app.get("/api/admin/catalog")
async def admin_catalog(status: str = "", authorization: str = Header(default="")):
    require_product_manager(authorization)
    if not database_enabled():
        return {"items": [], "storage": "unavailable"}
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            if status:
                cursor.execute(
                    "SELECT payload FROM radar_catalog_products WHERE payload->>'discovery_status' = %s ORDER BY last_seen_at DESC LIMIT 250",
                    (status,),
                )
            else:
                cursor.execute("SELECT payload FROM radar_catalog_products ORDER BY last_seen_at DESC LIMIT 250")
            items = [row[0] for row in cursor.fetchall()]
    return {"items": items, "storage": "postgres"}


@app.post("/api/admin/catalog/{canonical_key}/approve")
async def approve_catalog_product(canonical_key: str, authorization: str = Header(default="")):
    require_admin(authorization)
    if not database_enabled():
        raise HTTPException(status_code=503, detail="PostgreSQL is required for discovery approval")
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM radar_catalog_products WHERE canonical_key = %s", (canonical_key,))
            row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Discovered product was not found")
    candidate = row[0]
    if candidate.get("discovery_status") != "pending_verification":
        raise HTTPException(status_code=409, detail="This catalog product is not awaiting verification")
    source = _clean_source({
        "product": candidate.get("title"),
        "url": candidate.get("product_url"),
        "game": candidate.get("tcg"),
        "store": candidate.get("retailer"),
        "set_name": candidate.get("set_name", ""),
        "product_type": candidate.get("product_type", "other_pack_product"),
        "priority": "normal",
        "area": "Online",
    })
    sources = _all_sources()
    if not any(canonical_product_key(item) == canonical_key for item in sources):
        sources.append(source)
        _write_sources(sources)
    candidate["discovery_status"] = "monitoring"
    candidate["approved_at"] = now_iso()
    upsert_catalog_product(candidate)
    return {"approved": True, "source": {**source, "id": source_id(source)}}


@app.post("/api/admin/catalog/{canonical_key}/reject")
async def reject_catalog_product(canonical_key: str, authorization: str = Header(default="")):
    require_admin(authorization)
    if not database_enabled():
        raise HTTPException(status_code=503, detail="PostgreSQL is required for discovery review")
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM radar_catalog_products WHERE canonical_key = %s", (canonical_key,))
            row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Discovered product was not found")
    candidate = row[0]
    candidate["discovery_status"] = "rejected"
    candidate["rejected_at"] = now_iso()
    upsert_catalog_product(candidate)
    return {"rejected": True}


@app.get("/api/admin/priority-automation")
async def get_priority_automation(authorization: str = Header(default="")):
    role = require_product_manager(authorization)
    return {**priority_automation, "role": role}


@app.patch("/api/admin/priority-automation")
async def update_priority_automation(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    if "auto_high_priority" not in payload:
        raise HTTPException(status_code=422, detail="Provide the auto high priority setting")
    return write_priority_automation({
        **priority_automation,
        "auto_high_priority": bool(payload.get("auto_high_priority")),
    })


@app.get("/api/admin/products")
async def admin_products(authorization: str = Header(default="")):
    role = require_product_manager(authorization)
    sources = _all_sources()
    return {"role": role, "items": [{**source, "id": source_id(source)} for source in sources]}


@app.post("/api/admin/products/test-link")
async def test_official_product_link(payload: dict, authorization: str = Header(default="")):
    require_product_manager(authorization)
    url = canonical_product_url(payload.get("url", ""))
    if not verified_product_url(url):
        raise HTTPException(status_code=422, detail="Use a stable HTTPS product page from an approved official retailer")
    try:
        response = await http_client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=12,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="The retailer page could not be reached right now")
    if response.status_code != 200:
        raise HTTPException(status_code=422, detail=f"The retailer page returned HTTP {response.status_code}")
    return {
        "valid": True,
        "url": url,
        "retailer": retailer_name(url),
        "title": extract_page_title(response.text),
        "image_url": extract_image_url(response.text),
        "message": "Official retailer product page reached. This test does not check or claim stock.",
    }


@app.post("/api/admin/products", status_code=201)
async def add_product(payload: dict, authorization: str = Header(default="")):
    require_product_manager(authorization)
    source = _clean_source({**payload, "published": False, "added_at": now_iso()})
    # A manual HTTPS override wins; otherwise one public page read finds a thumbnail.
    if not source.get("image_url"):
        image_url = await fetch_best_product_image(source)
        if image_url:
            source["image_url"] = image_url
    sources = _all_sources()
    if any(source.get("url") == item.get("url") for item in sources):
        raise HTTPException(status_code=409, detail="That product URL is already being monitored")
    if any(monitored_product_key(source) == monitored_product_key(item) for item in sources):
        raise HTTPException(status_code=409, detail="That retailer and product are already in the tracking list")
    sources.append(source)
    _write_sources(sources)
    return {**source, "id": source_id(source), "staged": True}


@app.post("/api/admin/products/refresh-images")
async def refresh_missing_product_images(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    requested = int(payload.get("limit", 25) or 25)
    result = await fill_missing_product_images(limit=min(25, max(1, requested)))
    return {**result, "message": f"Found {result['refreshed']} official image(s). {result['remaining']} item(s) still need an image."}


@app.post("/api/admin/restart")
async def restart_server(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    staged_count = sum(1 for source in _all_sources() if source.get("published") is False)
    if staged_count and not bool(payload.get("confirm_staged", False)):
        return {"restarting": False, "requires_confirmation": True, "staged_count": staged_count}
    async def stop_process():
        await asyncio.sleep(1)
        os._exit(0)
    asyncio.create_task(stop_process())
    return {"restarting": True, "staged_count": staged_count, "message": "Restart requested. Railway will bring TCG Radar back online shortly."}


@app.post("/api/admin/products/publish")
async def publish_staged_products(authorization: str = Header(default="")):
    require_admin(authorization)
    sources = _all_sources()
    published = 0
    for source in sources:
        if not source.get("published", True):
            source["published"] = True
            published += 1
    if published:
        _write_sources(sources)
    return {"published": published, "message": f"Published {published} staged item(s) to live monitoring"}


@app.patch("/api/admin/products/{product_id}")
async def update_product(product_id: str, payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    sources = _all_sources()
    for index, current in enumerate(sources):
        if source_id(current) != product_id:
            continue
        if "stock_estimate" in payload:
            estimate = str(payload.get("stock_estimate") or "").strip()[:60]
            payload["stock_estimate"] = estimate
            if estimate:
                ttl_hours = safe_float(payload.get("stock_estimate_ttl_hours"), 24)
                ttl_hours = min(168, max(1, ttl_hours if ttl_hours is not None else 24))
                payload["stock_estimate_reported_at"] = now_iso()
                payload["stock_estimate_expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
                ).isoformat()
            else:
                payload["stock_estimate_reported_at"] = None
                payload["stock_estimate_expires_at"] = None
        source = _clean_source({**current, **payload})
        # Clearing the override asks the app to use the official retailer page image again.
        if "image_url" in payload and not source.get("image_url"):
            image_url = await fetch_best_product_image(source)
            if image_url:
                source["image_url"] = image_url
        if any(index != other_index and source.get("url") == other.get("url") for other_index, other in enumerate(sources)):
            raise HTTPException(status_code=409, detail="That product URL is already being monitored")
        if any(index != other_index and monitored_product_key(source) == monitored_product_key(other) for other_index, other in enumerate(sources)):
            raise HTTPException(status_code=409, detail="That retailer and product are already in the tracking list")
        sources[index] = source
        products.pop(product_id, None)
        _write_sources(sources)
        return {**source, "id": source_id(source)}
    raise HTTPException(status_code=404, detail="Monitored product was not found")


@app.delete("/api/admin/products/{product_id}", status_code=204)
async def delete_product(product_id: str, authorization: str = Header(default="")):
    require_product_manager(authorization)
    sources = _all_sources()
    remaining = [item for item in sources if source_id(item) != product_id]
    if len(remaining) == len(sources):
        raise HTTPException(status_code=404, detail="Monitored product was not found")
    products.pop(product_id, None)
    _write_sources(remaining)


@app.get("/api/admin/owner-pin")
async def owner_pin_status(authorization: str = Header(default="")):
    require_admin(authorization)
    return {"configured": bool(_owner_pin_hash()), "min_length": 4, "max_length": 20}


@app.put("/api/admin/owner-pin")
async def set_owner_pin(payload: dict, authorization: str = Header(default="")):
    # Only the verified Google owner or existing recovery secret may set the owner PIN.
    token = authorization.removeprefix("Bearer ").strip()
    if not ((ADMIN_SECRET and token and secrets.compare_digest(token, ADMIN_SECRET)) or _is_google_owner(authorization)):
        raise HTTPException(status_code=403, detail="Sign in with the owner Google account to change the owner PIN")
    current_pin = str(payload.get("current_pin", "")).strip()
    pin = str(payload.get("pin", "")).strip()
    confirm_pin = str(payload.get("confirm_pin", "")).strip()
    stored_hash = _owner_pin_hash()
    if stored_hash and not (current_pin and secrets.compare_digest(_token_hash(current_pin), stored_hash)):
        raise HTTPException(status_code=401, detail="Incorrect PIN")
    if not PIN_PATTERN.fullmatch(pin):
        raise HTTPException(status_code=422, detail="PIN must be 4 to 20 numbers")
    if pin != confirm_pin:
        raise HTTPException(status_code=422, detail="New PIN entries do not match")
    _write_owner_pin_hash(_token_hash(pin))
    return {"configured": True, "message": "Successful"}


@app.get("/api/admin/moderators")
async def list_moderators(authorization: str = Header(default="")):
    require_admin(authorization)
    return {
        "owner_nickname": _owner_nickname(),
        "items": [{"id": moderator["id"], "nickname": moderator["name"], "created_at": moderator.get("created_at")} for moderator in _moderators()],
    }


@app.put("/api/profile/nickname")
async def set_public_nickname(payload: dict, authorization: str = Header(default="")):
    user = google_user(authorization)
    if not database_enabled():
        raise HTTPException(status_code=503, detail="Nickname storage is not available")
    nickname = _nickname(payload.get("nickname"))
    if _nickname_taken(nickname, exclude_google_sub=user["google_sub"]):
        raise HTTPException(status_code=409, detail="That nickname is already taken")
    with _database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE radar_users SET nickname = %s, updated_at = NOW() WHERE google_sub = %s", (nickname, user["google_sub"]))
        connection.commit()
    user["nickname"] = nickname
    return {"nickname": nickname}


@app.get("/api/profile/nickname")
async def get_public_nickname(authorization: str = Header(default="")):
    user = google_user(authorization)
    return {"nickname": user.get("nickname", "")}


@app.post("/api/admin/moderators", status_code=201)
async def add_moderator(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    name = _nickname(payload.get("nickname", payload.get("name", "")))
    if _nickname_taken(name):
        raise HTTPException(status_code=409, detail="That nickname is already taken")
    access_code = str(payload.get("access_code", "")).strip()
    if not PIN_PATTERN.fullmatch(access_code):
        raise HTTPException(status_code=422, detail="Moderator PIN must be 4 to 20 numbers")
    moderator = {"id": secrets.token_urlsafe(8), "name": name, "secret_hash": _token_hash(access_code)}
    moderators = _moderators()
    moderators.append(moderator)
    _write_moderators(moderators)
    return {"id": moderator["id"], "nickname": name, "access_code": access_code}


@app.delete("/api/admin/moderators/{moderator_id}", status_code=204)
async def delete_moderator(moderator_id: str, authorization: str = Header(default="")):
    require_admin(authorization)
    moderators = _moderators()
    kept = [moderator for moderator in moderators if moderator.get("id") != moderator_id]
    if len(kept) == len(moderators):
        raise HTTPException(status_code=404, detail="Moderator was not found")
    _write_moderators(kept)


def _quiet_hours_active(quiet_hours):
    if not quiet_hours.get("enabled") or quiet_hours.get("start_minute") == quiet_hours.get("end_minute"):
        return False
    now = datetime.now(timezone.utc)
    minute = (now.hour * 60 + now.minute - int(quiet_hours.get("timezone_offset", 0))) % 1440
    start, end = int(quiet_hours["start_minute"]), int(quiet_hours["end_minute"])
    return start <= minute < end if start < end else (minute >= start or minute < end)


def _matches_push_preferences(subscription, item):
    preference = _clean_push_preferences(subscription.get("preferences"))
    if _quiet_hours_active(preference.get("quiet_hours", {})):
        return False
    if preference["games"] and item.get("game") not in preference["games"]:
        return False
    if preference["stores"] and item.get("store") not in preference["stores"]:
        return False
    product_keys = {str(item.get("id", "")), str(item.get("catalog_key", ""))}
    if product_keys.intersection(preference.get("muted_product_ids", [])):
        return False
    if int(preference.get("muted_stores_until", {}).get(str(item.get("store", "")), 0) or 0) > int(time.time()):
        return False
    markup = safe_float(item.get("markup"))
    return not (markup is not None and markup > preference["max_markup"])


async def _broadcast_owner_confirmed_drop(event):
    if not push_ready():
        return
    payload = {
        "title": "TCG RADAR — OWNER CONFIRMED",
        "body": f"{event['product']} • {event['store']}" + (f" • ${event['price']:.2f}" if event.get("price") is not None else ""),
        "url": event["url"],
        "tag": event["id"],
    }
    expired = []
    for subscription in _subscriptions():
        if not _matches_push_preferences(subscription, event):
            continue
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


async def _broadcast_authorized_signal(event):
    if not push_ready():
        return
    payload = {
        "title": "TCG RADAR — AUTHORIZED SIGNAL",
        "body": event["product"] + " • " + event["store"] + ((" • $" + format(event["price"], ".2f")) if event.get("price") is not None else ""),
        "url": event["url"],
        "tag": event["id"],
    }
    expired = []
    for subscription in _subscriptions():
        if not _matches_push_preferences(subscription, event):
            continue
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


async def _broadcast_announcement(title, body, url):
    payload = {"title": title, "body": body, "url": url, "tag": f"announcement-{int(time.time())}"}
    expired = []
    for subscription in _subscriptions():
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


@app.get("/api/admin/intake-sources")
async def list_intake_sources(authorization: str = Header(default="")):
    require_admin(authorization)
    return {"items": [_public_intake_source(item) for item in _authorized_intake_sources()]}


@app.post("/api/admin/intake-sources", status_code=201)
async def create_intake_source(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    label = str(payload.get("label", "")).strip()[:80]
    if not label:
        raise HTTPException(status_code=422, detail="Give this authorized source a label")
    sources = _authorized_intake_sources()
    secret = secrets.token_urlsafe(24)
    source = {"id": secrets.token_urlsafe(10), "label": label, "secret_hash": _token_hash(secret), "created_at": now_iso()}
    sources.append(source)
    _write_authorized_intake_sources(sources)
    return {**_public_intake_source(source), "webhook_url": "/api/intake/" + source["id"], "secret": secret}


@app.delete("/api/admin/intake-sources/{intake_id}", status_code=204)
async def delete_intake_source(intake_id: str, authorization: str = Header(default="")):
    require_admin(authorization)
    sources = _authorized_intake_sources()
    kept = [item for item in sources if item.get("id") != intake_id]
    if len(kept) == len(sources):
        raise HTTPException(status_code=404, detail="Authorized source was not found")
    _write_authorized_intake_sources(kept)


@app.post("/api/intake/{intake_id}", status_code=201)
async def receive_authorized_signal(intake_id: str, payload: dict, x_tcg_radar_intake_key: str = Header(default="")):
    source = next((item for item in _authorized_intake_sources() if item.get("id") == intake_id), None)
    if not source or not x_tcg_radar_intake_key or not secrets.compare_digest(_token_hash(x_tcg_radar_intake_key), source.get("secret_hash", "")):
        raise HTTPException(status_code=401, detail="Authorized intake key was not accepted")
    product = str(payload.get("product", "")).strip()[:160]
    game = str(payload.get("game", "")).strip()
    url = canonical_product_url(payload.get("url", ""))
    declared_store = str(payload.get("store", "")).strip()
    store = retailer_name(url, declared_store)
    price = safe_float(payload.get("price"))
    if not product or game not in {"Pokemon", "One Piece", "Magic", "Other"}:
        raise HTTPException(status_code=422, detail="A product name and supported game are required")
    if not verified_product_url(url) or (declared_store and declared_store != store):
        raise HTTPException(status_code=422, detail="Use a matching approved official retailer product URL")
    if payload.get("retailer_direct_confirmed") is not True:
        raise HTTPException(status_code=422, detail="Authorized signals must explicitly confirm retailer-direct availability")
    recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    for item in _alert_history():
        if item.get("url") != url:
            continue
        try:
            created = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= recent_cutoff:
                return {"accepted": False, "deduplicated": True}
        except (TypeError, ValueError):
            continue
    event = {
        "id": "authorized-signal-" + secrets.token_urlsafe(10),
        "product_id": "authorized-signal-" + secrets.token_urlsafe(8),
        "product": product, "game": game, "store": store, "url": url,
        "status": "in_stock", "price": round(price, 2) if price is not None else None,
        "source": "authorized_external", "signal_label": source["label"],
        "verification": "Authorized external signal: " + source["label"],
        "created_at": now_iso(),
    }
    _write_alert_event(event)
    attempted = sum(1 for subscription in _subscriptions() if _matches_push_preferences(subscription, event)) if push_ready() else 0
    if attempted:
        asyncio.create_task(_broadcast_authorized_signal(event))
    return {"accepted": True, "push_attempted": attempted, "event_id": event["id"]}


@app.post("/api/admin/verified-drops", status_code=201)
async def create_verified_drop(payload: dict, authorization: str = Header(default="")):
    # This is an owner-only, explicitly human-confirmed signal. It is never
    # presented as a retailer inventory scan and it still uses the subscriber's
    # game, retailer and price filters.
    require_admin(authorization)
    product = str(payload.get("product", "")).strip()[:160]
    game = str(payload.get("game", "")).strip()
    url = str(payload.get("url", "")).strip()
    declared_store = str(payload.get("store", "")).strip()
    store = retailer_name(url, declared_store)
    price = safe_float(payload.get("price"))
    msrp = safe_float(payload.get("msrp"))
    if not product or game not in {"Pokemon", "One Piece", "Magic", "Other"}:
        raise HTTPException(status_code=422, detail="A product name and supported game are required")
    if not verified_product_url(url):
        raise HTTPException(status_code=422, detail="Use an exact HTTPS product page from a supported retailer")
    if declared_store and store != declared_store:
        raise HTTPException(status_code=422, detail="The retailer must match the product link")
    if payload.get("seller_confirmed") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the retailer itself is the seller before posting")
    if price is not None and price < 0:
        raise HTTPException(status_code=422, detail="Price cannot be negative")
    if msrp is not None and msrp <= 0:
        raise HTTPException(status_code=422, detail="MSRP must be greater than zero")
    markup = None if price is None or msrp is None else round(((price - msrp) / msrp) * 100, 1)
    event = {
        "id": f"owner-confirmed-{secrets.token_urlsafe(10)}",
        "product_id": f"owner-confirmed-{secrets.token_urlsafe(8)}",
        "product": product,
        "game": game,
        "store": store,
        "url": url,
        "status": "in_stock",
        "price": round(price, 2) if price is not None else None,
        "msrp": round(msrp, 2) if msrp is not None else None,
        "markup": markup,
        "source": "owner_confirmed",
        "verification": "Owner-confirmed retailer-direct listing",
        "created_at": now_iso(),
    }
    _write_alert_event(event)
    attempted = 0
    if push_ready():
        attempted = sum(1 for subscription in _subscriptions() if _matches_push_preferences(subscription, event))
        if attempted:
            asyncio.create_task(_broadcast_owner_confirmed_drop(event))
    return {**event, "push_attempted": attempted}


@app.post("/api/admin/announcements")
async def announcement(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    if not push_ready():
        raise HTTPException(status_code=503, detail="Push alerts are not configured yet")
    title = str(payload.get("title", "TCG Radar update")).strip()[:70]
    body = str(payload.get("body", "")).strip()[:240]
    url = str(payload.get("url", "https://failedxassassin.github.io/TCG-Restock-Radar/")).strip()
    if not title or not body or not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="A title, message, and public link are required")
    attempted = len(_subscriptions())
    if attempted:
        asyncio.create_task(_broadcast_announcement(title, body, url))
    return {"attempted": attempted}


@app.post("/api/reports", status_code=201)
async def create_report(payload: dict, request: Request):
    product_id = str(payload.get("product_id", "")).strip()
    reason = str(payload.get("reason", "false_alert")).strip().lower()
    details = str(payload.get("details", "")).strip()[:500]
    allowed = {"false_alert", "wrong_price", "broken_link", "other"}
    if product_id not in products and not any(source_id(source) == product_id for source in _all_sources()):
        raise HTTPException(status_code=404, detail="Product was not found")
    if reason not in allowed:
        raise HTTPException(status_code=422, detail="That report type is not supported")
    report = {"id": secrets.token_urlsafe(10), "product_id": product_id, "reason": reason, "details": details}
    _write_report(report)
    return {"reported": True, "id": report["id"]}


@app.get("/api/admin/reports")
async def list_reports(authorization: str = Header(default="")):
    require_admin(authorization)
    return {"items": _reports()}


@app.get("/api/push/config")
async def push_config():
    return {"enabled": push_ready(), "public_key": VAPID_PUBLIC_KEY if push_ready() else None}


@app.post("/api/push/subscribe", status_code=201)
async def subscribe_push(payload: dict):
    if not push_ready():
        raise HTTPException(status_code=503, detail="Push alerts are not configured yet")
    subscription = payload.get("subscription", payload)
    if not _valid_subscription(subscription):
        raise HTTPException(status_code=422, detail="Browser push subscription is incomplete")
    preferences = _clean_push_preferences(payload.get("preferences"))
    subscriptions = _subscriptions()
    endpoint = subscription["endpoint"]
    subscriptions = [item for item in subscriptions if item.get("endpoint") != endpoint]
    subscriptions.append({**subscription, "preferences": preferences})
    _write_subscriptions(subscriptions)
    return {"subscribed": True, "preferences": preferences}


async def _broadcast_test_push():
    await asyncio.sleep(10)
    subscriptions = _subscriptions()
    payload = {"title": "TCG Radar closed-app test", "body": "This alert arrived while TCG Radar was closed.", "url": "https://failedxassassin.github.io/TCG-Restock-Radar/", "tag": "tcg-radar-closed-test"}
    expired = []
    for subscription in subscriptions:
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


@app.post("/api/admin/push/test")
async def test_push(authorization: str = Header(default="")):
    require_admin(authorization)
    if not push_ready():
        raise HTTPException(status_code=503, detail="Push alerts are not configured yet")
    attempted = len(_subscriptions())
    if attempted:
        asyncio.create_task(_broadcast_test_push())
    return {"attempted": attempted, "scheduled_delay_seconds": 10}

