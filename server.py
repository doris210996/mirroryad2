import os
import re
import json
import time
import datetime
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, send_from_directory, jsonify, request
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Proxy rotation — stop/start VM to get a fresh IP when blocked
# ---------------------------------------------------------------------------
ROTATE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rotate_proxy.sh")
_rotate_lock = threading.Lock()
_last_rotate = 0  # epoch timestamp of last rotation


def rotate_proxy_ip():
    """Rotate the proxy VM's IP. Deduplicates calls within a 120s window."""
    global _last_rotate
    with _rotate_lock:
        if time.time() - _last_rotate < 120:
            print("[Rotate] Skipped — rotated less than 2 min ago")
            return
        _last_rotate = time.time()
    print("[Rotate] Triggering IP rotation...")
    try:
        result = subprocess.run(
            ["bash", ROTATE_SCRIPT],
            capture_output=True, text=True, timeout=180,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"[Rotate] ERROR: {result.stderr}")
        else:
            # Clear all sessions so they reconnect through new proxy
            reset_yad2_session()
            _thread_local.__dict__.clear()
    except Exception as e:
        print(f"[Rotate] Failed: {e}")

# ---------------------------------------------------------------------------
# MongoDB setup (optional — works without it, just no change detection)
# ---------------------------------------------------------------------------
searches_collection = None
try:
    from pymongo import MongoClient
    from bson import ObjectId
    MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/realestate")
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    mongo_client.server_info()
    mongo_db = mongo_client.get_default_database()
    searches_collection = mongo_db["searches"]
    print("[MongoDB] Connected")
except Exception as e:
    print(f"[MongoDB] Not available ({e}), running without persistence")


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Yad2 scraper using curl_cffi — rotates browser fingerprints to avoid blocks
# ---------------------------------------------------------------------------
SOCKS_PROXY = os.environ.get("YAD2_PROXY", "socks5://127.0.0.1:1080")
_BROWSERS = ["safari", "safari_ios", "edge99", "chrome"]
_browser_idx = 0
_yad2_session = None


def _make_session(browser=None):
    """Create a curl_cffi session with proxy and browser fingerprint."""
    global _browser_idx
    if browser is None:
        browser = _BROWSERS[_browser_idx % len(_BROWSERS)]
    s = cffi_requests.Session(impersonate=browser, proxy=SOCKS_PROXY)
    return s


def get_yad2_session():
    global _yad2_session, _browser_idx
    if _yad2_session is None:
        browser = _BROWSERS[_browser_idx % len(_BROWSERS)]
        print(f"[Yad2] Creating session with {browser} fingerprint via proxy")
        _yad2_session = _make_session(browser)
        _yad2_session.get("https://www.yad2.co.il/", timeout=15)
    return _yad2_session


def reset_yad2_session():
    """Drop the current session and rotate to the next browser fingerprint."""
    global _yad2_session, _browser_idx
    _browser_idx += 1
    _yad2_session = None


def fetch_yad2_page(deal_type, city_code, min_price, max_price, page):
    """Fetch a Yad2 search results page and extract data from __NEXT_DATA__."""
    deal_path = "rent" if deal_type == "rent" else "forsale"
    url = (
        f"https://www.yad2.co.il/realestate/{deal_path}"
        f"?city={city_code}&price={min_price}-{max_price}&page={page}"
    )

    session = get_yad2_session()
    resp = session.get(url, timeout=15)

    match = re.search(
        r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text, re.DOTALL
    )
    if not match:
        is_captcha = "ShieldSquare" in resp.text or "captcha" in resp.text.lower()
        reset_yad2_session()
        # Retry with a different browser fingerprint
        session = get_yad2_session()
        resp = session.get(url, timeout=15)
        match = re.search(
            r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text, re.DOTALL
        )
    if not match:
        is_captcha = "ShieldSquare" in resp.text or "captcha" in resp.text.lower()
        reset_yad2_session()
        if is_captcha:
            print("[Yad2] Captcha detected — triggering proxy IP rotation")
            threading.Thread(target=rotate_proxy_ip, daemon=True).start()
        raise RuntimeError("Could not extract data from Yad2 page")

    next_data = json.loads(match.group(1))
    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )

    feed = None
    for q in queries:
        key = q.get("queryKey", [""])[0]
        if "feed" in str(key) and deal_path in str(key):
            feed = q["state"]["data"]
            break

    if not feed:
        raise RuntimeError("No feed data found in Yad2 response")

    return feed


