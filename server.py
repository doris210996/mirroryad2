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
_BROWSERS = ["edge99", "safari", "safari_ios", "chrome"]
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


def fetch_yad2_page(deal_type, city_code, min_price, max_price, page, neighborhood_id=None):
    """Fetch a Yad2 search results page and extract data from __NEXT_DATA__."""
    deal_path = "rent" if deal_type == "rent" else "forsale"
    url = (
        f"https://www.yad2.co.il/realestate/{deal_path}"
        f"?city={city_code}&price={min_price}-{max_price}&page={page}"
    )
    if neighborhood_id:
        url += f"&neighborhood={neighborhood_id}"

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
            customer = item.get("customer", {})
            ad_type = item.get("adType", section)  # "private" or "commercial"
            agency_name = customer.get("agencyName", "")
            is_broker = ad_type == "commercial" or bool(agency_name)
            tags = [t.get("text", t) if isinstance(t, dict) else str(t)
                    for t in item.get("tags", [])]
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
                "adType": ad_type,
                "isBroker": is_broker,
                "agencyName": agency_name,
                "tags": tags,
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


def _fetch_page_direct(deal_type, city_code, min_price, max_price, page, neighborhood_id=None):
    """Fetch a single page using the thread's own session. Thread-safe."""
    session = _get_thread_session()
    deal_path = "rent" if deal_type == "rent" else "forsale"
    url = (
        f"https://www.yad2.co.il/realestate/{deal_path}"
        f"?city={city_code}&price={min_price}-{max_price}&page={page}"
    )
    if neighborhood_id:
        url += f"&neighborhood={neighborhood_id}"
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


def _fetch_page_worker(deal_type, city_code, min_price, max_price, page, neighborhood_id=None):
    """Worker for concurrent page fetching. Uses thread-local session."""
    try:
        feed = _fetch_page_direct(deal_type, city_code, min_price, max_price, page, neighborhood_id)
        return page, extract_items(feed) if feed else []
    except Exception as e:
        print(f"[Yad2] Failed to fetch page {page}: {e}")
        return page, []


def fetch_all_yad2_pages(deal_type, city_code, min_price, max_price, neighborhood_id=None):
    """Fetch all pages of Yad2 results concurrently. Returns (items, total_pages)."""
    # Page 1 first to discover total_pages (uses main thread session)
    feed = fetch_yad2_page(deal_type, city_code, min_price, max_price, 1, neighborhood_id)
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
            fut = pool.submit(_fetch_page_worker, deal_type, city_code, min_price, max_price, pg, neighborhood_id)
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


def compute_price_stats(items):
    """Enrich items with pricePerSqm, neighborhood avg, and filtered avg."""
    for item in items:
        price = parse_price(item.get("price"))
        sqm = item.get("squareMeters")
        if price and sqm and float(sqm) > 0:
            item["pricePerSqm"] = round(price / float(sqm))
        else:
            item["pricePerSqm"] = None

    filtered_ppsqm = [item["pricePerSqm"] for item in items if item.get("pricePerSqm")]
    filtered_avg = round(sum(filtered_ppsqm) / len(filtered_ppsqm)) if filtered_ppsqm else None

    neighborhood_vals = {}
    for item in items:
        ppsqm = item["pricePerSqm"]
        if ppsqm is None:
            continue
        nb = item.get("neighborhood") or ""
        if nb:
            neighborhood_vals.setdefault(nb, []).append(ppsqm)

    neighborhood_avg = {k: round(sum(v) / len(v)) for k, v in neighborhood_vals.items()}

    for item in items:
        nb = item.get("neighborhood") or ""
        item["avgPricePerSqmNeighborhood"] = neighborhood_avg.get(nb)
        item["avgPricePerSqmFiltered"] = filtered_avg

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

