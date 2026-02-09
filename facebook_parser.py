"""
Facebook Group Post Parser for Real Estate Listings
Extracts structured data from unstructured Hebrew Facebook posts.
"""

import re
from typing import Optional, Dict, Any

# City codes from Yad2 (common cities for matching)
CITY_CODES = {
    "תל אביב": "5000",
    "ירושלים": "3000",
    "חיפה": "4000",
    "ראשון לציון": "8600",
    "פתח תקווה": "7900",
    "אשדוד": "1200",
    "נתניה": "7400",
    "באר שבע": "9000",
    "בני ברק": "6100",
    "חולון": "6200",
    "רמת גן": "8300",
    "אשקלון": "1300",
    "רחובות": "8500",
    "בת ים": "6300",
    "כפר סבא": "6900",
    "הרצליה": "6400",
    "מודיעין": "1200",
    "רעננה": "8400",
    "קריית אתא": "2610",
    "גבעתיים": "6400",
    "רמלה": "8200",
    "רמת השרון": "8700",
    "הוד השרון": "6600",
}

# Broker keywords for detection
BROKER_KEYWORDS = [
    "תיווך",
    "משרד נדל\"ן",
    "משרד נדלן",
    "מתווך",
    "מתווכת",
    "רילוקיישן",
    "relocation",
    "תיווך ללא דמי",
    "ללא דמי תיווך",
]

# Property keywords to filter relevant posts
PROPERTY_KEYWORDS = [
    "להשכרה",
    "דירה",
    "דירת",
    "חדרים",
    "חדר",
    "חד'",
    "מ\"ר",
    "מר",
    "קומה",
    "משופצת",
    "מרוהטת",
    "apartment",
    "rent",
    "rooms",
]


def contains_property_keywords(text: str) -> bool:
    """
    Check if text contains property-related keywords.
    Returns True if it looks like a real estate listing.
    """
    if not text:
        return False

    text_lower = text.lower()

    # Check for at least one property keyword
    return any(keyword.lower() in text_lower for keyword in PROPERTY_KEYWORDS)


def extract_price(text: str) -> Optional[str]:
    """
    Extract price from text in various Hebrew formats.
    Examples: "5,200 ₪", "5200 שקלים", "5200ש\"ח", "5,200"
    """
    if not text:
        return None

    # Pattern for price: digits with optional thousands separators, followed by currency
    patterns = [
        r'(\d{1,3}(?:,\d{3})+)\s*(?:₪|שקלים|שח|ש"ח)',  # 5,200 ₪
        r'(\d{4,6})\s*(?:₪|שקלים|שח|ש"ח)',  # 5200 ₪
        r'(?:^|\s)(\d{4,6})(?:\s|$)',  # 5200 (standalone number)
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            price_str = match.group(1)
            # Remove commas and convert to int to validate
            try:
                price_num = int(price_str.replace(',', ''))
                # Reasonable price range: 1,000 - 50,000 ₪ per month
                if 1000 <= price_num <= 50000:
                    return f"{price_num:,} ₪"
            except ValueError:
                continue

    return None


def extract_rooms(text: str) -> Optional[str]:
    """
    Extract number of rooms from text.
    Examples: "3.5 חדרים", "3 חד'", "4 חדרים"
    """
    if not text:
        return None

    # Pattern for rooms: number (with optional decimal) followed by rooms indicator
    patterns = [
        r'(\d+\.?\d*)\s*חדרים',
        r'(\d+\.?\d*)\s*חדר',
        r'(\d+\.?\d*)\s*חד[\'׳]',
        r'(\d+\.?\d*)\s*ח[\'׳]',
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            rooms_str = match.group(1)
            try:
                rooms_num = float(rooms_str)
                # Reasonable range: 0.5 - 10 rooms
                if 0.5 <= rooms_num <= 10:
                    # Format as int if whole number, otherwise keep decimal
                    if rooms_num == int(rooms_num):
                        return f"{int(rooms_num)} חדרים"
                    else:
                        return f"{rooms_num} חדרים"
            except ValueError:
                continue

    return None


def extract_sqm(text: str) -> Optional[str]:
    """
    Extract square meters from text.
    Examples: "80 מ\"ר", "80 מר", "80 מטר"
    """
    if not text:
        return None

    # Pattern for square meters
    patterns = [
        r'(\d+)\s*מ"ר',
        r'(\d+)\s*מר',
        r'(\d+)\s*מטר',
        r'(\d+)\s*sqm',
        r'(\d+)\s*m2',
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            sqm_str = match.group(1)
            try:
                sqm_num = int(sqm_str)
                # Reasonable range: 20 - 500 sqm
                if 20 <= sqm_num <= 500:
                    return f"{sqm_num} מ\"ר"
            except ValueError:
                continue

    return None


def extract_city(text: str) -> Optional[str]:
    """
    Extract city name by matching against known cities.
    Returns the city name if found.
    """
    if not text:
        return None

    text_lower = text.lower()

    # Check each known city
    for city_name in CITY_CODES.keys():
        if city_name.lower() in text_lower:
            return city_name

    return None


def detect_broker(text: str) -> bool:
    """
    Detect if post is from a broker based on keywords.
    Returns True if broker indicators found.
    """
    if not text:
        return False

    text_lower = text.lower()

    return any(keyword.lower() in text_lower for keyword in BROKER_KEYWORDS)


def parse_post(post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse a Facebook post and extract structured real estate data.

    Args:
        post: Dictionary from facebook-scraper with keys like 'text', 'post_url', 'time', etc.

    Returns:
        Unified schema dictionary with extracted fields, or None if not a valid listing.
    """
    # Get post text
    text = post.get('text', '')
    if not text:
        return None

    # Filter: Must contain property keywords
    if not contains_property_keywords(text):
        return None

    # Extract price (REQUIRED - filter out posts without price)
    price = extract_price(text)
    if not price:
        return None

    # Extract other fields (optional)
    rooms = extract_rooms(text)
    sqm = extract_sqm(text)
    city = extract_city(text)
    is_broker = detect_broker(text)

    # Build unified schema
    item = {
        "source": "facebook",
        "linkToken": post.get('post_id', ''),
        "link": post.get('post_url', ''),
        "price": price,
        "mainImage": post.get('image', ''),  # First image if available
        "dateAdded": post.get('time', '').isoformat() if post.get('time') else '',
        "title": text[:100] + '...' if len(text) > 100 else text,  # First 100 chars as title
        "description": text,

        # Optional fields
        "rooms": rooms,
        "sqm": sqm,
        "city": city,

        # Metadata
        "broker": is_broker,

        # Facebook-specific fields
        "likes": post.get('likes', 0),
        "comments": post.get('comments', 0),

        # Fields that may not be available (set defaults)
        "floor": None,
        "street": None,
        "neighborhood": None,
    }

    return item


def parse_posts_batch(posts: list) -> list:
    """
    Parse a batch of Facebook posts and return list of valid listings.

    Args:
        posts: List of post dictionaries from facebook-scraper

    Returns:
        List of parsed item dictionaries (only valid listings)
    """
    results = []

    for post in posts:
        try:
            parsed = parse_post(post)
            if parsed:
                results.append(parsed)
        except Exception as e:
            # Skip posts that fail to parse
            print(f"Warning: Failed to parse Facebook post: {e}")
            continue

    return results