def extract_items(feed):
    """Extract listings from private + agency sections of the feed."""
    items = []
    for section in ("private", "agency"):
        for item in feed.get(section, []):
            addr = item.get("address", {})
            details = item.get("additionalDetails", {})
            house = addr.get("house", {})
            items.append({
                "status": "default",
                "linkToken": item.get("token", ""),
                "estateType": details.get("property", {}).get("text", ""),
                "street": addr.get("street", {}).get("text", ""),
                "neighborhood": addr.get("neighborhood", {}).get("text", ""),
                "settlement": addr.get("city", {}).get("text", ""),
                "rooms": details.get("roomsCount"),
                "floor": house.get("floor"),
                "squareMeters": details.get("squareMeter"),
                "price": item.get("price", ""),
                "updatedAt": item.get("metaData", {}).get("updatedAt", ""),
            })
    return items


MAX_PAGES = 20        # cap to avoid excessive fetching
MAX_WORKERS = 4       # concurrent page fetches
PAGE_DELAY = 0.15     # small stagger between thread launches

# Thread-local storage for per-thread Yad2 sessions
_thread_local = threading.local()
_thread_browser_counter = 0
_thread_browser_lock = threading.Lock()


def _get_thread_session():
    """Return a per-thread curl_cffi session with a rotated browser fingerprint."""
    global _thread_browser_counter
    if not hasattr(_thread_local, "session"):
        with _thread_browser_lock:
            browser = _BROWSERS[_thread_browser_counter % len(_BROWSERS)]
            _thread_browser_counter += 1
        s = _make_session(browser)
        s.get("https://www.yad2.co.il/", timeout=15)
        _thread_local.session = s
    return _thread_local.session


def _fetch_page_direct(deal_type, city_code, min_price, max_price, page):
    """Fetch a single page using the thread's own session. Thread-safe."""
    session = _get_thread_session()
    deal_path = "rent" if deal_type == "rent" else "forsale"
    url = (
        f"https://www.yad2.co.il/realestate/{deal_path}"
        f"?city={city_code}&price={min_price}-{max_price}&page={page}"
    )
    resp = session.get(url, timeout=20)
    match = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not match:
        return None
    next_data = json.loads(match.group(1))
    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    for q in queries:
        key = q.get("queryKey", [""])[0]
        if "feed" in str(key) and deal_path in str(key):
            return q["state"]["data"]
    return None


def _fetch_page_worker(deal_type, city_code, min_price, max_price, page):
    """Worker for concurrent page fetching. Uses thread-local session."""
    try:
        feed = _fetch_page_direct(deal_type, city_code, min_price, max_price, page)
        return page, extract_items(feed) if feed else []
    except Exception as e:
        print(f"[Yad2] Failed to fetch page {page}: {e}")
        return page, []


def fetch_all_yad2_pages(deal_type, city_code, min_price, max_price):
    """Fetch all pages of Yad2 results concurrently. Returns (items, total_pages)."""
    # Page 1 first to discover total_pages (uses main thread session)
    feed = fetch_yad2_page(deal_type, city_code, min_price, max_price, 1)
    total_pages = feed.get("pagination", {}).get("totalPages", 1)
    all_items = extract_items(feed)

    pages_to_fetch = min(total_pages, MAX_PAGES)
    if pages_to_fetch < 2:
        return all_items, total_pages

    remaining = list(range(2, pages_to_fetch + 1))
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for pg in remaining:
            time.sleep(PAGE_DELAY)  # small stagger to avoid burst
            fut = pool.submit(_fetch_page_worker, deal_type, city_code, min_price, max_price, pg)
            futures[fut] = pg

        for fut in as_completed(futures):
            page_num, items = fut.result()
            all_items.extend(items)

    elapsed = round(time.time() - t_start, 1)
    print(f"[Yad2] Fetched {pages_to_fetch}/{total_pages} pages in {elapsed}s, {len(all_items)} raw items")
    return all_items, total_pages