# ---------------------------------------------------------------------------
# Neighborhood → city mapping with Yad2 numeric IDs
# Each neighborhood maps to {"cities": [...], "yad2_id": int}
# IDs were discovered by scanning Yad2's feed-literal resolver
# ---------------------------------------------------------------------------
NEIGHBORHOOD_MAP = {
    # ── חיפה (names as they appear in Yad2 listings) ──
    "אחוזה": {"cities": ["חיפה"], "yad2_id": 613},
    "בת גלים": {"cities": ["חיפה"], "yad2_id": 598},
    "גאולה": {"cities": ["חיפה"], "yad2_id": 1682},
    "הדר מרכז": {"cities": ["חיפה"], "yad2_id": 1671},
    "הדר עליון": {"cities": ["חיפה"], "yad2_id": 1684},
    "הוד הכרמל, דניה": {"cities": ["חיפה"], "yad2_id": 616},
    "המיימוני": {"cities": ["חיפה"], "yad2_id": 1683},
    "העיר התחתית, ואדי סאליב": {"cities": ["חיפה"], "yad2_id": 651},
    "הרצליה": {"cities": ["חיפה"], "yad2_id": 1669},
    "ואדי נסנאס": {"cities": ["חיפה"], "yad2_id": 622},
    "זיו": {"cities": ["חיפה"], "yad2_id": 643},
    "חוף הכרמל": {"cities": ["חיפה"], "yad2_id": 1665},
    "חליסה": {"cities": ["חיפה"], "yad2_id": 632},
    "יזרעאליה": {"cities": ["חיפה"], "yad2_id": 641},
    "יל\"ג": {"cities": ["חיפה"], "yad2_id": 1685},
    "כבאביר": {"cities": ["חיפה"], "yad2_id": 610},
    "כבירים": {"cities": ["חיפה"], "yad2_id": 611},
    "כרמל מערבי": {"cities": ["חיפה"], "yad2_id": 609},
    "כרמל מרכזי - שד' קיש": {"cities": ["חיפה"], "yad2_id": 625},
    "כרמל צרפתי": {"cities": ["חיפה"], "yad2_id": 602},
    "כרמליה": {"cities": ["חיפה"], "yad2_id": 612},
    "מסדה": {"cities": ["חיפה"], "yad2_id": 1668},
    "מרכז הכרמל": {"cities": ["חיפה"], "yad2_id": 624},
    "מת\"מ, נאות פרס": {"cities": ["חיפה"], "yad2_id": 607},
    "נווה דוד": {"cities": ["חיפה"], "yad2_id": 606},
    "נווה יוסף": {"cities": ["חיפה"], "yad2_id": 644},
    "נווה פז": {"cities": ["חיפה"], "yad2_id": 645},
    "נווה שאנן": {"cities": ["חיפה"], "yad2_id": 642},
    "נעמ\"ת": {"cities": ["חיפה"], "yad2_id": 1675},
    "סביוני הכרמל": {"cities": ["חיפה"], "yad2_id": 634},
    "עבאס": {"cities": ["חיפה"], "yad2_id": 1670},
    "עין הים": {"cities": ["חיפה"], "yad2_id": 601},
    "קרית אליהו": {"cities": ["חיפה"], "yad2_id": 619},
    "קרית אליעזר": {"cities": ["חיפה"], "yad2_id": 599},
    "קרית חיים מזרחית": {"cities": ["חיפה"], "yad2_id": 650},
    "קרית חיים מערבית": {"cities": ["חיפה"], "yad2_id": 648},
    "קרית שמואל": {"cities": ["חיפה"], "yad2_id": 649},
    "קרית שפרינצק": {"cities": ["חיפה"], "yad2_id": 604},
    "רוממה": {"cities": ["חיפה"], "yad2_id": 628},
    "רמות רמז": {"cities": ["חיפה"], "yad2_id": 637},
    "רמת אלון": {"cities": ["חיפה"], "yad2_id": 635},
    "רמת אלמוגי": {"cities": ["חיפה"], "yad2_id": 636},
    "רמת אשכול": {"cities": ["חיפה"], "yad2_id": 614},
    "רמת בגין": {"cities": ["חיפה"], "yad2_id": 615},
    "רמת בן גוריון": {"cities": ["חיפה"], "yad2_id": 629},
    "רמת גולדה": {"cities": ["חיפה"], "yad2_id": 631},
    "רמת הדר": {"cities": ["חיפה"], "yad2_id": 626},
    "רמת התשבי": {"cities": ["חיפה"], "yad2_id": 608},
    "רמת חביב / רמת הנשיא": {"cities": ["חיפה"], "yad2_id": 1664},
    "רמת שאול": {"cities": ["חיפה"], "yad2_id": 605},
    "שוק תלפיות": {"cities": ["חיפה"], "yad2_id": 1672},
    "שמבור": {"cities": ["חיפה"], "yad2_id": 1674},
    "שער העליה": {"cities": ["חיפה"], "yad2_id": 603},
    "שער פלמר, תחנת רכבת השמונה": {"cities": ["חיפה"], "yad2_id": 1667},
    "תל עמל": {"cities": ["חיפה"], "yad2_id": 652},
}


