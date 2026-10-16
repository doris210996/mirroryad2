"""
Polite, compliant HTTP fetcher for third-party listing sites.

This replaces the old "anti-bot evasion" layer (browser-fingerprint spoofing,
proxy IP rotation, captcha dodging). Instead of hiding from a site's bot
protection, it follows the site's published rules and backs off when told to:

  * Honest identity  — a descriptive User-Agent with a contact address, no
                       browser impersonation, no proxies, no cookie reuse.
  * robots.txt       — full RFC 9309 matching (wildcards, `$`, longest match),
                       cached for at most 24h. Disallowed URLs are never fetched.
  * Rate limiting    — one request per host per `min_interval` seconds, or the
                       site's Crawl-delay if that is larger.
  * Caching          — successful responses are reused for `cache_ttl` seconds,
                       so repeated searches don't hit the site again.
  * Backoff          — 429/503 honour Retry-After, with bounded retries.
  * Circuit breaker  — a 401/403 or a bot-challenge page means "no": the host is
                       paused for `block_cooldown` seconds and the error is
                       reported to the caller. It is never worked around.
"""

import json
import os
import random
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import requests

BOT_NAME = "MirrorYad2Bot"
BOT_VERSION = "2.0"
BOT_INFO_URL = os.environ.get("FETCHER_INFO_URL", "https://github.com/doris210996/mirroryad2")
BOT_CONTACT = os.environ.get("FETCHER_CONTACT", "")

# Markers of bot-challenge / captcha interstitials served instead of content.
CHALLENGE_MARKERS = (
    "shieldsquare", "perfdrive", "radware bot manager", "validate.perfdrive",
    "px-captcha", "cf-chl", "challenge-platform", "g-recaptcha", "h-captcha",
)

ROBOTS_TTL = 24 * 3600          # RFC 9309: don't use a cached robots.txt longer than 24h
ROBOTS_UNREACHABLE_TTL = 600    # re-check an unreachable robots.txt after 10 min


class FetchError(RuntimeError):
    """The page could not be fetched (network error, bad status, bad content)."""


class RobotsDisallowed(FetchError):
    """robots.txt does not allow this URL for our user agent."""


class AccessDenied(FetchError):
    """The site refused automated access (401/403/challenge/long Retry-After)."""

    def __init__(self, message, host, retry_at):
        super().__init__(message)
        self.host = host
        self.retry_at = retry_at