def remove_duplicates(items):
    seen = set()
    result = []
    for item in items:
        token = item["linkToken"]
        if token and token not in seen:
            seen.add(token)
            result.append(item)
    return result


def filter_items(items, min_sqm=None, max_sqm=None, neighborhood=None):
    """Filter items by sqm range and/or neighborhood (substring match)."""
    filtered = []
    for item in items:
        # sqm filter
        sqm = item.get("squareMeters")
        if min_sqm is not None or max_sqm is not None:
            if sqm is None:
                continue
            try:
                sqm_val = float(sqm)
            except (ValueError, TypeError):
                continue
            if min_sqm is not None and sqm_val < min_sqm:
                continue
            if max_sqm is not None and sqm_val > max_sqm:
                continue

        # neighborhood filter (substring match for Hebrew)
        if neighborhood:
            item_nb = item.get("neighborhood") or ""
            if neighborhood not in item_nb:
                continue

        filtered.append(item)
    return filtered


def parse_price(price_str):
    """Extract a numeric price from a string like '₪5,200' or '5200'."""
    if not price_str:
        return None
    cleaned = re.sub(r'[^\d.]', '', str(price_str))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def compute_price_stats(items, cached_avgs=None):
    """Enrich items with pricePerSqm and neighborhood/city averages.

    If cached_avgs is provided (from city-wide sampling), use those for
    neighborhood/city averages. Otherwise fall back to page-level averages.
    Also computes avgPricePerSqmFiltered — the average across all items in
    the current (already-filtered) result set.
    """
    # First pass: compute pricePerSqm for each item
    for item in items:
        price = parse_price(item.get("price"))
        sqm = item.get("squareMeters")
        if price and sqm and float(sqm) > 0:
            item["pricePerSqm"] = round(price / float(sqm))
        else:
            item["pricePerSqm"] = None

    # Filtered average: avg price/sqm across all items in this result set
    filtered_ppsqm = [item["pricePerSqm"] for item in items if item.get("pricePerSqm")]
    filtered_avg = round(sum(filtered_ppsqm) / len(filtered_ppsqm)) if filtered_ppsqm else None

    if cached_avgs:
        # Use city-wide averages from cache
        cached_nb = cached_avgs.get("neighborhood", {})
        cached_city = cached_avgs.get("city")

        # Page-level fallback for neighborhoods not in cache
        page_nb_vals = {}
        for item in items:
            ppsqm = item["pricePerSqm"]
            if ppsqm is None:
                continue
            nb = item.get("neighborhood") or ""
            if nb and nb not in cached_nb:
                page_nb_vals.setdefault(nb, []).append(ppsqm)
        page_nb_avg = {k: round(sum(v) / len(v)) for k, v in page_nb_vals.items()}

        for item in items:
            nb = item.get("neighborhood") or ""
            item["avgPricePerSqmNeighborhood"] = cached_nb.get(nb) or page_nb_avg.get(nb)
            item["avgPricePerSqmCity"] = cached_city
            item["avgPricePerSqmFiltered"] = filtered_avg
            item["statsScope"] = "city-wide"
    else:
        # Page-level averages only
        neighborhood_vals = {}
        city_vals = {}
        for item in items:
            ppsqm = item["pricePerSqm"]
            if ppsqm is None:
                continue
            nb = item.get("neighborhood") or ""
            city = item.get("settlement") or ""
            if nb:
                neighborhood_vals.setdefault(nb, []).append(ppsqm)
            if city:
                city_vals.setdefault(city, []).append(ppsqm)

        neighborhood_avg = {k: round(sum(v) / len(v)) for k, v in neighborhood_vals.items()}
        city_avg = {k: round(sum(v) / len(v)) for k, v in city_vals.items()}

        for item in items:
            nb = item.get("neighborhood") or ""
            city = item.get("settlement") or ""
            item["avgPricePerSqmNeighborhood"] = neighborhood_avg.get(nb)
            item["avgPricePerSqmCity"] = city_avg.get(city)
            item["avgPricePerSqmFiltered"] = filtered_avg
            item["statsScope"] = "all results"

    return items


