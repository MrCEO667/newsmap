"""Polite HTTP: per-host spacing, retries on 429/5xx, size caps."""
import html
import re
import threading
import time
import urllib.parse as up

import requests

from config import HTTP_UA

_session = requests.Session()
_session.headers["User-Agent"] = HTTP_UA
_lock = threading.Lock()
_last = {}
HOST_INTERVAL = {
    "www.reddit.com": 15.0, "reddit.com": 15.0,   # Reddit rate-limits anonymous readers hard
    "lobste.rs": 1.0, "api.github.com": 1.0,
    "services.nvd.nist.gov": 6.5,                 # NVD allows 5 requests / 30 s without an API key
    "hacker-news.firebaseio.com": 0.1, "hn.algolia.com": 0.3,
}


def _wait(host):
    iv = HOST_INTERVAL.get(host, 0.3)
    while True:
        with _lock:
            now = time.time()
            t = _last.get(host, 0)
            if now - t >= iv:
                _last[host] = now
                return
            delay = iv - (now - t)
        time.sleep(delay)


def get(url, timeout=20, max_bytes=4_000_000, retries=None):
    host = up.urlsplit(url).hostname or ""
    if retries is None:
        retries = 3 if "reddit" in host else 2
    last_exc = None
    for attempt in range(retries + 1):
        _wait(host)
        try:
            r = _session.get(url, timeout=timeout, stream=True)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                r.close()
                time.sleep((75 if "reddit" in host else 20) * (attempt + 1))
                continue
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    break
            r.close()
            r._content = bytes(buf)
            r._content_consumed = True
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise last_exc


def get_json(url, **kw):
    r = get(url, **kw)
    r.raise_for_status()
    return r.json()


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h\d|p)\b[^>]*>")


def strip_html(s):
    if not s:
        return ""
    s = _BLOCK.sub("\n", s)
    s = html.unescape(_TAG.sub("", s))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()
