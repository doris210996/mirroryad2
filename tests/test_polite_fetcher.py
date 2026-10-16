import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polite_fetcher import (  # noqa: E402
    AccessDenied, FetchError, PoliteFetcher, RobotsDisallowed, RobotsRules,
)

YAD2_ROBOTS = """
User-agent: *
Allow: /realestate/rent?shelter=1
Disallow: /api/
Disallow: /*?*price=
Disallow: /*?*page=0
Disallow: /personal/
"""


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Maps URL -> list of (status, headers, text); the last response repeats."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        if url not in self.routes:
            return 404, {}, ""
        queue = self.routes[url]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result


def make(routes, **kw):
    clock = FakeClock()
    transport = FakeTransport(routes)
    f = PoliteFetcher(user_agent="MirrorYad2Bot/2.0 (+test)", transport=transport,
                      clock=clock.time, sleep=clock.sleep, **kw)
    return f, transport, clock


ROBOTS = "https://www.yad2.co.il/robots.txt"
PAGE = "https://www.yad2.co.il/realestate/rent?city=5000&page=1"
OK_PAGE = (200, {}, '<script id="__NEXT_DATA__">{}</script>')


class RobotsRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RobotsRules.parse(YAD2_ROBOTS, "MirrorYad2Bot/2.0")

    def test_wildcard_query_rules(self):
        self.assertFalse(self.rules.can_fetch("/realestate/rent?city=5000&price=0-5000&page=1"))
        self.assertFalse(self.rules.can_fetch("/realestate/rent?city=5000&page=0"))
        self.assertTrue(self.rules.can_fetch("/realestate/rent?city=5000&page=1"))
        self.assertTrue(self.rules.can_fetch("/realestate/item/abc123"))

    def test_prefix_and_allow(self):
        self.assertFalse(self.rules.can_fetch("/api/realestate/item/1"))
        self.assertTrue(self.rules.can_fetch("/realestate/rent?shelter=1"))

    def test_specific_group_wins_and_end_anchor(self):
        rules = RobotsRules.parse(
            "User-agent: *\nDisallow: /\n\nUser-agent: mirroryad2bot\nDisallow: /*.pdf$\nCrawl-delay: 7\n",
            "MirrorYad2Bot/2.0 (+x)")
        self.assertTrue(rules.can_fetch("/realestate"))
        self.assertFalse(rules.can_fetch("/docs/a.pdf"))
        self.assertTrue(rules.can_fetch("/docs/a.pdf?x=1"))
        self.assertEqual(rules.crawl_delay, 7.0)

    def test_longest_match_and_tie(self):
        rules = RobotsRules.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\nAllow: /c\nDisallow: /c\n", "bot")
        self.assertTrue(rules.can_fetch("/a/b/c"))
        self.assertFalse(rules.can_fetch("/a/x"))
        self.assertTrue(rules.can_fetch("/c"))  # tie -> allow


class PoliteFetcherTest(unittest.TestCase):
    def test_honest_user_agent_and_cache(self):
        f, t, _ = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [OK_PAGE]})
        r1 = f.get(PAGE)
        r2 = f.get(PAGE)
        self.assertFalse(r1.from_cache)
        self.assertTrue(r2.from_cache)
        self.assertEqual([u for u, _ in t.calls], [ROBOTS, PAGE])
        self.assertTrue(all(h["User-Agent"].startswith("MirrorYad2Bot/") for _, h in t.calls))

    def test_robots_disallowed_url_is_never_requested(self):
        url = "https://www.yad2.co.il/realestate/rent?city=5000&price=0-5000"
        f, t, _ = make({ROBOTS: [(200, {}, YAD2_ROBOTS)]})
        with self.assertRaises(RobotsDisallowed):
            f.get(url)
        self.assertNotIn(url, [u for u, _ in t.calls])

    def test_unreachable_robots_means_disallow_all(self):
        f, t, _ = make({ROBOTS: [(503, {}, "")], PAGE: [OK_PAGE]})
        with self.assertRaises(RobotsDisallowed):
            f.get(PAGE)

    def test_missing_robots_means_allow_all(self):
        f, _, _ = make({PAGE: [OK_PAGE]})  # robots -> 404
        self.assertEqual(f.get(PAGE).status, 200)

    def test_rate_limit_spaces_requests(self):
        p2 = PAGE.replace("page=1", "page=2")
        f, _, clock = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [OK_PAGE], p2: [OK_PAGE]},
                           min_interval=2.0)
        start = clock.now
        f.get(PAGE)
        f.get(p2)
        # robots + 2 pages = 3 requests -> at least 2 intervals between them
        self.assertGreaterEqual(clock.now - start, 4.0)

    def test_crawl_delay_is_respected(self):
        p2 = PAGE.replace("page=1", "page=2")
        f, _, clock = make({ROBOTS: [(200, {}, "User-agent: *\nCrawl-delay: 10\n")],
                            PAGE: [OK_PAGE], p2: [OK_PAGE]}, min_interval=1.0)
        f.get(PAGE)
        t0 = clock.now
        f.get(p2)
        self.assertGreaterEqual(clock.now - t0, 10.0)

    def test_challenge_page_opens_circuit_breaker(self):
        challenge = (200, {}, "<html>Radware Bot Manager ... validate.perfdrive.com captcha</html>")
        f, t, _ = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [challenge]}, block_cooldown=600)
        with self.assertRaises(AccessDenied):
            f.get(PAGE, validate=lambda text: "__NEXT_DATA__" in text)
        calls = len(t.calls)
        # While blocked, no further requests go out at all.
        with self.assertRaises(AccessDenied):
            f.get(PAGE.replace("page=1", "page=2"))
        self.assertEqual(len(t.calls), calls)
        self.assertGreater(f.blocked_for(PAGE), 0)

    def test_403_blocks_and_block_expires(self):
        f, t, clock = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [(403, {}, "no"), OK_PAGE]},
                           block_cooldown=100)
        with self.assertRaises(AccessDenied):
            f.get(PAGE)
        clock.now += 101
        self.assertEqual(f.get(PAGE).status, 200)

    def test_429_retry_after_then_success(self):
        f, _, clock = make({ROBOTS: [(200, {}, YAD2_ROBOTS)],
                            PAGE: [(429, {"Retry-After": "5"}, ""), OK_PAGE]})
        self.assertEqual(f.get(PAGE).status, 200)
        self.assertIn(5.0, clock.sleeps)

    def test_long_retry_after_blocks_instead_of_waiting(self):
        f, _, _ = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [(429, {"Retry-After": "3600"}, "")]})
        with self.assertRaises(AccessDenied):
            f.get(PAGE)
        self.assertGreater(f.blocked_for(PAGE), 3000)

    def test_invalid_content_without_challenge_is_plain_error(self):
        f, _, _ = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [(200, {}, "<html>maintenance</html>")]})
        with self.assertRaises(FetchError) as ctx:
            f.get(PAGE, validate=lambda text: "__NEXT_DATA__" in text)
        self.assertNotIsInstance(ctx.exception, AccessDenied)
        self.assertEqual(f.blocked_for(PAGE), 0)

    def test_network_error_retries(self):
        f, t, _ = make({ROBOTS: [(200, {}, YAD2_ROBOTS)], PAGE: [ConnectionError("reset"), OK_PAGE]})
        self.assertEqual(f.get(PAGE).status, 200)


if __name__ == "__main__":
    unittest.main()