@dataclass
class Response:
    url: str
    status: int
    headers: Dict[str, str]
    text: str
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# robots.txt (RFC 9309)
# ---------------------------------------------------------------------------
def _pattern_to_regex(pattern):
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(body + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    rules: List[Tuple[bool, str, "re.Pattern"]] = field(default_factory=list)  # (allow, raw, regex)
    crawl_delay: Optional[float] = None
    allow_all: bool = False
    disallow_all: bool = False

    @classmethod
    def parse(cls, text, agent):
        """Parse robots.txt and keep the group that applies to `agent`."""
        token = agent.split("/", 1)[0].strip().lower()   # product token, e.g. "mirroryad2bot"
        groups = []          # [(agents, lines)]
        current = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = ([], [])
                    groups.append(current)
                current[0].append(value.lower())
                last_was_agent = True
            elif current is not None:
                current[1].append((key, value))
                last_was_agent = False

        specific = [g for g in groups if token in g[0]]
        chosen = specific or [g for g in groups if "*" in g[0]]

        result = cls()
        for _, lines in chosen:
            for key, value in lines:
                if key in ("allow", "disallow") and value:
                    path = unquote(value)
                    result.rules.append((key == "allow", path, _pattern_to_regex(path)))
                elif key == "crawl-delay":
                    try:
                        result.crawl_delay = float(value)
                    except ValueError:
                        pass
        return result

    def can_fetch(self, path_and_query):
        if self.disallow_all:
            return False
        if self.allow_all or path_and_query == "/robots.txt":
            return True
        target = unquote(path_and_query)
        best_len, best_allow = -1, True
        for allow, raw, regex in self.rules:
            if regex.match(target):
                length = len(raw)
                # Longest match wins; on a tie, allow wins (RFC 9309 §2.2.2).
                if length > best_len or (length == best_len and allow):
                    best_len, best_allow = length, allow
        return best_allow


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------
@dataclass
class _HostState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_slot: float = 0.0
    robots: Optional[RobotsRules] = None
    robots_expires: float = 0.0
    blocked_until: float = 0.0
    block_reason: str = ""
    requests: int = 0
    cache_hits: int = 0
    robots_refusals: int = 0
    denials: int = 0


Transport = Callable[[str, Dict[str, str], float], Tuple[int, Dict[str, str], str]]


def _requests_transport():
    local = threading.local()

    def transport(url, headers, timeout):
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return resp.status_code, dict(resp.headers), resp.text

    return transport


class PoliteFetcher:
    def __init__(
        self,
        user_agent=None,
        min_interval=float(os.environ.get("FETCHER_MIN_INTERVAL", "2.0")),
        cache_ttl=float(os.environ.get("FETCHER_CACHE_TTL", "600")),
        cache_size=int(os.environ.get("FETCHER_CACHE_SIZE", "500")),
        block_cooldown=float(os.environ.get("FETCHER_BLOCK_COOLDOWN", "1800")),
        max_retries=2,
        max_retry_wait=60.0,
        timeout=20.0,
        transport: Optional[Transport] = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if user_agent is None:
            user_agent = os.environ.get("FETCHER_USER_AGENT") or self._default_user_agent()
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.cache_size = cache_size
        self.block_cooldown = block_cooldown
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.timeout = timeout
        self._transport = transport or _requests_transport()
        self._clock = clock
        self._sleep = sleep
        self._hosts: Dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()
        self._cache: "OrderedDict[str, Tuple[float, Response]]" = OrderedDict()
        self._cache_lock = threading.Lock()

    @staticmethod
    def _default_user_agent():
        contact = f"; {BOT_CONTACT}" if BOT_CONTACT else ""
        return f"{BOT_NAME}/{BOT_VERSION} (+{BOT_INFO_URL}{contact})"

    # -- public API ---------------------------------------------------------
    def get(self, url, validate: Optional[Callable[[str], bool]] = None, use_cache=True) -> Response:
        """
        Fetch `url` politely. `validate(text)` should return True when the body is
        the expected content; a failed validation that looks like a bot challenge
        is treated as an access denial.
        """
        if use_cache:
            cached = self._cache_get(url)
            if cached is not None:
                self._host(urlsplit(url).netloc).cache_hits += 1
                return cached

        parts = urlsplit(url)
        host = parts.netloc
        state = self._host(host)
        self._check_blocked(host, state)

        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        rules = self._robots_for(parts.scheme, host, state)
        if not rules.can_fetch(path):
            state.robots_refusals += 1
            raise RobotsDisallowed(f"robots.txt of {host} disallows {path}")

        resp = self._request_with_retries(url, host, state, rules)

        if resp.status in (401, 403):
            self._block(host, state, f"HTTP {resp.status}")
        if resp.status != 200:
            raise FetchError(f"{host} returned HTTP {resp.status}")
        valid = validate(resp.text) if validate else True
        if self._looks_like_challenge(resp.text, valid):
            self._block(host, state, "bot challenge / captcha page")
        if not valid:
            raise FetchError(f"Unexpected content from {host}")

        if use_cache:
            self._cache_put(url, resp)
        return resp

    def status(self):
        now = self._clock()
        hosts = {}
        with self._hosts_lock:
            items = list(self._hosts.items())
        for host, st in items:
            hosts[host] = {
                "requests": st.requests,
                "cacheHits": st.cache_hits,
                "robotsRefusals": st.robots_refusals,
                "denials": st.denials,
                "blocked": st.blocked_until > now,
                "blockedForSec": max(0, round(st.blocked_until - now)),
                "blockReason": st.block_reason if st.blocked_until > now else "",
                "crawlDelay": st.robots.crawl_delay if st.robots else None,
            }
        return {
            "userAgent": self.user_agent,
            "minIntervalSec": self.min_interval,
            "cacheTtlSec": self.cache_ttl,
            "cacheEntries": len(self._cache),
            "hosts": hosts,
        }

    def blocked_for(self, url):
        """Seconds until the host of `url` may be contacted again (0 if not blocked)."""
        st = self._host(urlsplit(url).netloc)
        return max(0.0, st.blocked_until - self._clock())

    # -- internals ----------------------------------------------------------
    def _host(self, host):
        with self._hosts_lock:
            st = self._hosts.get(host)
            if st is None:
                st = self._hosts[host] = _HostState()
            return st

    def _check_blocked(self, host, state):
        remaining = state.blocked_until - self._clock()
        if remaining > 0:
            raise AccessDenied(
                f"{host} refused automated access ({state.block_reason}); "
                f"pausing for {int(remaining)}s instead of retrying",
                host, state.blocked_until,
            )

    def _block(self, host, state, reason, seconds=None):
        state.denials += 1
        state.block_reason = reason
        state.blocked_until = self._clock() + (seconds if seconds is not None else self.block_cooldown)
        self._check_blocked(host, state)

    def _wait_turn(self, state, interval):
        with state.lock:
            now = self._clock()
            slot = max(now, state.next_slot)
            state.next_slot = slot + interval
        if slot > now:
            self._sleep(slot - now)

    def _raw_get(self, url, state, interval):
        self._wait_turn(state, interval)
        state.requests += 1
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "he,en;q=0.8",
        }
        try:
            status, resp_headers, text = self._transport(url, headers, self.timeout)
        except Exception as e:
            raise FetchError(f"Network error fetching {url}: {e}") from e
        return Response(url, status, {k.lower(): v for k, v in resp_headers.items()}, text)

    def _robots_for(self, scheme, host, state):
        if state.robots is not None and self._clock() < state.robots_expires:
            return state.robots
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            resp = self._raw_get(robots_url, state, self.min_interval)
            if 200 <= resp.status < 300:
                rules, ttl = RobotsRules.parse(resp.text, self.user_agent), ROBOTS_TTL
            elif 400 <= resp.status < 500:
                rules, ttl = RobotsRules(allow_all=True), ROBOTS_TTL      # RFC 9309 §2.3.1.3
            else:
                rules, ttl = RobotsRules(disallow_all=True), ROBOTS_UNREACHABLE_TTL  # §2.3.1.4
        except FetchError:
            rules, ttl = RobotsRules(disallow_all=True), ROBOTS_UNREACHABLE_TTL
        state.robots = rules
        state.robots_expires = self._clock() + ttl
        return rules

    def _request_with_retries(self, url, host, state, rules):
        interval = max(self.min_interval, rules.crawl_delay or 0)
        attempt = 0
        while True:
            try:
                resp = self._raw_get(url, state, interval)
            except FetchError:
                if attempt >= self.max_retries:
                    raise
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            if resp.status not in (429, 503):
                return resp
            wait = self._retry_after(resp.headers.get("retry-after"))
            if wait is not None and wait > self.max_retry_wait:
                self._block(host, state, f"HTTP {resp.status} with Retry-After {int(wait)}s", seconds=wait)
            if attempt >= self.max_retries:
                self._block(host, state, f"HTTP {resp.status} (rate limited)",
                            seconds=wait if wait is not None else None)
            self._sleep(wait if wait is not None else self._backoff(attempt))
            attempt += 1

    @staticmethod
    def _backoff(attempt):
        return (2 ** attempt) * 2.0 + random.uniform(0, 1.0)

    @staticmethod
    def _retry_after(value):
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _looks_like_challenge(text, valid):
        if valid:
            return False
        head = text[:50000].lower()
        return any(marker in head for marker in CHALLENGE_MARKERS)

    def _cache_get(self, url):
        with self._cache_lock:
            entry = self._cache.get(url)
            if entry is None:
                return None
            expires, resp = entry
            if self._clock() >= expires:
                del self._cache[url]
                return None
            self._cache.move_to_end(url)
            return Response(resp.url, resp.status, resp.headers, resp.text, from_cache=True)

    def _cache_put(self, url, resp):
        if self.cache_ttl <= 0:
            return
        with self._cache_lock:
            self._cache[url] = (self._clock() + self.cache_ttl, resp)
            self._cache.move_to_end(url)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)


_default = None
_default_lock = threading.Lock()


def default_fetcher():
    """Process-wide shared fetcher, so rate limits and blocks apply across callers."""
    global _default
    with _default_lock:
        if _default is None:
            _default = PoliteFetcher()
        return _default
