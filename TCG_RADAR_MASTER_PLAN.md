# TCG Radar --- Master Project Plan

**Project:** TCG Radar\
**Repository:** `FailedXAssassin/TCG-Restock-Radar`\
**Backend:** Railway + FastAPI\
**Frontend:** Installable Android-friendly PWA

> **Instruction for ChatGPT Work:** Read this entire file before
> changing TCG Radar. Inspect the current repository and deployed
> behavior first. Treat this as the project's living source of truth.
> Planned features are not necessarily implemented. Update this document
> whenever implementation status or architecture changes.

## 1. Mission

TCG Radar helps users catch Pokémon TCG, One Piece Card Game, and Magic:
The Gathering products at MSRP or reasonably close to MSRP without
requiring expensive restock-alert subscriptions.

Core principle: **fast and useful without being abusive.** Use
legitimate public retailer data, conservative request pacing,
staggering, caching, and adaptive backoff. Never bypass CAPTCHAs, access
controls, private/employee systems, authentication, or anti-bot
protections.

The product should also avoid annoying/scummy UX: no deceptive ads,
moving buttons, obstructive popups, fake urgency, or intentionally
miserable free tiers.

## 2. Current Architecture

GitHub repository: `FailedXAssassin/TCG-Restock-Radar`, public, default
branch `main`.

Known files include `server.py`, `requirements.txt`, `railway.json`,
`sources.json`, `restocks.json`, `app.js`, `index.html`, `style.css`,
`manifest.webmanifest`, `service-worker.js`, `icon.svg`,
`scripts/check_restocks.py`, and `.github/workflows/restock-check.yml`.

Railway service: `TCG-Restock-Radar`, environment `production`.

Backend URL: `https://tcg-restock-radar-production.up.railway.app`

Known endpoints: - `/health` - `/api/feed` - `/api/retailer-health`

The backend uses FastAPI/Uvicorn with an asynchronous background
scheduler.

Railway auto-deploy from GitHub has not behaved consistently; some
recent changes required manual deployment. Verify/fix auto-deploy after
the backend is stable.

## 3. Exact Current Stopping Point

The backend is running after correcting Python indentation errors
introduced during mobile GitHub editing.

### Walmart test

Pokémon Scarlet & Violet Prismatic Evolutions Elite Trainer Box\
URL: `https://www.walmart.com/ip/13816151308`\
MSRP: \$49.99\
Priority: high

Recent result: HTTP 200, `status: unknown`, `price: null`.

This conservative result is intentional. Earlier generic parsing falsely
reported `in_stock` around \$158--\$163 because a third-party Walmart
Marketplace offer was mistaken for Walmart retail inventory.

`detect_walmart_offer(text)` now attempts seller-aware detection. A
temporary diagnostic was added that prints:

`WALMART_SELLER_DEBUG:`

**Completed locally, pending deployment (2026-09-13):** the same public
Walmart product response was inspected directly. Its primary structured
offer associates `sellerName: Rares Market L.L.C.`, `sellerType:
EXTERNAL`, `wfsEnabled: true`, `availabilityStatus: IN_STOCK`, and a
\$160 price in one product object. This proves the current offer is a
Walmart-fulfilled Marketplace offer, not a Walmart-sold retail offer.

A structured `__NEXT_DATA__` parser and fixture tests now associate
seller, availability, and price within the same offer. The current page
classifies as `marketplace_in_stock` at \$160 from Rares Market. It does
not trigger a Walmart retail in-stock classification. Temporary seller
debug logging was removed. Deploy and verify this result on Railway next.

### Target test

One Piece Card Game: The World's Strongest Warriors Double Pack Set 12\
URL: `https://www.target.com/p/-/A-95290385`\
MSRP: \$11.99\
Priority: high

Recent behavior: HTTP 200; generic parser has reported `unknown` and
later `sold_out`; price can be null. Target needs a retailer-specific
parser before production alerts are trusted.

### Best Buy test

Magic: The Gathering The Hobbit Scene Box, SKU 6678102\
MSRP: \$41.99\
Priority: high

