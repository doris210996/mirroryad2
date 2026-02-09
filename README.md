# Yad 1.5 — Smart Filtered Real Estate

A real-estate search dashboard that aggregates listings from multiple sources (Yad2 + Facebook groups) with advanced filtering, price analytics, and automated proxy rotation to avoid bot detection.

## Architecture

```
Browser  -->  Flask (port 8181)  -->  SSH SOCKS tunnel (port 1080)  -->  GCE VM  -->  Yad2
                  |
                  +-- rotate_proxy.sh (stop/start VM for fresh IP)
```

### Stack

- **Frontend:** Single-page HTML (`index.html`) with Inter font, orange-themed dark UI
- **Backend:** Flask + `curl_cffi` (Chrome/Safari TLS fingerprint impersonation)
- **Proxy:** GCE `e2-micro` VM with SSH SOCKS5 tunnel
- **Database:** MongoDB (optional — graceful fallback without it)

### Files

| File | Purpose |
|------|---------|
| `server.py` | Flask app, multi-source aggregation, price stats, proxy rotation |
| `facebook_parser.py` | Facebook post parser — extracts structured data from unstructured Hebrew text |
| `index.html` | UI — search form, listing cards, source badges, particle background |
| `rotate_proxy.sh` | Shell script to rotate the proxy VM's IP |

---

## Multi-Source Aggregation

The app now aggregates listings from multiple sources in parallel:

### Sources

1. **Yad2** — Israel's largest real estate platform (structured data)
2. **Facebook Groups** — Community groups with direct landlord listings (unstructured text)

### Parallel Fetching

Both sources are fetched concurrently using `ThreadPoolExecutor` with 2 source-level workers:

```
/api/realestate/search
  ├─→ fetch_yad2_source() [4 concurrent page workers]
  └─→ fetch_facebook_source() [sequential HTTP scraping]

Results merged → Unified filtering → Combined dataset
```

**Performance:**
- Yad2 alone: ~10-15s (20 pages, concurrent)
- Facebook alone: ~5-10s (5 pages, HTTP)
- **Parallel execution: ~15-20s** (max of both + merge)

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
- Errors are included in response: `{"errors": {"facebook": "Connection timeout"}}`
- UI displays error messages in info bar

---

## Search & Filtering

### How a search works

1. User submits filters: deal type, city, neighborhood, price range, sqm range
2. Server fetches **all pages** from Yad2 concurrently (4 workers, capped at 20 pages)
3. Results are deduplicated, then filtered server-side by neighborhood and sqm range
4. Price stats are computed and attached to each listing
5. JSON response returned to the browser

### Available filters

| Filter | Type | Behavior |
|--------|------|----------|
| Deal type | `rent` / `forsale` | Passed to Yad2 URL |
| City | Select dropdown | Mapped to Yad2 city code via `CITY_CODES` dict |
| Neighborhood | Text input | Substring match against listing's neighborhood field (Hebrew) |
| Min/Max price | Number | Passed to Yad2 URL (server-side filtering by Yad2) |
| Min/Max sqm | Number | Post-fetch filtering in `filter_items()` |

### Concurrent page fetching

`fetch_all_yad2_pages()` fetches page 1 to discover `totalPages`, then dispatches remaining pages (up to `MAX_PAGES=20`) across a `ThreadPoolExecutor` with `MAX_WORKERS=4`. Each worker thread uses its own `curl_cffi` session (thread-local) with a rotated browser fingerprint.

A `PAGE_DELAY=0.15s` stagger between thread launches prevents burst traffic.

**Typical performance:**

| City size | Pages | Time |
|-----------|-------|------|
| Small (Eilat) | ~8 | ~10s |
| Large (Tel Aviv) | 20 (capped) | ~15s |

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

## Anti-Bot & Proxy System

Yad2 uses Radware Bot Manager (ShieldSquare) which blocks scrapers via:
- TLS fingerprinting (solved by `curl_cffi` impersonation)
- IP-based rate limiting / captcha (solved by proxy rotation)

### Browser fingerprint rotation

Sessions cycle through `["safari", "safari_ios", "edge99", "chrome"]`. The main session and each worker thread get different fingerprints via a rotating counter.

### GCE VM proxy

All Yad2 traffic routes through a SOCKS5 tunnel to a GCE VM (`yad2-proxy`, `e2-micro`, `us-central1-a`). The VM's ephemeral IP changes on each stop/start cycle.

**Setup:**

```bash
# Create VM (one-time)
gcloud compute instances create yad2-proxy \
  --machine-type=e2-micro --zone=us-central1-a \
  --image-family=debian-12 --image-project=debian-cloud

# Create SSH SOCKS tunnel
gcloud compute ssh yad2-proxy --zone=us-central1-a \
  --ssh-flag="-D 1080 -f -N -4"
```

### IP rotation (`rotate_proxy.sh`)

The rotation script:
1. Kills the existing SSH tunnel
2. Stops the VM (releases the IP)
3. Starts the VM (gets a new ephemeral IP)
4. Waits for SSH readiness
5. Re-establishes the SOCKS tunnel
6. Verifies connectivity via httpbin.org

Takes ~60-90 seconds per rotation.

### Three rotation triggers

| Trigger | How it works |
|---------|-------------|
| **Periodic** | Background thread rotates every `ROTATE_INTERVAL` minutes (default: 30). Configurable via env var. |
| **Reactive** | When `fetch_yad2_page()` detects a ShieldSquare captcha, it triggers rotation automatically. A 120-second cooldown prevents rapid re-triggers. |
| **Manual** | `POST /api/rotate` triggers immediate rotation. |

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `YAD2_PROXY` | `socks5://127.0.0.1:1080` | SOCKS proxy address for Yad2 |
| `ROTATE_INTERVAL` | `30` | Minutes between periodic IP rotations |
| `FB_GROUP_ID` | `457465901082882` | Facebook group ID to scrape |
| `FB_MAX_PAGES` | `5` | Maximum pages to fetch from Facebook (~10 posts per page) |
| `FB_TIMEOUT` | `60` | Timeout in seconds for Facebook scraping |
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
| `fbGroupId` | no | Facebook group ID (overrides `FB_GROUP_ID` env var) |

Returns: `{ search_id, items[], total_pages, sources: {yad2, facebook}, errors: {}, fetchTime, avgSampleCount }`

### `GET /api/realestate/update`

| Param | Required | Description |
|-------|----------|-------------|
| `searchId` | yes | ID from a previous search (requires MongoDB) |

Re-fetches all pages, applies same filters, detects new/updated listings.

### `DELETE /api/realestate/cancel`

| Param | Required | Description |
|-------|----------|-------------|
| `searchId` | yes | ID to cancel |

### `POST /api/rotate`

Triggers manual proxy IP rotation. Returns `{ status: "rotation started" }`.

---

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server (proxy tunnel must be active for Yad2)
python server.py

# With custom Facebook group
FB_GROUP_ID=123456789 python server.py

# With custom rotation interval (minutes)
ROTATE_INTERVAL=15 python server.py

# Without proxy (direct connection to Yad2)
YAD2_PROXY="" python server.py
```

Server runs on port `8181`.

### Requirements

- Python 3.8+
- Dependencies: `flask`, `pymongo`, `curl_cffi`, `facebook-scraper`
- Optional: MongoDB for change tracking
- Optional: SSH tunnel to GCE VM for Yad2 proxy rotation
