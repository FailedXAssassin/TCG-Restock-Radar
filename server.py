import asyncio
import hashlib
import html
import json
import os
import secrets
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from parsers import PARSER_VERSION, parse_target, parse_walmart


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES_FILE = ROOT / "sources.json"
DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ROOT))
SOURCES_FILE = Path(os.environ.get("TCG_RADAR_DATA_PATH", DATA_DIR / "sources.json"))
PUSH_SUBSCRIPTIONS_FILE = Path(os.environ.get("TCG_RADAR_PUSH_SUBSCRIPTIONS_PATH", DATA_DIR / "push_subscriptions.json"))
MODERATORS_FILE = Path(os.environ.get("TCG_RADAR_MODERATORS_PATH", DATA_DIR / "moderators.json"))
ALERT_HISTORY_FILE = Path(os.environ.get("TCG_RADAR_ALERT_HISTORY_PATH", DATA_DIR / "alert_history.json"))
ADMIN_SECRET = os.environ.get("TCG_RADAR_ADMIN_SECRET", "")
VAPID_PUBLIC_KEY = os.environ.get("TCG_RADAR_VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("TCG_RADAR_VAPID_PRIVATE_KEY", "")
VAPID_CONTACT = os.environ.get("TCG_RADAR_VAPID_CONTACT", "mailto:owner@example.com")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", os.environ.get("TCG_RADAR_GOOGLE_CLIENT_ID", ""))
OWNER_EMAIL = os.environ.get("TCG_RADAR_OWNER_EMAIL", "").strip().lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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

MEANINGFUL_TRANSITIONS = {
    ("unknown", "loaded"), ("loaded", "in_stock"),
    ("sold_out", "in_stock"), ("unknown", "in_stock"),
}

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

    raw = _all_sources()
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
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_users (google_sub TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_purchases (id TEXT PRIMARY KEY, google_sub TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("CREATE TABLE IF NOT EXISTS radar_visitors (client_id TEXT PRIMARY KEY, first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cursor.execute("SELECT COUNT(*) FROM radar_products")
            if cursor.fetchone()[0] == 0:
                for source in _file_sources():
                    cursor.execute("INSERT INTO radar_products (id, payload) VALUES (%s, %s::jsonb)", (source_id(source), json.dumps(source)))
        connection.commit()


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


def _clean_source(payload):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Product data must be an object")
    url = str(payload.get("url", "")).strip()
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="A public product URL is required")
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
    return {
        "enabled": bool(payload.get("enabled", True)),
        "game": str(payload.get("game", "Other")).strip() or "Other",
        "area": str(payload.get("area", "Online")).strip() or "Online",
        "store": retailer_name(url, str(payload.get("store", "")).strip()),
        "product": product,
        "url": url,
        "msrp": msrp,
        "max_markup": max_markup,
        "priority": priority,
    }


def require_admin(authorization=Header(default="")):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Owner controls are not configured yet")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Owner secret is not valid")


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Manager controls are not configured yet")
    token = authorization.removeprefix("Bearer ").strip()
    if token and secrets.compare_digest(token, ADMIN_SECRET):
        return "owner"
    hashed = _token_hash(token) if token else ""
    for moderator in _moderators():
        if hashed and secrets.compare_digest(hashed, moderator.get("secret_hash", "")):
            return "moderator"
    raise HTTPException(status_code=401, detail="That owner or moderator code was not accepted")


def require_product_manager(authorization=Header(default="")):
    return manager_role(authorization)



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
    allowed_stores = {"Walmart", "Target", "Amazon", "Best Buy", "GameStop"}
    games = [str(item) for item in payload.get("games", []) if str(item) in allowed_games]
    stores = [str(item) for item in payload.get("stores", []) if str(item) in allowed_stores]
    return {"max_markup": max_markup, "games": games, "stores": stores}


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


def _write_alert_event(event):
    if database_enabled():
        with _database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO radar_alert_events (id, product_id, payload) VALUES (%s, %s, %s::jsonb)", (event["id"], event["product_id"], json.dumps(event)))
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
            connection.commit()
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
    if not item.get("status_changed"):
        return
    previous, current = item.get("previous_status"), item.get("status")
    if (previous, current) not in MEANINGFUL_TRANSITIONS:
        return
    event = {
        "id": secrets.token_urlsafe(12),
        "product_id": item.get("id"),
        "product": item.get("product"),
        "store": item.get("store"),
        "url": item.get("url"),
        "previous_status": previous,
        "status": current,
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
        if preference["games"] and item.get("game") not in preference["games"]:
            continue
        if preference["stores"] and item.get("store") not in preference["stores"]:
            continue
        if current == "in_stock" and markup is not None and markup > preference["max_markup"]:
            continue
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


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
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'["\']image["\']\s*:\s*["\'](https?://[^"\']+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            candidate = html.unescape(match.group(1)).strip()
            if candidate.startswith("https://"):
                return candidate
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

        if invitation_signal(response.text):
            # Invitation-only is not purchasable stock. Keep the listing quiet
            # and unconfirmed so it cannot generate a false restock push.
            status = "unknown"
            price = None
        quantity = detect_quantity(
            response.text
        )
        image_url = str(source.get("image_url", "")).strip() or extract_image_url(response.text)

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

        checked_at = now_iso()
        notification_at = previous.get("notification_at")
        if (old_status, status) in MEANINGFUL_TRANSITIONS:
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

    ensure_data_files()
    ensure_database()
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
    version="3.4.4",
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
        "version": "3.4.4",
        "status": "online",
        "message": "TCG Radar backend is running.",
    }


@app.get("/health")
async def health():
    sources = load_sources()

    return {
        "status": "online",
        "version": "3.4.4",
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
        "persistent_storage": "postgres" if database_enabled() else ("volume" if "RAILWAY_VOLUME_MOUNT_PATH" in os.environ else "ephemeral"),
    }


@app.get("/api/feed")
async def feed():
    items = list(
        products.values()
    )

    # Notification activity gets priority; otherwise retain first-seen order
    # so routine background checks never reshuffle the product list.
    items.sort(key=lambda item: (item.get("notification_at") or "", item.get("first_seen_at") or ""), reverse=True)

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
    return {"anonymous_devices": anonymous, "google_accounts": accounts, "push_devices": push, "help_devices": help_users}


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


@app.get("/api/admin/products")
async def admin_products(authorization: str = Header(default="")):
    role = require_product_manager(authorization)
    sources = _all_sources()
    return {"role": role, "items": [{**source, "id": source_id(source)} for source in sources]}


@app.post("/api/admin/products", status_code=201)
async def add_product(payload: dict, authorization: str = Header(default="")):
    require_product_manager(authorization)
    source = _clean_source(payload)
    sources = _all_sources()
    if any(source.get("url") == item.get("url") for item in sources):
        raise HTTPException(status_code=409, detail="That product URL is already being monitored")
    sources.append(source)
    _write_sources(sources)
    return {**source, "id": source_id(source)}


@app.patch("/api/admin/products/{product_id}")
async def update_product(product_id: str, payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    sources = _all_sources()
    for index, current in enumerate(sources):
        if source_id(current) != product_id:
            continue
        source = _clean_source({**current, **payload})
        if any(index != other_index and source.get("url") == other.get("url") for other_index, other in enumerate(sources)):
            raise HTTPException(status_code=409, detail="That product URL is already being monitored")
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


@app.get("/api/admin/moderators")
async def list_moderators(authorization: str = Header(default="")):
    require_admin(authorization)
    return {"items": [{"id": moderator["id"], "name": moderator["name"], "created_at": moderator.get("created_at")} for moderator in _moderators()]}


@app.post("/api/admin/moderators", status_code=201)
async def add_moderator(payload: dict, authorization: str = Header(default="")):
    require_admin(authorization)
    name = str(payload.get("name", "")).strip()
    if not 2 <= len(name) <= 60:
        raise HTTPException(status_code=422, detail="Moderator name must be 2 to 60 characters")
    access_code = secrets.token_urlsafe(18)
    moderator = {"id": secrets.token_urlsafe(8), "name": name, "secret_hash": _token_hash(access_code)}
    moderators = _moderators()
    moderators.append(moderator)
    _write_moderators(moderators)
    return {"id": moderator["id"], "name": name, "access_code": access_code}


@app.delete("/api/admin/moderators/{moderator_id}", status_code=204)
async def delete_moderator(moderator_id: str, authorization: str = Header(default="")):
    require_admin(authorization)
    moderators = _moderators()
    kept = [moderator for moderator in moderators if moderator.get("id") != moderator_id]
    if len(kept) == len(moderators):
        raise HTTPException(status_code=404, detail="Moderator was not found")
    _write_moderators(kept)


async def _broadcast_announcement(title, body, url):
    payload = {"title": title, "body": body, "url": url, "tag": f"announcement-{int(time.time())}"}
    expired = []
    for subscription in _subscriptions():
        if await asyncio.to_thread(_send_web_push, subscription, payload):
            expired.append(subscription.get("endpoint"))
    if expired:
        _write_subscriptions([item for item in _subscriptions() if item.get("endpoint") not in expired])


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

