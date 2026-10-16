# Yad 1.5 — Smart Filtered Real Estate

A real-estate search dashboard that aggregates listings from Yad2 and manually imported Facebook posts, with advanced filtering and price analytics. All automated fetching goes through a **polite, compliant fetcher**: it follows robots.txt, paces its requests, identifies itself honestly, and never tries to get around bot protection.

## Architecture

```
Browser  -->  Flask (port 8181)  -->  PoliteFetcher  -->  Yad2
                  |                    (robots.txt, rate limit, cache,
                  |                     backoff, circuit breaker)
                  +-- fb_imports.json  (Facebook posts pasted by the user)
```

### Stack

- **Frontend:** Single-page HTML (`index.html`) with Inter font, orange-themed dark UI
- **Backend:** Flask + `requests` via `polite_fetcher.py`
- **Database:** MongoDB (optional — graceful fallback without it)

### Files

| File | Purpose |
|------|---------|
| `server.py` | Flask app, multi-source aggregation, price stats |
| `polite_fetcher.py` | Compliant HTTP client: robots.txt (RFC 9309), per-host rate limit, cache, Retry-After, circuit breaker |
| `facebook_imports.py` | Stores Facebook posts the user pastes in (parsed by `facebook_parser.py`) |
| `facebook_parser.py` | Facebook post parser — extracts structured data from unstructured Hebrew text |
| `index.html` | UI — search form, Facebook import panel, listing cards, source badges |
| `tests/` | Unit tests (`python -m unittest discover -s tests`) |

---

## Multi-Source Aggregation

### Sources

1. **Yad2** — Israel's largest real estate platform (structured data), fetched politely
2. **Facebook** — posts the user copies from a group and pastes into the "ייבוא פוסט מפייסבוק" panel. Automated Facebook scraping (with logged-in session cookies) was removed because it breaks Meta's Terms of Service.

```
/api/realestate/search
  ├─→ fetch_all_yad2_pages()   [sequential, paced by PoliteFetcher]
  └─→ get_facebook_items()     [imported posts matching the city]

Results merged → Unified filtering (price, sqm, neighborhood) → Combined dataset
```

### Facebook Parser

`facebook_parser.py` extracts structured fields from unstructured Hebrew text using regex patterns:

| Field | Pattern Example | Notes |
|-------|----------------|-------|
| **Price** | `(\d{1,3}(,\d{3})*)\s*₪` | Required — posts without price are filtered |
| **Rooms** | `(\d+\.?\d*)\s*חדרים` | Optional |
| **Square meters** | `(\d+)\s*מ"ר` | Optional |
| **City** | Match against `CITY_CODES` | Optional |
| **Broker** | Keywords: "תיווך", "משרד נדל״ן" | Boolean flag |

**Data quality:** ~30-40% of Facebook posts may lack complete data. Only posts with prices are included.

### Unified Schema

All items include a `source` field (`"yad2"` or `"facebook"`) for attribution:

```json
{
  "source": "facebook",
  "linkToken": "post_12345",
  "price": "5,200 ₪",
  "rooms": "3 חדרים",
  "sqm": "80 מ״ר",
  "city": "תל אביב",
  "broker": false
}
```

### Error Handling

- If one source fails, return results from the other
- Errors are user-facing Hebrew messages: `{"errors": {"yad2": "Yad2 חסם גישה אוטומטית — ..."}}`
- UI displays the messages in the info bar

---

## Search & Filtering

### How a search works

1. User submits filters: deal type, city, neighborhood, price range, sqm range
2. Server fetches Yad2 result pages one at a time through the polite fetcher (capped at `YAD2_MAX_PAGES`, default 10); repeat searches within 10 minutes are served from cache
3. Results are deduplicated, then filtered server-side by price, sqm range and neighborhood
4. Price stats are computed and attached to each listing
5. JSON response returned to the browser

### Available filters

