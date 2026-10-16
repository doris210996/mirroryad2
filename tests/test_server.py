import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FB_IMPORTS_FILE"] = os.path.join(tempfile.mkdtemp(), "fb_imports.json")

import server  # noqa: E402
from polite_fetcher import AccessDenied  # noqa: E402


def yad2_item(token, price, hood="פלורנטין"):
    return {"token": token, "price": price, "address": {"neighborhood": {"text": hood}},
            "additionalDetails": {"squareMeter": 50}}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        if os.path.exists(server.facebook_imports.STORE_PATH):
            os.remove(server.facebook_imports.STORE_PATH)

    def test_yad2_url_never_contains_price_param(self):
        urls = []

        def fake_next_data(url):
            urls.append(url)
            return {"props": {"pageProps": {"dehydratedState": {"queries": [
                {"queryKey": ["feed-rent"], "state": {"data": {
                    "private": [yad2_item("a", 4000), yad2_item("b", 9000)],
                    "pagination": {"totalPages": 1}}}}]}}}}

        with mock.patch.object(server, "fetch_next_data", fake_next_data):
            res = self.client.get("/api/realestate/search?dealType=rent&neighborhood=פלורנטין"
                                  "&minPrice=3000&maxPrice=5000&sources=yad2")
        data = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(urls and all("price=" not in u for u in urls))
        self.assertEqual([it["linkToken"] for it in data["items"]], ["a"])  # price filtered locally

    def test_blocked_source_reports_message(self):
        def blocked(url):
            raise AccessDenied("blocked", "www.yad2.co.il", server.time.monotonic() + 600)

        with mock.patch.object(server, "fetch_next_data", blocked):
            res = self.client.get("/api/realestate/search?dealType=rent&neighborhood=פלורנטין&sources=yad2")
        self.assertEqual(res.status_code, 502)
        self.assertIn("לא עוקפת", res.get_json()["error"])

    def test_facebook_import_flow(self):
        res = self.client.post("/api/facebook/imports", json={"text": "שלום לכולם"})
        self.assertEqual(res.status_code, 422)

        res = self.client.post("/api/facebook/imports", json={
            "text": "להשכרה בפלורנטין תל אביב 2 חדרים 50 מ\"ר 4,200 ₪",
            "url": "https://www.facebook.com/groups/1/posts/2"})
        self.assertEqual(res.status_code, 201)
        token = res.get_json()["linkToken"]

        res = self.client.get("/api/realestate/search?dealType=rent&neighborhood=פלורנטין"
                              "&minPrice=3000&maxPrice=5000&sources=facebook")
        self.assertEqual([it["linkToken"] for it in res.get_json()["items"]], [token])

        self.assertEqual(self.client.delete(f"/api/facebook/imports/{token}").status_code, 204)
        self.assertEqual(self.client.get("/api/facebook/imports").get_json(), [])

    def test_fetcher_status(self):
        data = self.client.get("/api/fetcher/status").get_json()
        self.assertTrue(data["userAgent"].startswith("MirrorYad2Bot/"))


if __name__ == "__main__":
    unittest.main()
