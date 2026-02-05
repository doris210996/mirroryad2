import os
import re
import json
import datetime

from flask import Flask, send_from_directory, jsonify, request
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

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
# Yad2 scraper using curl_cffi (impersonates Chrome TLS fingerprint)
# ---------------------------------------------------------------------------
_yad2_session = None


def get_yad2_session():
    global _yad2_session
    if _yad2_session is None:
        _yad2_session = cffi_requests.Session(impersonate="chrome")
        _yad2_session.get("https://www.yad2.co.il/", timeout=15)
    return _yad2_session


def reset_yad2_session():
    global _yad2_session
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
        reset_yad2_session()
        # Retry once with a fresh session
        session = get_yad2_session()
        resp = session.get(url, timeout=15)
        match = re.search(
            r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text, re.DOTALL
        )
    if not match:
        reset_yad2_session()
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
                "squareMeters": details.get("squaremeter"),
                "price": item.get("price", ""),
                "updatedAt": item.get("metaData", {}).get("updatedAt", ""),
            })
    return items


def remove_duplicates(items):
    seen = set()
    result = []
    for item in items:
        token = item["linkToken"]
        if token and token not in seen:
            seen.add(token)
            result.append(item)
    return result


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
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/realestate/search")
def realestate_search():
    deal_type = request.args.get("dealType", "rent")
    settlement = request.args.get("settlement", "")
    min_price = request.args.get("minPrice", "0")
    max_price = request.args.get("maxPrice", "100000000")
    page = request.args.get("page", "1")

    if not settlement:
        return jsonify({"error": "settlement is required"}), 400

    try:
        page = int(page)
        min_price = int(min_price)
        max_price = int(max_price)
    except ValueError:
        return jsonify({"error": "Invalid numeric parameter"}), 400

    city_code = resolve_city_code(settlement)
    if not city_code:
        return jsonify({"error": f"Settlement '{settlement}' not found. Use a city code or one of: {', '.join(sorted(CITY_CODES.keys()))}"}), 404

    try:
        feed = fetch_yad2_page(deal_type, city_code, min_price, max_price, page)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data from Yad2: {e}"}), 502

    pagination = feed.get("pagination", {})
    total_pages = pagination.get("totalPages", 1)
    items = extract_items(feed)
    items = remove_duplicates(items)

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
                    "page": page,
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
        feed = fetch_yad2_page(
            params["dealType"], params["cityCode"],
            params["minPrice"], params["maxPrice"], params["page"],
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data from Yad2: {e}"}), 502

    pagination = feed.get("pagination", {})
    total_pages = pagination.get("totalPages", 1)
    new_items = extract_items(feed)
    new_items = remove_duplicates(new_items)
    changed_items = detect_changes(doc["items"], new_items)

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8181)
