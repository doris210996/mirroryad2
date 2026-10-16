import os
import re
import json
import time
import datetime

from flask import Flask, send_from_directory, jsonify, request

import facebook_imports
from polite_fetcher import AccessDenied, FetchError, RobotsDisallowed, default_fetcher

# GovMap API configuration
GOVMAP_API_TOKEN = os.environ.get("GOVMAP_API_TOKEN", "")
if GOVMAP_API_TOKEN:
    print(f"[GovMap] API token configured: {GOVMAP_API_TOKEN[:8]}...")
else:
    print("[GovMap] No API token - using sample data for deep analysis")

app = Flask(__name__)
fetcher = default_fetcher()
print(f"[Fetcher] User-Agent: {fetcher.user_agent}")

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
# Yad2 — fetched through the polite fetcher (robots.txt, rate limit, cache).
# Yad2's robots.txt disallows the `price=` query parameter, so we request the
# city/neighborhood feed only and apply the price range locally.
# ---------------------------------------------------------------------------
YAD2_MAX_PAGES = int(os.environ.get("YAD2_MAX_PAGES", "10"))
DEFAULT_MAX_PRICE = 100000000
_NEXT_DATA_RE = re.compile(r'__NEXT_DATA__[^>]*>(.*?)</script>', re.DOTALL)


def _has_next_data(text):
    return "__NEXT_DATA__" in text


def fetch_next_data(url):
    """Fetch a Yad2 page and return its parsed __NEXT_DATA__ JSON."""
    resp = fetcher.get(url, validate=_has_next_data)
    match = _NEXT_DATA_RE.search(resp.text)
    if not match:
        raise FetchError("Could not extract data from Yad2 page")
    return json.loads(match.group(1))