Railway has repeatedly encountered request failures/HTTP errors. Do not
bypass Best Buy protections. Use legitimate public data, respectful
retries/backoff, and `unknown`/`error` when reliable public information
is unavailable.

## 4. Walmart Parser Requirements

Walmart detection must distinguish Walmart as seller from third-party
Marketplace sellers and distinguish seller identity from fulfillment.
"Fulfilled by Walmart" does not mean "sold by Walmart."

Desired states include: - 🟢 Walmart Retail In Stock - 🟠 Marketplace In
Stock - 🔴 Sold Out - ⚪ Unknown - ⚫ Error / Not Found

For a strong Walmart retail alert, seller identity, availability, and
price should be associated with the **same structured offer/product
object**. Do not grab the first dollar amount on the page or associate
unrelated nearby strings. If association is uncertain, return `unknown`.

Remove or gate temporary seller-debug logging after verification.

## 5. Product States

**🟡 Loaded / Not Released:** A legitimate public product record exists
but is not yet purchasable. Evidence may include a public product page
becoming valid, a public SKU/TCIN resolving, public structured product
data, or legitimate public retailer API metadata.

**🟢 In Stock / Live:** The intended retailer offer is confidently
purchasable.

**🟠 Marketplace In Stock:** A third-party seller has inventory. This
should not trigger the same alert as an MSRP retailer restock unless
explicitly enabled.

**🔴 Sold Out:** Product exists but the intended offer is unavailable.

**⚪ Unknown:** Page is reachable but evidence is ambiguous. Prefer this
over a false positive.

**⚫ Error / Not Found:** Request failure, server error, invalid page,
confirmed not found, or other unusable response.

## 6. Monitoring Strategy

The desired high-priority sweep is roughly every 30--60 seconds, but
never blindly request every product every 30 seconds. Stagger checks
across products and retailers.

Priority attention should occur around quarter-hour boundaries: -
`:00` - `:15` - `:30` - `:45`

The user has observed Target drops around 3:00 AM Eastern and Walmart
around 5:00 PM Eastern. Priority windows should increase attention
around these boundaries without generating synchronized request spikes.

Suggested classes: - High: \~30--60 sec - Normal: \~60--120 sec - Low:
\~180--300 sec

Use jitter/staggering and per-retailer pacing.

Adaptive backoff should respond to 403, 429, repeated 5xx, timeouts,
connection failures, and confidently detected block/challenge responses.
Do not hammer a failing retailer.

Notifications should be driven primarily by meaningful state transitions
such as `unknown → loaded`, `loaded → in_stock`, `sold_out → in_stock`,
and `in_stock → sold_out`.

## 7. Compliance / Safety Rules

Allowed: public pages, public HTML/JSON, legitimate public APIs,
caching, staggering, normal HTTP clients, backoff, latency/health
monitoring, and public-state alerts.

Not allowed: CAPTCHA bypass, access-control defeat, private employee
systems, stolen/private API credentials, stealth fingerprinting
specifically to defeat anti-bot enforcement, rotating IPs to evade
blocks, ignoring 403/429 and hammering, or claiming the system is
"undetectable."

## 8. Retailer Health

Continue developing `/api/retailer-health`.

Useful fields: retailer, last success, last failure, latest HTTP status,
latency, consecutive failures, backoff multiplier, pacing state, last
error, monitored product count, and next eligible check.

Potential labels: Healthy, Slowed, Backing Off, Blocked, Error.

Historical failures should recover/decay so old problems do not
permanently throttle a retailer.

## 9. Automatic Product Discovery

Automatic discovery is a major requirement. Manual product entry should
remain available, but the owner should not have to manually seed every
future Pokémon/One Piece/MTG release.

Use verified legitimate public discovery surfaces such as public
category/search pages, public feeds, legitimate public APIs, or publicly
exposed structured product records.

Conceptual pipeline:

`Discover → classify → deduplicate → validate → monitor → alert`

Capture retailer, game, title, URL, retailer product ID, likely product
type, state, reliable price, and seller where relevant.

Do not invent retailer discovery endpoints. Uncertain discoveries can
remain pending.