def resolve_neighborhood(neighborhood_name):
    """Return list of cities a neighborhood belongs to, or empty list."""
    entry = NEIGHBORHOOD_MAP.get(neighborhood_name)
    if entry is None:
        return []
    return entry["cities"]


def resolve_neighborhood_yad2_id(neighborhood_name):
    """Return the Yad2 numeric neighborhood ID, or None."""
    entry = NEIGHBORHOOD_MAP.get(neighborhood_name)
    if entry is None:
        return None
    return entry.get("yad2_id")


def resolve_city_code(settlement):
    """Return a city code — accepts codes directly or known city names."""
    if settlement.isdigit():
        return settlement
    return CITY_CODES.get(settlement)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/cities")
def api_cities():
    return jsonify(sorted(CITY_CODES.keys()))


@app.route("/api/locations")
def api_locations():
    """Return all known neighborhoods grouped by city for the frontend picker."""
    # Build city → [neighborhoods] from NEIGHBORHOOD_MAP
    city_hoods = {}
    for hood, entry in NEIGHBORHOOD_MAP.items():
        for city in entry["cities"]:
            city_hoods.setdefault(city, []).append(hood)
    # Sort neighborhoods within each city
    result = {city: sorted(hoods) for city, hoods in city_hoods.items()}
    return jsonify(result)