def detect_changes(old_items, new_items):
    old_map = {item["linkToken"]: item for item in old_items}
    unchangeable = {"linkToken", "status", "estateType", "settlement", "neighborhood", "street"}
    result = []
    for new_item in new_items:
        token = new_item["linkToken"]
        if token not in old_map:
            result.append({**new_item, "status": "new"})
            continue
        old_item = old_map[token]
        updated = False
        for key in new_item:
            if key in unchangeable:
                continue
            if new_item.get(key) != old_item.get(key):
                updated = True
                break
        result.append({**new_item, "status": "updated" if updated else "default"})
    return result


CITY_CODES = {
    "תל אביב": "5000", "ירושלים": "3000", "חיפה": "4000",
    "באר שבע": "7900", "ראשון לציון": "8300", "פתח תקווה": "7900",
    "אשדוד": "70", "נתניה": "7400", "חולון": "6600",
    "בני ברק": "6200", "רמת גן": "8600", "אשקלון": "7100",
    "בת ים": "6100", "הרצליה": "6400", "כפר סבא": "6900",
    "רעננה": "8700", "מודיעין": "1200", "לוד": "7000",
    "רמלה": "8500", "נהריה": "1740", "עכו": "4100",
    "גבעתיים": "6300", "הוד השרון": "9800", "רחובות": "8400",
    "קריית אתא": "1020", "עפולה": "7700", "טבריה": "8200",
    "אילת": "2600", "קריית גת": "7200", "נצרת": "7500",
    "כרמיאל": "1139", "רמת השרון": "2650", "יבנה": "2660",
}


def resolve_city_code(settlement):
    """Return a city code — accepts codes directly or known city names."""
    if settlement.isdigit():
        return settlement
    return CITY_CODES.get(settlement)


# ---------------------------------------------------------------------------
# City-wide price/sqm averages — background sampling + monthly cache
# ---------------------------------------------------------------------------
_avg_cache = {}       # key: (deal_type, city_code, "YYYY-MM") → {"neighborhood": {name: avg}, "city": avg}
_avg_building = set() # tracks in-progress builds to avoid duplicate threads
_avg_lock = threading.Lock()


def _fetch_yad2_page_standalone(session, deal_type, city_code, min_price, max_price, page):
    """Like fetch_yad2_page but uses a provided session (thread-safe)."""
    deal_path = "rent" if deal_type == "rent" else "forsale"
    url = (
        f"https://www.yad2.co.il/realestate/{deal_path}"
        f"?city={city_code}&price={min_price}-{max_price}&page={page}"
    )
    resp = session.get(url, timeout=20)
    match = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not extract data from Yad2 page")
    next_data = json.loads(match.group(1))
    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    feed = None
    for q in queries:
        key = q.get("queryKey", [""])[0]
        if "feed" in str(key) and deal_path in str(key):
            feed = q["state"]["data"]
            break
    if not feed:
        raise RuntimeError("No feed data found in Yad2 response")
    return feed


def _build_area_averages(deal_type, city_code):
    """Fetch a sample of pages across ALL listings for this city/deal_type and compute averages."""
    cache_key = (deal_type, city_code, datetime.datetime.now().strftime("%Y-%m"))
    try:
        # Use a dedicated session for the background thread with rotated fingerprint
        import random
        browser = random.choice(_BROWSERS)
        session = _make_session(browser)
        session.get("https://www.yad2.co.il/", timeout=15)

        # Page 1 with full price range to get total_pages
        feed = _fetch_yad2_page_standalone(session, deal_type, city_code, 0, 100000000, 1)
        total_pages = feed.get("pagination", {}).get("totalPages", 1)

        all_items = extract_items(feed)

        # Pick up to 5 evenly-spaced pages (page 1 already fetched)
        if total_pages <= 5:
            pages_to_fetch = [p for p in range(2, total_pages + 1)]
        else:
            step = total_pages / 5
            sample_pages = [max(1, int(round(step * i))) for i in range(1, 5)]
            # Deduplicate and exclude page 1 (already fetched)
            pages_to_fetch = sorted(set(p for p in sample_pages if p > 1))

        for pg in pages_to_fetch:
            try:
                pg_feed = _fetch_yad2_page_standalone(session, deal_type, city_code, 0, 100000000, pg)
                all_items.extend(extract_items(pg_feed))
            except Exception:
                continue  # skip pages that fail

        # Compute pricePerSqm for sampled items
        neighborhood_vals = {}
        city_vals = []
        for item in all_items:
            price = parse_price(item.get("price"))
            sqm = item.get("squareMeters")
            if not price or not sqm or float(sqm) <= 0:
                continue
            ppsqm = price / float(sqm)
            city_vals.append(ppsqm)
            nb = item.get("neighborhood") or ""
            if nb:
                neighborhood_vals.setdefault(nb, []).append(ppsqm)

        neighborhood_avg = {k: round(sum(v) / len(v)) for k, v in neighborhood_vals.items()}
        city_avg = round(sum(city_vals) / len(city_vals)) if city_vals else None

        with _avg_lock:
            _avg_cache[cache_key] = {"neighborhood": neighborhood_avg, "city": city_avg}

        print(f"[AvgCache] Built averages for {deal_type}/{city_code}: "
              f"{len(city_vals)} samples, {len(neighborhood_avg)} neighborhoods, "
              f"{len(pages_to_fetch)+1} pages sampled")

    except Exception as e:
        print(f"[AvgCache] Failed to build averages for {deal_type}/{city_code}: {e}")
    finally:
        with _avg_lock:
            _avg_building.discard(cache_key)