## 10. Collected Product URLs

### Best Buy

`https://www.bestbuy.com/product/wizards-of-the-coast-magic-the-gathering-the-hobbit-scene-box-1-scene-box-per-order-styles-may-vary/JJ8VP7KQLK/sku/6678102`

### Target

-   `https://www.target.com/p/-/A-1010892071`
-   `https://www.target.com/p/-/A-1010892075`
-   `https://www.target.com/p/-/A-1011407490`
-   `https://www.target.com/p/-/A-1012422107`
-   `https://www.target.com/p/-/A-95290385`

Unresolved Howl URL --- do not guess: `https://howl.link/7mjk4kwrhu56j`

### Walmart

-   `https://www.walmart.com/ip/20180856917`
-   `https://www.walmart.com/ip/20102351151`
-   `https://www.walmart.com/ip/20329455531`
-   `https://www.walmart.com/ip/20149600074`
-   `https://www.walmart.com/ip/19939024731`
-   `https://www.walmart.com/ip/19380764160`
-   `https://www.walmart.com/ip/20140716298`
-   `https://www.walmart.com/ip/13816151308`
-   `https://www.walmart.com/ip/14191660941`
-   `https://www.walmart.com/ip/20243261734`
-   `https://www.walmart.com/ip/14091452016`

Do not bulk-enable these until the corresponding parser is trustworthy.

## 11. MSRP / Markup

Initial frontend maximum-markup default: **200%** during testing.

Users should eventually choose their own threshold. Marketplace price
must remain distinguishable from retailer price; a marketplace offer
below a markup threshold is not automatically a retailer restock.

## 12. Frontend / PWA

The existing PWA predates Railway integration and historically loads
`restocks.json`. Production should consume the Railway `/api/feed`.

Required capabilities: Android-first installable PWA, responsive product
cards, retailer/game/product, status, price, MSRP, markup, last checked,
purchase/open-product action, search, filters, manual/automatic refresh,
notification controls, retailer health, and authenticated admin
controls.

Filters should include game, retailer, online/local, status, max markup,
and text search.

Use a legitimate direct cart URL only when the retailer provides a
stable supported mechanism; otherwise open the official product page.
Never invent cart URLs.

## 13. Add / Manage Products

Add Product should allow pasting a retailer URL, choosing game, optional
MSRP, and priority. Auto-detect retailer, product ID, title, price, and
state when possible.

Manage Products should allow viewing, enabling, disabling, removing,
editing MSRP/priority, and seeing health/last status change.

Once implemented, normal administration should not require manually
editing `sources.json`.

## 14. Admin Security

The PWA may be publicly viewable, but owner/admin controls must be
restricted. The user's fiancée should be able to install, view, and
receive alerts without automatically having admin privileges.

Never expose GitHub tokens, Railway secrets, admin passwords, or VAPID
private keys in public frontend JS or the public repository.

An initial strong admin secret stored in Railway environment variables
and sent as `Authorization: Bearer <secret>` over HTTPS is acceptable.
Frontend may retain it in `sessionStorage`. OAuth/accounts can come
later.

## 15. Persistent Storage

Add/Manage Product and Web Push require persistent backend storage.

Initial options: - SQLite on a mounted Railway Volume - PostgreSQL for
larger scale

Persist products/configuration, monitoring state, last status, relevant
history, push subscriptions, and future user preferences. Do not rely on
ephemeral Railway filesystem state.

## 16. Push Notifications

Major launch requirement: notifications must reach both intended phones
even while the PWA is closed.

Priority alerts: - 🟡 Loaded - 🟢 In Stock

Optional/configurable: Sold Out, Marketplace, price-threshold alerts.

Preferred architecture: service worker + Push API + VAPID + backend
sender + persistent subscription storage. `pywebpush` is a possible
Python implementation.

VAPID private key belongs in Railway environment variables.

Alerts should originate from backend state changes. Do not claim push
works until tested on an installed phone with the app closed.

## 17. Local Inventory

Long-term local focus: - Morgantown, WV - Martinsburg, WV

Discussed Morgantown stores include Kassar's Games, Kahuna's Collection,
and Four Horsemen Comics and Gaming.

