"""Per-domain robots.txt cache for the archiver daemon."""

from __future__ import annotations

import threading
from collections import OrderedDict

import requests

from arkivari.robots import RobotsPolicy


class RobotsCache:
    def __init__(self, user_agent: str, max_domains: int = 64) -> None:
        self._user_agent = user_agent
        self._max_domains = max_domains
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, RobotsPolicy] = OrderedDict()

    def can_fetch(self, url: str) -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        base = f"{parsed.scheme}://{parsed.netloc.lower()}"
        policy = self._get(base)
        return policy.can_fetch(url)

    def _get(self, base_url: str) -> RobotsPolicy:
        with self._lock:
            if base_url in self._cache:
                self._cache.move_to_end(base_url)
                return self._cache[base_url]
        session = requests.Session()
        policy = RobotsPolicy(base_url, self._user_agent, session=session)
        policy.load()
        with self._lock:
            self._cache[base_url] = policy
            self._cache.move_to_end(base_url)
            while len(self._cache) > self._max_domains:
                self._cache.popitem(last=False)
        return policy