def _get_or_start_avg_cache(deal_type, city_code):
    """Return cached averages if available, else start a background build and return None."""
    cache_key = (deal_type, city_code, datetime.datetime.now().strftime("%Y-%m"))
    with _avg_lock:
        if cache_key in _avg_cache:
            return _avg_cache[cache_key]
        if cache_key not in _avg_building:
            _avg_building.add(cache_key)
            t = threading.Thread(target=_build_area_averages, args=(deal_type, city_code), daemon=True)
            t.start()
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/cities")
def api_cities():
    return jsonify(sorted(CITY_CODES.keys()))


@app.route("/api/neighborhoods")
def api_neighborhoods():
    """Fetch page 1 for a city and return unique neighborhood names."""
    settlement = request.args.get("city", "")
    deal_type = request.args.get("dealType", "rent")
    city_code = resolve_city_code(settlement)
    if not city_code:
        return jsonify([])
    try:
        feed = fetch_yad2_page(deal_type, city_code, "0", "100000000", 1)
        items = extract_items(feed)
        neighborhoods = sorted(set(
            it.get("neighborhood", "") for it in items if it.get("neighborhood")
        ))
        return jsonify(neighborhoods)
    except Exception:
        return jsonify([])


@app.route("/api/realestate/search")
def realestate_search():
    deal_type = request.args.get("dealType", "rent")
    settlement = request.args.get("settlement", "")
    min_price = request.args.get("minPrice", "0")
    max_price = request.args.get("maxPrice", "100000000")
    min_sqm_str = request.args.get("minSqm", "")
    max_sqm_str = request.args.get("maxSqm", "")
    neighborhood = request.args.get("neighborhood", "").strip()

    if not settlement:
        return jsonify({"error": "settlement is required"}), 400

    try:
        min_price = int(min_price)
        max_price = int(max_price)
        min_sqm = int(min_sqm_str) if min_sqm_str else None
        max_sqm = int(max_sqm_str) if max_sqm_str else None
    except ValueError:
        return jsonify({"error": "Invalid numeric parameter"}), 400

    city_code = resolve_city_code(settlement)
    if not city_code:
        return jsonify({"error": f"Settlement '{settlement}' not found. Use a city code or one of: {', '.join(sorted(CITY_CODES.keys()))}"}), 404

    t_start = time.time()
    try:
        items, total_pages = fetch_all_yad2_pages(deal_type, city_code, min_price, max_price)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data from Yad2: {e}"}), 502

    raw_count = len(items)
    items = remove_duplicates(items)
    items = filter_items(items, min_sqm=min_sqm, max_sqm=max_sqm, neighborhood=neighborhood)

    cached_avgs = _get_or_start_avg_cache(deal_type, city_code)
    items = compute_price_stats(items, cached_avgs)
    fetch_time = round(time.time() - t_start, 1)

    # Count items that contributed to the filtered average
    avg_sample_count = sum(1 for it in items if it.get("pricePerSqm"))

    # Collect unique neighborhoods for autocomplete
    neighborhoods = sorted(set(
        it.get("neighborhood", "") for it in items if it.get("neighborhood")
    ))

    search_id = ""
    if searches_collection is not None:
        try:
            doc = {
                "items": items,
                "search_params": {
                    "dealType": deal_type,
                    "settlement": settlement,
                    "cityCode": city_code,
                    "minPrice": min_price,
                    "maxPrice": max_price,
                    "minSqm": min_sqm,
                    "maxSqm": max_sqm,
                    "neighborhood": neighborhood,
                },
                "total_pages": total_pages,
                "last_updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            result = searches_collection.insert_one(doc)
            search_id = str(result.inserted_id)
        except Exception:
            pass

    stats_scope = "city-wide" if cached_avgs else "all results"
    return jsonify({
        "search_id": search_id,
        "items": items,
        "total_pages": total_pages,
        "statsScope": stats_scope,
        "fetchTime": fetch_time,
        "avgSampleCount": avg_sample_count,
        "neighborhoods": neighborhoods,
    })


@app.route("/api/realestate/update")
def realestate_update():
    search_id = request.args.get("searchId", "")
    if not search_id:
        return jsonify({"error": "searchId is required"}), 400

    if searches_collection is None:
        return jsonify({"error": "MongoDB not available, cannot track updates"}), 503

    try:
        doc = searches_collection.find_one({"_id": ObjectId(search_id)})
    except Exception:
        return jsonify({"error": "Invalid searchId"}), 400

    if not doc:
        return jsonify({"error": "Search not found"}), 404

    params = doc["search_params"]

    try:
        new_items, total_pages = fetch_all_yad2_pages(
            params["dealType"], params["cityCode"],
            params["minPrice"], params["maxPrice"],
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data from Yad2: {e}"}), 502

    new_items = remove_duplicates(new_items)
    new_items = filter_items(
        new_items,
        min_sqm=params.get("minSqm"),
        max_sqm=params.get("maxSqm"),
        neighborhood=params.get("neighborhood", ""),
    )
    changed_items = detect_changes(doc["items"], new_items)

    cached_avgs = _get_or_start_avg_cache(params["dealType"], params["cityCode"])
    changed_items = compute_price_stats(changed_items, cached_avgs)

    try:
        searches_collection.update_one(
            {"_id": ObjectId(search_id)},
            {"$set": {
                "items": changed_items,
                "total_pages": total_pages,
                "last_updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }},
        )
    except Exception:
        pass

    stats_scope = "city-wide" if cached_avgs else "all results"
    return jsonify({
        "items": changed_items,
        "search_params": params,
        "total_pages": total_pages,
        "statsScope": stats_scope,
    })


@app.route("/api/realestate/cancel", methods=["DELETE"])
def realestate_cancel():
    search_id = request.args.get("searchId", "")
    if not search_id:
        return jsonify({"error": "searchId is required"}), 400

    if searches_collection is None:
        return "", 204

    try:
        from bson import ObjectId as ObjId
        searches_collection.delete_one({"_id": ObjId(search_id)})
    except Exception:
        pass

    return "", 204


@app.route("/api/rotate", methods=["POST"])
def manual_rotate():
    """Manually trigger proxy IP rotation."""
    threading.Thread(target=rotate_proxy_ip, daemon=True).start()
    return jsonify({"status": "rotation started"})


# ---------------------------------------------------------------------------
# Periodic proxy rotation (every ROTATE_INTERVAL_MIN minutes)
# ---------------------------------------------------------------------------
ROTATE_INTERVAL_MIN = int(os.environ.get("ROTATE_INTERVAL", "30"))


def _periodic_rotation():
    """Background loop that rotates the proxy IP periodically."""
    while True:
        time.sleep(ROTATE_INTERVAL_MIN * 60)
        print(f"[Rotate] Periodic rotation (every {ROTATE_INTERVAL_MIN} min)")
        rotate_proxy_ip()


if __name__ == "__main__":
    t = threading.Thread(target=_periodic_rotation, daemon=True)
    t.start()
    print(f"[Rotate] Periodic rotation enabled every {ROTATE_INTERVAL_MIN} min")
    app.run(host="0.0.0.0", port=8181)