Discussed Martinsburg-area stores include Walmart Foxcroft Ave, Walmart
Hammonds Mill Rd, Target Retail Commons Pkwy, GameStop Retail Commons
Pkwy, Thanks For Playing, Your Hobby Place, Mamba Collectibles,
Panhandle Games and Collectibles, and LoneStar Cards in Inwood.

Implement local inventory only where legitimate public data permits it.
Some retailers require selected-store/session/location context. Current
Railway monitoring should not be represented as reliable local inventory
yet.

## 18. Community Features --- Later

After the core radar is stable: - User "Report Restock" for local
stores, with store/city/product/time/quantity and optional photo. -
Reputation/anti-spam mechanisms. - Community chat only after several
months of stable operation and bug fixing.

Do not prioritize chat over accurate restock monitoring.

## 19. Monetization Philosophy

Initial priority is usefulness. The user dislikes services charging
around \$15/month merely for push alerts.

If monetization becomes necessary for hosting, keep it reasonable.
Optional unobtrusive ads may be considered, but never deceptive ads,
moving close buttons, obstructive UI, or intentionally miserable free
tiers.

## 20. Legacy GitHub Checker

`.github/workflows/restock-check.yml` runs the older
`scripts/check_restocks.py` / `restocks.json` flow. Railway is intended
to replace it for rapid monitoring.

Do not disable the legacy scheduled checker until Railway is stable, the
frontend consumes Railway, state changes are reliable, and push works.
Then disable its schedule to avoid duplicate retailer traffic. Manual
dispatch can remain as backup/testing.

## 21. Testing Standard

Do not mark a retailer working because one request returns HTTP 200.

Where possible test: - retailer-owned in stock - retailer-owned sold
out - marketplace-only - loaded/unreleased - invalid/not found -
transient failure - high markup - multiple offers

Verify seller, price, and availability belong to the same offer. Avoid
status flapping and duplicate alerts.

**A false `unknown` is inconvenient. A false `in_stock` destroys trust.
Prefer `unknown` when evidence is ambiguous.**

## 22. Observability

Future improvements: structured logs, product check history, transition
history, retailer latency/error counters, parser version, debug mode,
and admin-only debug endpoints.

Do not permanently dump large retailer HTML or sensitive configuration
into logs. Remove/gate temporary debug statements after use.

## 23. Implementation Order

1.  Inspect `WALMART_SELLER_DEBUG`.
2.  Finish Walmart structured seller/offer parser.
3.  Test Walmart retailer-owned vs marketplace states.
4.  Remove/gate temporary debug logging.
5.  Build/test Target-specific parser.
6.  Investigate Best Buy using legitimate public-data options only.
7.  Improve retailer-health recovery/backoff.
8.  Wire PWA to Railway `/api/feed`.
9.  Add polished states, search, filters, 200% default markup, product
    actions, retailer health.
10. Add persistent storage.
11. Add backend product CRUD and secure owner admin.
12. Add Add Product / Manage Products UI.
13. Implement VAPID Web Push and persistent subscriptions.
14. Test closed-app push on both intended Android phones.
15. Build automatic discovery and Loaded detection.
16. Add legitimate local inventory retailer-by-retailer.
17. Later: community reports, reputation, chat, multi-user preferences,
    sustainable monetization.

## 24. Launch Definition

A meaningful first launch should have: - stable backend - trustworthy
Walmart and Target detection - Best Buy either supported legitimately or
clearly represented as unsupported/error without false positives - PWA
consuming Railway feed - markup/filtering working - reliable state
changes - push notifications reaching both intended phones with the app
closed - owner Add/Manage Product without editing JSON - respectful
retailer backoff

Automatic discovery can immediately follow this milestone if it cannot
safely fit into the first usable build.

## 25. Rules for ChatGPT Work / Development Agents

