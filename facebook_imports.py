"""
Manually imported Facebook posts.

Automated Facebook scraping (logged-in session cookies + facebook-scraper) breaks
Meta's Terms of Service, so it was removed. Instead, a user pastes the text of a
post they are viewing, and the existing Hebrew parser turns it into a listing.
Imports are kept in a local JSON file (fb_imports.json, git-ignored).
"""

import datetime
import hashlib
import json
import os
import threading

import facebook_parser

STORE_PATH = os.environ.get(
    "FB_IMPORTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fb_imports.json"),
)
MAX_TEXT_LEN = 5000

_lock = threading.Lock()


def _load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"[Facebook] Could not read {STORE_PATH}: {e}")
        return []


def _save(items):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE_PATH)


def import_post(text, url=""):
    """
    Parse and store a pasted post. Returns (item, error). `error` is a Hebrew
    message when the text isn't a usable listing (no property keywords / price).
    """
    text = (text or "").strip()
    url = (url or "").strip()
    if not text:
        return None, "יש להדביק את טקסט הפוסט"
    if len(text) > MAX_TEXT_LEN:
        return None, f"הטקסט ארוך מדי (מקסימום {MAX_TEXT_LEN} תווים)"
    if url and not url.startswith(("https://www.facebook.com/", "https://facebook.com/",
                                   "https://m.facebook.com/", "https://fb.com/")):
        return None, "הקישור חייב להיות קישור לפייסבוק"

    post_id = "fbimp_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    item = facebook_parser.parse_post({
        "post_id": post_id,
        "post_url": url,
        "text": text,
        "time": datetime.datetime.now(),
    })
    if item is None:
        return None, "לא זוהתה מודעת נדל\"ן עם מחיר בטקסט"

    with _lock:
        items = [it for it in _load() if it.get("linkToken") != post_id]
        items.insert(0, item)
        _save(items)
    return item, None


def list_imports():
    with _lock:
        return _load()


def delete_import(link_token):
    with _lock:
        items = _load()
        kept = [it for it in items if it.get("linkToken") != link_token]
        if len(kept) == len(items):
            return False
        _save(kept)
        return True


def mock_items():
    """Parsed sample posts for local testing (enabled with FB_MOCK=1)."""
    from mock_facebook_data import MOCK_FACEBOOK_POSTS
    return facebook_parser.parse_posts_batch(MOCK_FACEBOOK_POSTS)
