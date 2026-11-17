import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse as up

from config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  url TEXT, title TEXT,
  created_at INTEGER, collected_at INTEGER,
  sources TEXT DEFAULT '[]',
  engagement INTEGER DEFAULT 0,
  snippet TEXT DEFAULT '',
  status TEXT DEFAULT 'new',          -- new | triaged | rejected | done | low | error
  relevant INTEGER, category TEXT, interest INTEGER,
  summary TEXT, value_score INTEGER,
  embedding BLOB, done_at INTEGER,
  error TEXT, attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at INTEGER, finished_at INTEGER,
  mode TEXT, state TEXT, pid INTEGER,
  control TEXT DEFAULT '',
  status TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(init=True):
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    if init:
        c.executescript(SCHEMA)
    return c


def meta_get(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (k, json.dumps(v)))
    c.commit()


_DROP_PARAMS = ("utm_", "fbclid", "gclid", "mc_", "ref_src", "ref")


def norm_url(u):
    if not u:
        return ""
    try:
        p = up.urlsplit(u.strip())
    except ValueError:
        return u.strip()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if host in ("arxiv.org", "export.arxiv.org"):
        m = re.search(r"(\d{4}\.\d{4,5})", path)
        if m:
            return "arxiv.org/abs/" + m.group(1)
    q = [(k, v) for k, v in up.parse_qsl(p.query)
         if not k.lower().startswith(_DROP_PARAMS)]
    path = path.rstrip("/") or "/"
    return host + path + ("?" + up.urlencode(q) if q else "")


def item_key(cand):
    n = norm_url(cand.get("url"))
    if n and not n.startswith(("news.ycombinator.com", "lobste.rs", "reddit.com")):
        return "u:" + hashlib.sha1(n.encode()).hexdigest()[:16]
    return cand["key"]


def upsert_candidate(c, cand):
    """Insert a collected candidate, or merge it into an existing item. Returns True if new."""
    iid = item_key(cand)
    src = {k: cand.get(k) for k in ("source", "source_id", "discuss_url", "engagement",
                                    "comments", "rank") if cand.get(k) not in (None, "")}
    row = c.execute("SELECT sources, engagement, snippet FROM items WHERE id=?", (iid,)).fetchone()
    now = int(time.time())
    if row is None:
        c.execute("""INSERT INTO items(id,url,title,created_at,collected_at,sources,engagement,snippet)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (iid, cand.get("url") or "", cand["title"].strip(), int(cand["created_at"]), now,
                   json.dumps([src]), int(cand.get("engagement") or 0), cand.get("snippet") or ""))
        return True
    sources = json.loads(row["sources"])
    for s in sources:
        if s.get("source") == src.get("source") and s.get("source_id") == src.get("source_id"):
            s.update(src)
            break
    else:
        sources.append(src)
    snippet = row["snippet"] or ""
    if len(cand.get("snippet") or "") > len(snippet):
        snippet = cand["snippet"]
    c.execute("UPDATE items SET sources=?, engagement=MAX(engagement, ?), snippet=? WHERE id=?",
              (json.dumps(sources), int(cand.get("engagement") or 0), snippet, iid))
    return False