1.  Read this file first.
2.  Inspect current repo/deployment before editing.
3.  Never assume planned = implemented.
4.  Make small, testable changes.
5.  Run Python syntax checks/tests before deployment.
6.  Do not silently remove features.
7.  Do not bulk-enable unverified products.
8.  Do not weaken retailer backoff for speed.
9.  Do not implement anti-bot bypass/evasion.
10. Never expose secrets in repo/frontend.
11. Prefer `unknown` over false positive.
12. Test retailer parsers across multiple states.
13. Keep this master plan updated.
14. Record significant architecture decisions here.
15. Preserve Android-friendly PWA behavior.
16. Optimize for successful legitimate MSRP purchases, not impressive
    request frequency.

## 26. Immediate Handoff

Start with the currently running backend.

The Walmart Prismatic Evolutions ETB returns HTTP 200 but
`unknown`/`null`, intentionally preventing an expensive third-party
Marketplace offer from being treated as a Walmart retail restock.

Deploy the tested structured Walmart and Target parser changes, then
verify the live Railway feed. Expected Walmart result for item
13816151308 is `marketplace_in_stock`, seller `Rares Market`, price
\$160. Target's public embedded data currently proves product existence
and future street dates, but may defer fulfillment data; ambiguous
availability must remain `loaded` or `unknown`, never a guessed in-stock
result.

After live verification, continue Target fulfillment research through
legitimate public data only, then proceed with frontend and persistence.

## 27. Success Standard

TCG Radar succeeds when it tells users about **real, purchasable
trading-card stock quickly, accurately, and respectfully enough that
they have a fair shot at buying products without paying scalper prices
or expensive alert subscriptions.**

**Accuracy + speed + respectful monitoring.**

## 28. Implementation Log / Architecture Decisions

### 2026-09-13 --- Parser and live-feed integration checkpoint

- Verified production v3.0.0 is online with three configured products.
- Added stable SHA-256 source IDs; Python process-randomized IDs could
  previously change after every restart and undermine transition state.
- Added `parsers.py` with versioned, structured Walmart and Target
  parsers plus seven standard-library unit tests.
- Walmart now distinguishes exact Walmart seller identity from external
  Marketplace sellers and does not treat `wfsEnabled` as seller identity.
- Target recognizes future `street_date` records as Loaded and treats
  missing/deferred fulfillment data conservatively.
- Expanded retailer-health fields and quarter-hour high-priority
  staggering; no synchronized burst is introduced.
- Updated the PWA locally to consume the Railway API when hosted on
  GitHub Pages, refresh every 30 seconds, default to a 200% test markup
  filter, show all product states/sellers/last-check times, and display
  retailer health. These changes remain pending deployment and live QA.
- Persistent storage, secure admin CRUD, VAPID Web Push, discovery, and
  local inventory remain unimplemented. Community reports/chat remain
  intentionally deferred.
- Railway payment is not required for this parser/frontend checkpoint.
  Confirm plan/usage requirements with the user before adding a paid
  volume, database, or other paid service.

### 2026-09-14 — GitHub access and deployment verified

- GitHub app installation now permits repository writes. Published parser/PWA checkpoint; Railway automatically deployed version 3.1.0.
- Corrected Target empty fulfillment handling: missing sections are not proof of sold-out inventory. Missing matching embedded records remain Unknown rather than claiming HTTP Not Found.
- Conflicting Walmart availability signals remain Unknown; an EXTERNAL seller cannot be classified as Walmart retail merely by name.
- Disabled the old notification-permission button and labeled push alerts as pending, because no backend Web Push delivery exists yet.
- Persistent storage, admin CRUD and closed-app Web Push remain launch blockers. No paid resources were provisioned.

### 2026-09-14 — Owner product controls

- Added authenticated owner endpoints for listing, adding, editing, enabling/disabling, and removing products from the PWA. Product inputs are validated; public URLs only; duplicate URLs rejected.
- Owner UI is phone-friendly and stores the entered owner secret only in browser session storage.
- `TCG_RADAR_ADMIN_SECRET` must be set in Railway before these controls can operate. Until then endpoints return a deliberate configuration message rather than allowing unauthenticated changes.
- Current JSON product file is functional for testing but is not durable across Railway redeployments without a persistent mounted volume. Do not claim closed-app alerts or persistent subscriptions are ready yet.