@app.route("/api/realestate/search")
def realestate_search():
    deal_type = request.args.get("dealType", "rent")
    settlement = request.args.get("settlement", "")
    min_price = request.args.get("minPrice", "0")
    max_price = request.args.get("maxPrice", "100000000")
    min_sqm_str = request.args.get("minSqm", "")
    max_sqm_str = request.args.get("maxSqm", "")
    neighborhood = request.args.get("neighborhood", "").strip()

    # Neighborhood-first: if no settlement but neighborhood given, resolve city
    if not settlement and neighborhood:
        cities = resolve_neighborhood(neighborhood)
        if len(cities) == 1:
            settlement = cities[0]
        elif len(cities) > 1:
            return jsonify({"error": "ambiguous", "cities": cities, "neighborhood": neighborhood}), 300
        else:
            return jsonify({"error": f"שכונה '{neighborhood}' לא נמצאה במאגר"}), 404

    if not settlement:
        return jsonify({"error": "יש לבחור שכונה"}), 400

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

    neighborhood_id = resolve_neighborhood_yad2_id(neighborhood) if neighborhood else None

    t_start = time.time()
    try:
        items, total_pages = fetch_all_yad2_pages(deal_type, city_code, min_price, max_price, neighborhood_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data from Yad2: {e}"}), 502

    raw_count = len(items)
    items = remove_duplicates(items)
    items = filter_items(items, min_sqm=min_sqm, max_sqm=max_sqm, neighborhood=neighborhood)

    items = compute_price_stats(items)
    fetch_time = round(time.time() - t_start, 1)

    avg_sample_count = sum(1 for it in items if it.get("pricePerSqm"))

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

    return jsonify({
        "search_id": search_id,
        "items": items,
        "total_pages": total_pages,
        "fetchTime": fetch_time,
        "avgSampleCount": avg_sample_count,
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

    neighborhood_id = resolve_neighborhood_yad2_id(params.get("neighborhood", ""))

    try:
        new_items, total_pages = fetch_all_yad2_pages(
            params["dealType"], params["cityCode"],
            params["minPrice"], params["maxPrice"],
            neighborhood_id,
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

    changed_items = compute_price_stats(changed_items)

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

    return jsonify({
        "items": changed_items,
        "search_params": params,
        "total_pages": total_pages,
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
# LLM Analysis — fetch listing description + call C++ sentiment service
# ---------------------------------------------------------------------------
# TODO: Upgrade to better Hebrew-capable LLM for improved sentiment analysis
#       Current: Qwen 2.5 1.5B (struggles with Hebrew, returns neutral/0%)
#       Future options: Qwen 2.5 3B/7B, multilingual BERT, or Hebrew-specific model
LLM_SERVICE_URL = os.environ.get("LLM_SERVICE_URL", "http://127.0.0.1:8282")
_description_cache = {}  # linkToken -> description string


def fetch_listing_description(token):
    """Fetch the description from an individual Yad2 listing detail page."""
    if token in _description_cache:
        return _description_cache[token]

    url = f"https://www.yad2.co.il/realestate/item/{token}"
    session = get_yad2_session()
    try:
        resp = session.get(url, timeout=20)
    except Exception:
        reset_yad2_session()
        session = get_yad2_session()
        resp = session.get(url, timeout=20)

    match = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not match:
        return None

    try:
        next_data = json.loads(match.group(1))
        page_props = next_data.get("props", {}).get("pageProps", {})
        # Try multiple paths where description might live
        item_data = page_props.get("itemData", {})
        description = (
            item_data.get("description")
            or item_data.get("info", {}).get("content")
            or item_data.get("metaData", {}).get("description")
            or ""
        )
        # Also try dehydratedState queries
        if not description:
            for q in page_props.get("dehydratedState", {}).get("queries", []):
                data = q.get("state", {}).get("data", {})
                if isinstance(data, dict):
                    # searchText contains the full listing description
                    description = data.get("searchText") or data.get("description", "")
                    if description:
                        break

        if description:
            # Extract just the client description part (after "תאור לקוח")
            if "תאור לקוח" in description:
                parts = description.split("תאור לקוח", 1)
                description = parts[1].strip().strip("*").strip()
            _description_cache[token] = description
        return description or None
    except (json.JSONDecodeError, KeyError):
        return None


_sentiment_cache = {}  # linkToken -> sentiment result dict


@app.route("/api/realestate/analyze/<token>")
def realestate_analyze(token):
    """Lazy endpoint: fetch description + call C++ LLM service for sentiment."""
    if not token:
        return jsonify({"error": "token is required"}), 400

    # Check Python-side cache
    if token in _sentiment_cache:
        return jsonify(_sentiment_cache[token])

    # Fetch description
    description = fetch_listing_description(token)
    if not description:
        result = {"sentiment": "neutral", "confidence": 0.0, "keywords": [],
                  "note": "No description available"}
        _sentiment_cache[token] = result
        return jsonify(result)

    # Call C++ LLM service
    import urllib.request
    try:
        payload = json.dumps({"items": [{"token": token, "description": description}]})
        req = urllib.request.Request(
            f"{LLM_SERVICE_URL}/analyze",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result = data.get("results", {}).get(token, {
                "sentiment": "neutral", "confidence": 0.0, "keywords": []
            })
            _sentiment_cache[token] = result
            return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"LLM service unavailable: {e}",
                        "sentiment": "neutral", "confidence": 0.0, "keywords": []}), 200


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