| Filter | Type | Behavior |
|--------|------|----------|
| Deal type | `rent` / `forsale` | Passed to Yad2 URL |
| City | Select dropdown | Mapped to Yad2 city code via `CITY_CODES` dict |
| Neighborhood | Text input | Yad2 neighborhood ID in the URL; substring match on neighborhood (or post text for Facebook) |
| Min/Max price | Number | Filtered locally in `filter_items()` — Yad2's robots.txt disallows the `price=` URL parameter |
| Min/Max sqm | Number | Post-fetch filtering in `filter_items()` |

---

## Price Statistics

### Per-listing: `pricePerSqm`

```
pricePerSqm = round(price / squareMeters)
```

Listings missing either field get `pricePerSqm: null`.

### `avgPricePerSqmFiltered`

Average price/sqm across **all listings in the current filtered result set**. If you filter to 60-100sqm in neighborhood "X", this average reflects exactly that subset. Useful for comparing any individual listing against peers matching the same criteria.

### `avgPricePerSqmNeighborhood`

Average price/sqm for the listing's neighborhood. Source depends on cache state (see below).

### `avgPricePerSqmCity`

Average price/sqm across all sampled listings in the city.

### City-wide sampling (background cache)

On first search for a city, a background thread samples up to 5 evenly-spaced pages with the full price range (`0-100M`) to build neighborhood and city averages. These are cached monthly (`(deal_type, city_code, YYYY-MM)` key).

- **Cache miss (first search):** Stats computed from all fetched pages ("all results" scope)
- **Cache hit (subsequent):** City-wide averages used ("city-wide" scope)
- Neighborhoods not in the cache fall back to current-result averages

### Color indicator

- **Green** — listing's price/sqm <= area average (good deal)
- **Red** — listing's price/sqm > area average

Comparison uses neighborhood avg if available, else city avg.

---

## Compliant Fetching (`polite_fetcher.py`)

Earlier versions evaded Yad2's Radware Bot Manager with spoofed browser TLS fingerprints, a proxy VM whose IP was rotated whenever a captcha appeared, and Facebook session cookies. All of that is gone. The fetcher now does the opposite: it works **with** the site's rules.

| Behavior | Details |
|----------|---------|
| **Honest identity** | `User-Agent: MirrorYad2Bot/2.0 (+<info url>; <contact>)`. No browser impersonation, no proxy rotation, no reused login cookies. |
| **robots.txt** | Full RFC 9309 matching (`*` and `$` wildcards, longest match wins, specific user-agent group over `*`). Cached ≤ 24h. A missing robots.txt (4xx) allows all; an unreachable one (5xx / network) disallows all. Disallowed URLs are never requested. |
| **Rate limiting** | One request per host every `FETCHER_MIN_INTERVAL` seconds (default 2), or the site's `Crawl-delay` if larger. Shared across threads. |
| **Caching** | Successful responses are reused for `FETCHER_CACHE_TTL` seconds (default 600), so refreshing a search doesn't hit the site again. |
| **Backoff** | HTTP 429/503 honour `Retry-After` (bounded retries, exponential backoff with jitter). A `Retry-After` longer than 60s pauses the host instead. |
| **Circuit breaker** | HTTP 401/403 or a bot-challenge/captcha page means "no": the host is paused for `FETCHER_BLOCK_COOLDOWN` seconds (default 1800) and the UI shows why. It is never worked around. |

`GET /api/fetcher/status` shows the live state per host (requests, cache hits, robots refusals, denials, block countdown).