def fetch_yad2_page(deal_type, city_code, page, neighborhood_id=None):
    """Fetch one Yad2 search results page and return its feed data."""
    deal_path = "rent" if deal_type == "rent" else "forsale"
    url = f"https://www.yad2.co.il/realestate/{deal_path}?city={city_code}&page={page}"
    if neighborhood_id:
        url += f"&neighborhood={neighborhood_id}"

    queries = (
        fetch_next_data(url).get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    for q in queries:
        key = q.get("queryKey", [""])[0]
        if "feed" in str(key) and deal_path in str(key):
            return q["state"]["data"]
    raise FetchError("No feed data found in Yad2 response")


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
            tags = [t.get("name", t) if isinstance(t, dict) else str(t)
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


def fetch_all_yad2_pages(deal_type, city_code, neighborhood_id=None):
    """
    Fetch Yad2 result pages one at a time (the fetcher paces requests per host).
    Returns (items, total_pages, pages_fetched). If Yad2 stops us part-way, the
    pages already fetched are returned; if page 1 fails, the error propagates.
    """
    feed = fetch_yad2_page(deal_type, city_code, 1, neighborhood_id)
    total_pages = feed.get("pagination", {}).get("totalPages", 1)
    all_items = extract_items(feed)
    pages_fetched = 1

    t_start = time.time()
    for page in range(2, min(total_pages, YAD2_MAX_PAGES) + 1):
        try:
            feed = fetch_yad2_page(deal_type, city_code, page, neighborhood_id)
        except FetchError as e:
            print(f"[Yad2] Stopping at page {page}: {e}")
            break
        all_items.extend(extract_items(feed))
        pages_fetched += 1

    elapsed = round(time.time() - t_start, 1)
    print(f"[Yad2] Fetched {pages_fetched}/{total_pages} pages in {elapsed}s, {len(all_items)} raw items")
    return all_items, total_pages, pages_fetched


# ---------------------------------------------------------------------------
# Facebook — manually imported posts (see facebook_imports.py)
# ---------------------------------------------------------------------------
FB_MOCK = os.environ.get("FB_MOCK", "") == "1"


def _same_city(a, b):
    return bool(a and b) and (a in b or b in a)


def get_facebook_items(settlement=""):
    """Imported posts whose detected city matches `settlement` (or has no city)."""
    items = facebook_imports.list_imports()
    if FB_MOCK:
        items = items + facebook_imports.mock_items()
    return [it for it in items if not it.get("city") or not settlement or _same_city(it["city"], settlement)]


def describe_fetch_error(e):
    """User-facing (Hebrew) message for a fetch failure."""
    if isinstance(e, AccessDenied):
        minutes = max(1, int((e.retry_at - time.monotonic()) / 60))
        return (f"Yad2 חסם גישה אוטומטית — המערכת מכבדת זאת ולא עוקפת את החסימה. "
                f"ניסיון חוזר בעוד כ-{minutes} דקות.")
    if isinstance(e, RobotsDisallowed):
        return "קובץ robots.txt של האתר אוסר על הבקשה הזו, ולכן היא לא נשלחה."
    return f"שליפת הנתונים נכשלה: {e}"


def fetch_multi_source(deal_type, city_code, settlement, neighborhood_id=None, enabled_sources=None):
    """
    Collect listings from all enabled sources.
    Returns dict with per-source items and user-facing error messages.
    """
    if enabled_sources is None:
        enabled_sources = ["yad2", "facebook"]

    results = {"yad2": [], "facebook": [], "errors": {}, "yad2_total_pages": 0, "yad2_pages_fetched": 0}

    if "yad2" in enabled_sources:
        try:
            items, total_pages, pages_fetched = fetch_all_yad2_pages(deal_type, city_code, neighborhood_id)
            for item in items:
                item["source"] = "yad2"
            results["yad2"] = items
            results["yad2_total_pages"] = total_pages
            results["yad2_pages_fetched"] = pages_fetched
        except Exception as e:
            print(f"[yad2] Error: {e}")
            results["errors"]["yad2"] = describe_fetch_error(e)

    if "facebook" in enabled_sources:
        results["facebook"] = get_facebook_items(settlement)

    return results


def remove_duplicates(items):
    """Remove duplicates using (source, linkToken) as key to prevent collisions."""
    seen = set()
    result = []
    for item in items:
        source = item.get("source", "yad2")  # Default to yad2 for backwards compatibility
        token = item.get("linkToken", "")
        key = (source, token)
        if token and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def filter_items(items, min_sqm=None, max_sqm=None, neighborhood=None, min_price=0, max_price=DEFAULT_MAX_PRICE):
    """Filter items by price range, sqm range and/or neighborhood (substring match)."""
    price_filtered = min_price > 0 or max_price < DEFAULT_MAX_PRICE
    filtered = []
    for item in items:
        # price filter (done locally: Yad2's robots.txt disallows the price= parameter)
        if price_filtered:
            price = parse_price(item.get("price"))
            if price is None or price < min_price or price > max_price:
                continue

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
        # (imported Facebook posts have no structured neighborhood — match their text)
        if neighborhood:
            item_nb = item.get("neighborhood") or item.get("description") or ""
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
    "תל אביב": "5000", "תל אביב יפו": "5000", "ירושלים": "3000", "חיפה": "4000",
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

    # ── תל אביב יפו ──
    "אוניברסיטת תל אביב": {"cities": ["תל אביב יפו"], "yad2_id": 846},
    "אורות": {"cities": ["תל אביב יפו"], "yad2_id": 487},
    "אזור תעסוקה צומת חולון": {"cities": ["תל אביב יפו"], "yad2_id": 491},
    "אזורי חן, גימל החדשה": {"cities": ["תל אביב יפו"], "yad2_id": 1490},
    "איזור שדה דב": {"cities": ["תל אביב יפו"], "yad2_id": 850},
    "אפקה": {"cities": ["תל אביב יפו"], "yad2_id": 1514},
    "בבלי": {"cities": ["תל אביב יפו"], "yad2_id": 1518},
    "ביצרון ורמת ישראל": {"cities": ["תל אביב יפו"], "yad2_id": 486},
    "גבעת הרצל, אזור המלאכה יפו": {"cities": ["תל אביב יפו"], "yad2_id": 847},
    "גלילות": {"cities": ["תל אביב יפו"], "yad2_id": 1491},
    "גני צהלה, רמות צהלה": {"cities": ["תל אביב יפו"], "yad2_id": 211},
    "גני שרונה, קרית הממשלה": {"cities": ["תל אביב יפו"], "yad2_id": 1462},
    "הגוש הגדול, רמת אביב החדשה, נופי ים": {"cities": ["תל אביב יפו"], "yad2_id": 195},
    "הדר יוסף": {"cities": ["תל אביב יפו"], "yad2_id": 1513},
    "המשתלה": {"cities": ["תל אביב יפו"], "yad2_id": 315},
    "הצפון החדש - דרום": {"cities": ["תל אביב יפו"], "yad2_id": 1519},
    "הצפון החדש - כיכר המדינה": {"cities": ["תל אביב יפו"], "yad2_id": 1516},
    "הצפון החדש - צפון": {"cities": ["תל אביב יפו"], "yad2_id": 204},
    "הצפון הישן - דרום": {"cities": ["תל אביב יפו"], "yad2_id": 1461},
    "הצפון הישן - צפון": {"cities": ["תל אביב יפו"], "yad2_id": 1483},
    'התקוה, בית יעקב, נווה צה"ל': {"cities": ["תל אביב יפו"], "yad2_id": 308},
    "יד אליהו": {"cities": ["תל אביב יפו"], "yad2_id": 206},
    "יפו ד', גבעת התמרים": {"cities": ["תל אביב יפו"], "yad2_id": 321},
    "יפו העתיקה": {"cities": ["תל אביב יפו"], "yad2_id": 320},
    "כוכב הצפון": {"cities": ["תל אביב יפו"], "yad2_id": 198},
    "כפיר, נווה כפיר": {"cities": ["תל אביב יפו"], "yad2_id": 210},
    "כרם התימנים": {"cities": ["תל אביב יפו"], "yad2_id": 1521},
    "לב תל אביב, לב העיר צפון": {"cities": ["תל אביב יפו"], "yad2_id": 1520},
    "לבנה": {"cities": ["תל אביב יפו"], "yad2_id": 319},
    "מונטיפיורי, הרכבת": {"cities": ["תל אביב יפו"], "yad2_id": 485},
    "מכללת תל אביב יפו, דקר": {"cities": ["תל אביב יפו"], "yad2_id": 496},
    "מעוז אביב": {"cities": ["תל אביב יפו"], "yad2_id": 214},
    "מרכז הירידים": {"cities": ["תל אביב יפו"], "yad2_id": 483},
    "נאות אפקה א'": {"cities": ["תל אביב יפו"], "yad2_id": 200},
    "נאות אפקה ב'": {"cities": ["תל אביב יפו"], "yad2_id": 199},
    "נווה אביבים": {"cities": ["תל אביב יפו"], "yad2_id": 196},
    "נווה אליעזר וכפר שלם מזרח": {"cities": ["תל אביב יפו"], "yad2_id": 209},
    "נווה ברבור, כפר שלם מערב": {"cities": ["תל אביב יפו"], "yad2_id": 208},
    "נווה גולן, יפו ג'": {"cities": ["תל אביב יפו"], "yad2_id": 492},
    "נווה חן": {"cities": ["תל אביב יפו"], "yad2_id": 207},
    "נווה עופר, תל כביר": {"cities": ["תל אביב יפו"], "yad2_id": 213},
    "נווה צדק": {"cities": ["תל אביב יפו"], "yad2_id": 848},
    "נווה שאנן": {"cities": ["תל אביב יפו"], "yad2_id": 307},
    "נווה שרת": {"cities": ["תל אביב יפו"], "yad2_id": 314},
    "נחלת יצחק": {"cities": ["תל אביב יפו"], "yad2_id": 317},
    "ניר אביב": {"cities": ["תל אביב יפו"], "yad2_id": 1650},
    "נמל תל אביב": {"cities": ["תל אביב יפו"], "yad2_id": 484},
    "עג'מי, גבעת העליה": {"cities": ["תל אביב יפו"], "yad2_id": 493},
    "עזרא, הארגזים": {"cities": ["תל אביב יפו"], "yad2_id": 488},
    "עתידים, אזור התעסוקה רמת החייל": {"cities": ["תל אביב יפו"], "yad2_id": 482},
    "פארק דרום": {"cities": ["תל אביב יפו"], "yad2_id": 489},
    "פארק החורשות": {"cities": ["תל אביב יפו"], "yad2_id": 490},
    "פארק הירקון": {"cities": ["תל אביב יפו"], "yad2_id": 1512},
    "פלורנטין": {"cities": ["תל אביב יפו"], "yad2_id": 205},
    "צהלה": {"cities": ["תל אביב יפו"], "yad2_id": 313},
    "צהלון, שיכוני חסכון": {"cities": ["תל אביב יפו"], "yad2_id": 494},
    "צוקי אביב": {"cities": ["תל אביב יפו"], "yad2_id": 991431},
    "צמרות איילון, פארק צמרת": {"cities": ["תל אביב יפו"], "yad2_id": 1517},
    "צפון יפו, המושבה האמריקאית-גרמנית": {"cities": ["תל אביב יפו"], "yad2_id": 495},
    "קרית שאול": {"cities": ["תל אביב יפו"], "yad2_id": 316},
    "קרית שלום": {"cities": ["תל אביב יפו"], "yad2_id": 212},
    "רביבים": {"cities": ["תל אביב יפו"], "yad2_id": 202},
    "רמת אביב": {"cities": ["תל אביב יפו"], "yad2_id": 197},
    "רמת אביב ג'": {"cities": ["תל אביב יפו"], "yad2_id": 1515},
    "רמת החייל": {"cities": ["תל אביב יפו"], "yad2_id": 203},
    "רמת הטייסים": {"cities": ["תל אביב יפו"], "yad2_id": 1649},
    "שאר העיר": {"cities": ["תל אביב יפו"], "yad2_id": 991392},
    "שיכון דן, נווה דן": {"cities": ["תל אביב יפו"], "yad2_id": 201},
    "שפירא": {"cities": ["תל אביב יפו"], "yad2_id": 215},
    "תכנית ל', למד": {"cities": ["תל אביב יפו"], "yad2_id": 849},
    "תל ברוך": {"cities": ["תל אביב יפו"], "yad2_id": 312},
    "תל ברוך צפון": {"cities": ["תל אביב יפו"], "yad2_id": 481},
    "תל חיים": {"cities": ["תל אביב יפו"], "yad2_id": 318},
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
    max_price = request.args.get("maxPrice", str(DEFAULT_MAX_PRICE))
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

    # Get enabled sources from request (comma-separated)
    sources_param = request.args.get("sources", "yad2,facebook")
    enabled_sources = [s.strip() for s in sources_param.split(",") if s.strip()]

    t_start = time.time()

    # Fetch from multiple sources in parallel
    try:
        source_results = fetch_multi_source(deal_type, city_code, settlement, neighborhood_id, enabled_sources)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data: {e}"}), 502

    # Merge all items from all sources
    items = source_results["yad2"] + source_results["facebook"]
    total_pages = source_results.get("yad2_total_pages", 0)
    pages_fetched = source_results.get("yad2_pages_fetched", 0)

    # Track source counts before filtering
    source_counts = {
        "yad2": len(source_results["yad2"]),
        "facebook": len(source_results["facebook"]),
    }

    # If both sources failed, return error
    if not items and source_results["errors"]:
        return jsonify({
            "error": " · ".join(source_results["errors"].values()),
            "errors": source_results["errors"]
        }), 502

    raw_count = len(items)
    items = remove_duplicates(items)
    items = filter_items(items, min_sqm=min_sqm, max_sqm=max_sqm, neighborhood=neighborhood,
                         min_price=min_price, max_price=max_price)

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
        "pages_fetched": pages_fetched,
        "fetchTime": fetch_time,
        "avgSampleCount": avg_sample_count,
        "sources": source_counts,
        "errors": source_results["errors"],
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
        new_items, total_pages, _ = fetch_all_yad2_pages(params["dealType"], params["cityCode"], neighborhood_id)
    except Exception as e:
        return jsonify({"error": describe_fetch_error(e)}), 502
    for item in new_items:
        item["source"] = "yad2"

    new_items = remove_duplicates(new_items)
    new_items = filter_items(
        new_items,
        min_sqm=params.get("minSqm"),
        max_sqm=params.get("maxSqm"),
        neighborhood=params.get("neighborhood", ""),
        min_price=params.get("minPrice", 0),
        max_price=params.get("maxPrice", DEFAULT_MAX_PRICE),
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


@app.route("/api/fetcher/status")
def fetcher_status():
    """Compliance/health view of the polite fetcher: rate limits, cache, blocks."""
    return jsonify(fetcher.status())


@app.route("/api/facebook/imports", methods=["GET"])
def facebook_list_imports():
    return jsonify(facebook_imports.list_imports())


@app.route("/api/facebook/imports", methods=["POST"])
def facebook_add_import():
    """Import a Facebook post the user pasted: {"text": "...", "url": "..."}."""
    body = request.get_json(silent=True) or {}
    item, error = facebook_imports.import_post(body.get("text", ""), body.get("url", ""))
    if error:
        return jsonify({"error": error}), 422
    return jsonify(item), 201


@app.route("/api/facebook/imports/<token>", methods=["DELETE"])
def facebook_delete_import(token):
    if not facebook_imports.delete_import(token):
        return jsonify({"error": "not found"}), 404
    return "", 204


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
    try:
        next_data = fetch_next_data(url)
    except (FetchError, ValueError) as e:
        print(f"[Analyze] Could not fetch listing {token}: {e}")
        return None

    try:
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
# Deep Analysis — Israeli Government Real Estate Data
# ---------------------------------------------------------------------------
@app.route("/api/realestate/deep-analysis")
def deep_analysis():
    """
    Deep analysis of a specific property using Israeli gov data.
    Accepts either a Yad2 URL or a direct address + rooms.
    """
    start_time = time.time()

    input_str = request.args.get("input", "").strip()
    rooms = request.args.get("rooms", "")

    if not input_str:
        return jsonify({"error": "יש להזין קישור או כתובת"}), 400

    # Parse input - determine if URL or address
    property_data = parse_property_input(input_str, rooms)
    if not property_data:
        return jsonify({"error": "לא הצלחנו לזהות את הכתובת"}), 400

    # Fetch historical transactions from Israeli government data
    transactions = fetch_gov_transactions(
        city=property_data.get("city"),
        neighborhood=property_data.get("neighborhood"),
        rooms=property_data.get("rooms")
    )

    # Calculate statistics
    statistics = calculate_transaction_stats(transactions, property_data)

    # Generate insights
    insights = generate_insights(property_data, statistics)

    fetch_time = round(time.time() - start_time, 2)

    return jsonify({
        "property": property_data,
        "transactions": transactions,
        "statistics": statistics,
        "insights": insights,
        "fetchTime": fetch_time,
        "source": "נתוני דוגמה להמחשה (Demo Data)",
        "note": "הנתונים הם דוגמאות סטטיסטיות להמחשה. לנתונים ממשיים נדרש חיבור ל-API רשות המסים או GovMap."
    })


def parse_property_input(input_str, rooms):
    """
    Parse input string - could be a Yad2 URL or a direct address.
    Returns property details dict.
    """
    property_data = {"rooms": rooms}

    # Check if input is a Yad2 URL
    if "yad2.co.il" in input_str or "yad2.com" in input_str:
        # Extract listing details from Yad2
        yad2_data = extract_yad2_listing(input_str)
        if yad2_data:
            property_data.update(yad2_data)
            return property_data

    # Otherwise, treat as address - parse it
    address_parts = parse_israeli_address(input_str)
    if address_parts:
        property_data.update(address_parts)
        return property_data

    return None


def extract_yad2_listing(url):
    """Fetch listing details from Yad2 URL."""
    try:
        # Extract item ID from URL
        match = re.search(r'/item/([a-zA-Z0-9-]+)', url)
        if not match:
            return None

        item_id = match.group(1)

        # Fetched politely: if robots.txt or the site refuses, fall back to address parsing
        api_url = f"https://api.yad2.co.il/realestate/item/{item_id}"
        data = fetcher.get(api_url).json()
        if not data.get("data"):
            return None

        listing = data["data"]

        return {
            "address": listing.get("address", {}).get("street", ""),
            "city": listing.get("address", {}).get("city", {}).get("text", ""),
            "neighborhood": listing.get("neighborhood", ""),
            "rooms": listing.get("rooms", ""),
            "price": listing.get("price", ""),
            "sqm": listing.get("square_meters", "")
        }
    except Exception as e:
        print(f"[Deep Analysis] Failed to fetch Yad2 listing: {e}")
        return None


def parse_israeli_address(address_str):
    """
    Parse Israeli address string.
    Format examples: "רחוב הרצל 10, תל אביב" or "תל אביב, שכונת לב העיר"
    """
    # Common Israeli cities
    cities = {
        "תל אביב": 5000, "תל אביב יפו": 5000, "תל-אביב": 5000,
        "ירושלים": 3000,
        "חיפה": 4000,
        "באר שבע": 9000, "באר-שבע": 9000,
        "ראשון לציון": 8300, "ראשון-לציון": 8300,
        "פתח תקווה": 7900, "פתח-תקווה": 7900,
        "אשדוד": 7700,
        "נתניה": 1500,
        "חולון": 6100,
        "בני ברק": 6400, "בני-ברק": 6400,
        "רמת גן": 8200, "רמת-גן": 8200,
        "אשקלון": 7800,
        "רחובות": 7400,
        "בת ים": 6800, "בת-ים": 6800,
        "הרצליה": 6900,
        "כפר סבא": 6900, "כפר-סבא": 6900,
        "חדרה": 6200,
        "מודיעין": 7100,
        "נצרת": 1700,
        "רעננה": 8500,
        "לוד": 7100,
        "רמלה": 7200,
    }

    city = None
    neighborhood = None

    # Try to find city in address
    for city_name, city_code in cities.items():
        if city_name in address_str:
            city = city_name
            break

    if not city:
        return None

    # Extract neighborhood if mentioned (שכונת, שכונה)
    hood_match = re.search(r'שכונת?\s+([א-ת\s]+?)(?:,|$)', address_str)
    if hood_match:
        neighborhood = hood_match.group(1).strip()

    return {
        "address": address_str,
        "city": city,
        "neighborhood": neighborhood
    }


def fetch_gov_transactions(city=None, neighborhood=None, rooms=None):
    """
    Fetch historical real estate transactions from Israeli government data.
    Tries multiple sources: GovMap API, Tax Authority site.
    """
    print(f"[Deep Analysis] Fetching transactions for city={city}, neighborhood={neighborhood}, rooms={rooms}")

    # Try Method 1: GovMap API
    transactions = try_govmap_api(city, neighborhood, rooms)
    if transactions:
        return transactions

    # Try Method 2: Tax Authority site scraping
    transactions = try_tax_authority_scraping(city, neighborhood, rooms)
    if transactions:
        return transactions

    # If all methods fail, return empty list with a note
    print("[Deep Analysis] No transaction data could be fetched")
    return []


def try_govmap_api(city, neighborhood, rooms):
    """
    Attempt to fetch data from GovMap API.

    NOTE: GovMap has API restrictions and requires proper authentication.
    For production use, consider:
    1. Obtaining official GovMap API credentials
    2. Using their documented geocoding API: https://api.govmap.gov.il/docs/
    3. Implementing proper layer querying for real estate data

    Current implementation returns sample data for demonstration.
    """
    print(f"[GovMap] API integration requires credentials - using sample data")

    # For demonstration, return sample transaction data
    # In production, this would query the actual GovMap API with proper authentication
    if not city:
        return None

    # Generate sample transactions based on city
    return generate_sample_transactions(city, neighborhood, rooms)


def generate_sample_transactions(city, neighborhood, rooms):
    """
    Generate sample transaction data for demonstration.
    Replace this with actual API calls in production.
    """
    import random
    from datetime import datetime, timedelta

    # Base prices per city (approximate averages for demonstration)
    city_base_prices = {
        "תל אביב": 3500000,
        "תל אביב יפו": 3500000,
        "תל-אביב": 3500000,
        "ירושלים": 2800000,
        "חיפה": 2000000,
        "באר שבע": 1500000,
        "באר-שבע": 1500000,
        "ראשון לציון": 2200000,
        "פתח תקווה": 2400000,
        "אשדוד": 1800000,
        "נתניה": 2000000,
        "חולון": 2300000,
        "בני ברק": 2600000,
        "רמת גן": 3000000,
        "הרצליה": 3200000,
    }

    base_price = city_base_prices.get(city, 2000000)

    # Try to parse rooms
    try:
        rooms_num = float(rooms) if rooms else 3.5
    except (ValueError, TypeError):
        rooms_num = 3.5

    # Generate 8-15 sample transactions
    num_transactions = random.randint(8, 15)
    transactions = []

    for i in range(num_transactions):
        # Random date in last 2 years
        days_ago = random.randint(0, 730)
        tx_date = datetime.now() - timedelta(days=days_ago)

        # Price variation: ±20% from base, adjusted by rooms
        price_factor = (rooms_num / 3.5) * random.uniform(0.8, 1.2)
        price = int(base_price * price_factor)

        # Random sqm (typically 25-35 sqm per room in Israel)
        sqm = int(rooms_num * random.uniform(25, 35))

        # Random small room variation (±0.5 rooms)
        tx_rooms = rooms_num + random.choice([-0.5, 0, 0.5])

        # Generate street name
        streets = ["הרצל", "בן יהודה", "דיזנגוף", "רוטשילד", "אלנבי", "ויצמן", "בגין"]
        street_num = random.randint(1, 200)
        address = f"{random.choice(streets)} {street_num}"
        if neighborhood:
            address += f", {neighborhood}"
        address += f", {city}"

        transactions.append({
            "date": tx_date.strftime("%d/%m/%Y"),
            "price": f"{price:,} ₪",
            "address": address,
            "rooms": str(tx_rooms),
            "sqm": str(sqm)
        })

    # Sort by date (newest first)
    transactions.sort(key=lambda x: datetime.strptime(x["date"], "%d/%m/%Y"), reverse=True)

    print(f"[GovMap] Generated {len(transactions)} sample transactions")
    return transactions


def parse_govmap_response(data, rooms_filter=None):
    """Parse GovMap API response to extract transaction data."""
    transactions = []

    try:
        # GovMap response structure varies - handle different formats
        if isinstance(data, dict):
            # Look for transaction records in various possible locations
            records = data.get("data", []) or data.get("results", []) or data.get("features", [])

            for record in records:
                # Extract transaction details from record
                tx = {}

                # Try to extract common fields
                if "properties" in record:
                    props = record["properties"]
                    tx["date"] = props.get("TARICH_BRIHA", "") or props.get("date", "")
                    tx["price"] = props.get("SHAVUI_BRIHA", "") or props.get("price", "")
                    tx["address"] = props.get("KTOVT_BRIHA", "") or props.get("address", "")
                    tx["rooms"] = props.get("CHADRIM", "") or props.get("rooms", "")
                    tx["sqm"] = props.get("SHIACH", "") or props.get("sqm", "")
                else:
                    # Direct fields
                    tx["date"] = record.get("date", "")
                    tx["price"] = record.get("price", "")
                    tx["address"] = record.get("address", "")
                    tx["rooms"] = record.get("rooms", "")
                    tx["sqm"] = record.get("sqm", "")

                # Filter by rooms if specified
                if rooms_filter and tx.get("rooms"):
                    try:
                        tx_rooms = float(tx["rooms"])
                        filter_rooms = float(rooms_filter)
                        # Allow ±0.5 room difference
                        if abs(tx_rooms - filter_rooms) > 0.5:
                            continue
                    except (ValueError, TypeError):
                        pass

                if tx.get("price") or tx.get("date"):
                    transactions.append(tx)

    except Exception as e:
        print(f"[GovMap] Parsing error: {e}")

    return transactions


def try_tax_authority_scraping(city, neighborhood, rooms):
    """
    Scrape the Tax Authority's real estate information website.

    The official site is: https://www.gov.il/he/service/real_estate_information

    NOTE: The Tax Authority site has bot protection (403 Forbidden) and requires:
    1. Proper browser headers and cookies
    2. JavaScript execution (the form uses ASP.NET ViewState)
    3. Handling of CAPTCHA if present

    For production implementation, consider:
    - Using Selenium with headless browser for JavaScript execution
    - Implementing proper cookie/session management
    - Handling ASP.NET ViewState and EventValidation tokens
    - Respecting robots.txt and rate limiting

    Alternative: Contact the Tax Authority for official API access.

    Current implementation: Returns None (scraping not active).
    """
    print("[Tax Authority] Web scraping not implemented due to bot protection")
    print("[Tax Authority] Recommendation: Use official API or Selenium with headless browser")

    return None


def calculate_transaction_stats(transactions, property_data):
    """Calculate statistics from historical transactions."""
    if not transactions:
        return {
            "avgPrice": "לא זמין",
            "avgPricePerSqm": "לא זמין",
            "minPrice": "לא זמין",
            "maxPrice": "לא זמין",
            "medianPrice": "לא זמין",
            "note": "אין מספיק נתונים להשוואה - נדרש מספר עסקאות מינימלי"
        }

    prices = [float(re.sub(r'[^\d.]', '', str(t.get("price", "0"))))
              for t in transactions if t.get("price")]
    prices = [p for p in prices if p > 0]

    if not prices:
        return {"note": "לא נמצאו עסקאות עם מחירים"}

    prices.sort()
    avg_price = sum(prices) / len(prices)
    median_price = prices[len(prices) // 2]

    return {
        "avgPrice": f"{int(avg_price):,} ₪",
        "minPrice": f"{int(min(prices)):,} ₪",
        "maxPrice": f"{int(max(prices)):,} ₪",
        "medianPrice": f"{int(median_price):,} ₪",
        "transactionCount": len(transactions)
    }


def generate_insights(property_data, statistics):
    """Generate insights based on property and market data."""
    insights = []

    # Add basic info
    city = property_data.get("city", "")
    neighborhood = property_data.get("neighborhood", "")

    if city and neighborhood:
        insights.append(f"הנכס ממוקם ב{neighborhood}, {city}.")
    elif city:
        insights.append(f"הנכס ממוקם ב{city}.")

    # Market comparison
    if statistics.get("transactionCount", 0) > 0:
        insights.append(f"נמצאו {statistics['transactionCount']} עסקאות דומות באזור.")
        insights.append(f"המחיר הממוצע באזור: {statistics.get('avgPrice', 'לא זמין')}.")
        insights.append(f"טווח מחירים: {statistics.get('minPrice', '')} - {statistics.get('maxPrice', '')}.")
    else:
        insights.append("לא נמצאו עסקאות דומות באזור לצורך השוואה.")
        insights.append("מומלץ לבדוק מחירים דומים באזורים סמוכים או להתייעץ עם שמאי מקרקעין.")

    return " ".join(insights)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8181)