> **Legal note:** being polite is necessary but not sufficient. Yad2's Terms of Use govern automated access to its content regardless of robots.txt. For production use, get written permission or a data agreement from Yad2. In practice Yad2's bot protection may challenge an honest bot — especially from cloud/datacenter IPs — and this app will then show the block instead of results.

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `FETCHER_CONTACT` | `` | Contact (e-mail) included in the User-Agent — set this |
| `FETCHER_INFO_URL` | repo URL | Info URL included in the User-Agent |
| `FETCHER_USER_AGENT` | — | Override the whole User-Agent |
| `FETCHER_MIN_INTERVAL` | `2.0` | Seconds between requests to the same host |
| `FETCHER_CACHE_TTL` | `600` | Seconds to cache successful responses |
| `FETCHER_BLOCK_COOLDOWN` | `1800` | Seconds to pause a host after it refuses access |
| `YAD2_MAX_PAGES` | `10` | Max Yad2 result pages per search |
| `FB_IMPORTS_FILE` | `fb_imports.json` | Where imported Facebook posts are stored |
| `FB_MOCK` | `` | `1` adds sample Facebook posts for local testing |
| `MONGO_URI` | `mongodb://localhost:27017/realestate` | MongoDB connection string |
| `LLM_SERVICE_URL` | `http://127.0.0.1:8282` | LLM service URL for sentiment analysis |
| `GOVMAP_API_TOKEN` | `` | GovMap API token for deep analysis real data (optional) |

---

## UI Design

### Theme

- **Background:** `#111827` (dark slate)
- **Cards:** `rgba(255,255,255,0.04)` with orange-tinted borders
- **Primary accent:** Orange gradient `#FF6B00` to `#FF9500`
- **Text:** `#f9fafb` primary, `#9ca3af` muted
- **Good deal:** `#10b981` green
- **Bad deal:** `#ef4444` red

### Layout

1. **Sticky header** — "Yad 1.5" in orange gradient text, thin orange border
2. **Search panel** — Two-row form: [deal type, city, neighborhood] + [price range, sqm range, search button]. Auto-refresh toggle + cancel below.
3. **Results** — Scrollable listing cards with two-column layout (info left, price right). Stats bar at bottom of each card. Hover: left orange border + elevation.
4. **Particle background** — Orange/amber particles with connecting lines

### Key UI features

- Google Font: Inter
- Form labels above each input
- Orange focus rings on inputs
- Loading spinner instead of plain text
- New/Updated badges as colored pills
- Orange custom scrollbar
- Responsive: stacks vertically on mobile

---

## API Endpoints

### `GET /api/realestate/search`

| Param | Required | Description |
|-------|----------|-------------|
| `dealType` | yes | `rent` or `forsale` |
| `settlement` | yes | City name (Hebrew) or city code |
| `minPrice` | no | Default: `0` |
| `maxPrice` | no | Default: `100000000` |
| `minSqm` | no | Filter by minimum square meters |
| `maxSqm` | no | Filter by maximum square meters |
| `neighborhood` | no | Substring match against neighborhood name |
| `sources` | no | Comma-separated: `yad2`, `facebook` (default both) |

Returns: `{ search_id, items[], total_pages, pages_fetched, sources: {yad2, facebook}, errors: {}, fetchTime, avgSampleCount }`

### `GET /api/realestate/update`

| Param | Required | Description |
|-------|----------|-------------|
| `searchId` | yes | ID from a previous search (requires MongoDB) |

Re-fetches all pages, applies same filters, detects new/updated listings.

### `DELETE /api/realestate/cancel`

| Param | Required | Description |
|-------|----------|-------------|
| `searchId` | yes | ID to cancel |

### `POST /api/facebook/imports`

Body: `{ "text": "<post text>", "url": "<facebook post link, optional>" }`. Returns the parsed listing (201), or 422 with a Hebrew message if no listing/price was found.

### `GET /api/facebook/imports` · `DELETE /api/facebook/imports/<linkToken>`

List or remove imported posts.

### `GET /api/fetcher/status`

Fetcher state per host: request count, cache hits, robots refusals, denials and block countdown.

---

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server (set a contact so site operators can reach you)
FETCHER_CONTACT=you@example.com python server.py

# Slower pacing / longer cache
FETCHER_MIN_INTERVAL=5 FETCHER_CACHE_TTL=1800 python server.py

# Run the tests
python -m unittest discover -s tests
```

Server runs on port `8181`.

### Requirements

- Python 3.8+
- Dependencies: `flask`, `pymongo`, `requests`
- Optional: MongoDB for change tracking
