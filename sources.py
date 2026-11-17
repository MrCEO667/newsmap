"""Collectors (return candidate dicts), article reader and discussion readers."""
import calendar
import datetime as dt
import html
import os
import re
import time
import urllib.parse as up
import xml.etree.ElementTree as ET

import feedparser

import config as C
import net
from net import strip_html


def iso_ts(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return None


def entry_ts(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else None


# ---------------------------------------------------------------- collectors
def collect_hn(since, now):
    out, a = [], since
    while a < now:
        b = min(a + 86400, now)
        d = net.get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=1000"
                         f"&numericFilters=created_at_i>{a},created_at_i<={b},points>={C.HN_MIN_POINTS}")
        for h in d.get("hits", []):
            if not h.get("title"):
                continue
            out.append(dict(url=h.get("url") or "", key=f"hn:{h['objectID']}", title=h["title"],
                            created_at=h["created_at_i"], source="Hacker News",
                            source_id=h["objectID"],
                            discuss_url=f"https://news.ycombinator.com/item?id={h['objectID']}",
                            engagement=h.get("points") or 0, comments=h.get("num_comments") or 0,
                            snippet=strip_html(h.get("story_text") or "")))
        a = b
    return out


def collect_lobsters(since, now):
    out = []
    for page in range(1, 60):
        d = net.get_json(f"https://lobste.rs/t/{C.LOBSTERS_TAGS}/page/{page}.json")
        if not d:
            break
        oldest = None
        for s in d:
            ts = iso_ts(s.get("created_at")) or now
            oldest = ts if oldest is None else min(oldest, ts)
            if ts < since or (s.get("score") or 0) < C.LOBSTERS_MIN_SCORE:
                continue
            sid = s["short_id"]
            disc = s.get("comments_url") or f"https://lobste.rs/s/{sid}"
            out.append(dict(url=s.get("url") or disc, key=f"lob:{sid}", title=s["title"],
                            created_at=ts, source="Lobsters", source_id=sid, discuss_url=disc,
                            engagement=s.get("score") or 0, comments=s.get("comment_count") or 0,
                            snippet=s.get("description_plain") or ""))
        if oldest is not None and oldest < since:
            break
    return out


_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')


def collect_reddit(sub, since, now):
    t = "month" if now - since > 8 * 86400 else "week"
    r = net.get(f"https://www.reddit.com/r/{sub}/top/.rss?t={t}&limit=50")
    r.raise_for_status()
    f = feedparser.parse(r.content)
    out = []
    for rank, e in enumerate(f.entries, 1):
        ts = entry_ts(e)
        if not ts or ts < since:
            continue
        body = e.content[0].value if e.get("content") else e.get("summary", "")
        m = _LINK_RE.search(body)
        link = html.unescape(m.group(1)) if m else e.link
        if "reddit.com" in link or "redd.it" in link:
            link = e.link
        rid = (e.get("id") or e.link).replace("t3_", "")
        text = strip_html(body)
        text = re.sub(r"submitted by\s+/u/\S+.*$", "", text, flags=re.S).strip()
        out.append(dict(url=link, key=f"rd:{rid}", title=e.title, created_at=ts,
                        source=f"r/{sub}", source_id=rid, discuss_url=e.link, rank=rank,
                        snippet=text))
    return out


def collect_hf(since, now):
    out = []
    d = dt.datetime.fromtimestamp(since, dt.timezone.utc).date()
    end = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    while d <= end:
        js = net.get_json(f"https://huggingface.co/api/daily_papers?date={d.isoformat()}")
        for x in js:
            p = x.get("paper", {})
            if (p.get("upvotes") or 0) < C.HF_MIN_UPVOTES:
                continue
            ts = iso_ts(p.get("submittedOnDailyAt")) or iso_ts(x.get("publishedAt")) or now
            snippet = ""
            if p.get("ai_summary"):
                snippet += "AI summary: " + p["ai_summary"] + "\n\n"
            snippet += "Abstract: " + (p.get("summary") or "")
            if p.get("githubRepo"):
                snippet += f"\n\nCode: {p['githubRepo']} ({p.get('githubStars') or 0} stars)"
            out.append(dict(url=f"https://arxiv.org/abs/{p['id']}", key=f"hf:{p['id']}",
                            title=p.get("title") or x.get("title"), created_at=ts,
                            source="HF Papers", source_id=p["id"],
                            discuss_url=f"https://huggingface.co/papers/{p['id']}",
                            engagement=p.get("upvotes") or 0, comments=x.get("numComments") or 0,
                            snippet=snippet))
        d += dt.timedelta(days=1)
    return out


def collect_kev(since, now):
    d = net.get_json(C.KEV_URL, max_bytes=20_000_000)
    out = []
    for v in d.get("vulnerabilities", []):
        ts = iso_ts(v.get("dateAdded", "") + "T12:00:00+00:00")
        if not ts or ts < since:
            continue
        cve = v["cveID"]
        out.append(dict(
            url=f"https://nvd.nist.gov/vuln/detail/{cve}", key=f"kev:{cve}",
            title=f"Exploited in the wild: {v.get('vendorProject')} {v.get('product')} - "
                  f"{v.get('vulnerabilityName')} ({cve})",
            created_at=ts, source="CISA KEV", source_id=cve,
            discuss_url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
            snippet=(f"{v.get('shortDescription', '')}\nRequired action: {v.get('requiredAction', '')}"
                     f"\nKnown ransomware campaign use: {v.get('knownRansomwareCampaignUse', '')}"
                     f"\nCWE: {', '.join(v.get('cwes') or [])}\nNotes: {v.get('notes', '')}")))
    return out


def collect_ghsa(since, now):
    out = []
    url = ("https://api.github.com/advisories?severity=critical&type=reviewed"
           "&per_page=100&sort=published&direction=desc")
    for _ in range(5):
        r = net.get(url)
        r.raise_for_status()
        js = r.json()
        stop = False
        for a in js:
            ts = iso_ts(a.get("published_at"))
            if not ts or ts < since:
                stop = True
                continue
            pk = ", ".join(f"{v['package']['ecosystem']}:{v['package']['name']}"
                           for v in a.get("vulnerabilities") or [] if v.get("package"))
            cvss = (a.get("cvss") or {}).get("score")
            title = a.get("summary") or a["ghsa_id"]
            if a.get("cve_id"):
                title += f" ({a['cve_id']})"
            out.append(dict(url=a["html_url"], key=f"ghsa:{a['ghsa_id']}",
                            title="Critical advisory: " + title, created_at=ts,
                            source="GitHub Advisory", source_id=a["ghsa_id"],
                            discuss_url=a["html_url"],
                            snippet=f"Packages: {pk}\nCVSS: {cvss}\n\n{(a.get('description') or '')[:3000]}"))
        nxt = r.links.get("next", {}).get("url")
        if stop or not nxt:
            break
        url = nxt
    return out


def feed_list():
    """Karpathy-shared 'most popular HN blogs' OPML (cached weekly) + curated AI/security feeds."""
    feeds = list(C.EXTRA_FEEDS)
    xml = None
    fresh = os.path.exists(C.FEEDS_CACHE) and time.time() - os.path.getmtime(C.FEEDS_CACHE) < 7 * 86400
    if not fresh:
        try:
            r = net.get(C.OPML_URL)
            if r.status_code == 200 and b"<opml" in r.content[:500]:
                with open(C.FEEDS_CACHE, "wb") as f:
                    f.write(r.content)
        except Exception:
            pass
    if os.path.exists(C.FEEDS_CACHE):
        with open(C.FEEDS_CACHE, "rb") as f:
            xml = f.read()
    seen = {u.rstrip("/") for _, u in feeds}
    if xml:
        for o in ET.fromstring(xml).iter("outline"):
            u = o.get("xmlUrl")
            if u and u.rstrip("/") not in seen:
                seen.add(u.rstrip("/"))
                feeds.append((o.get("title") or o.get("text") or up.urlsplit(u).hostname, u))
    return feeds


def collect_feed(name, url, since, now):
    r = net.get(url, timeout=20, retries=1)
    r.raise_for_status()
    f = feedparser.parse(r.content)
    out = []
    for e in f.entries[:80]:
        ts = entry_ts(e)
        if not ts or ts < since or ts > now + 86400:
            continue
        body = e.content[0].value if e.get("content") else e.get("summary", "")
        link = e.get("link") or ""
        out.append(dict(url=link, key=f"rss:{e.get('id') or link}", title=e.get("title") or "(untitled)",
                        created_at=ts, source=name[:40], source_id=e.get("id") or link,
                        discuss_url="", snippet=strip_html(body)[:14000]))
    return out


# ---------------------------------------------------------------- readers
SKIP_HOSTS = ("x.com", "twitter.com", "youtube.com", "youtu.be", "reddit.com", "news.ycombinator.com",
              "linkedin.com", "facebook.com", "instagram.com", "tiktok.com", "bsky.app", "lobste.rs")


def fetch_article(url):
    if not url:
        return ""
    p = up.urlsplit(url)
    host = (p.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS) or p.path.lower().endswith(".pdf"):
        return ""
    try:
        r = net.get(url, timeout=15, retries=0, max_bytes=3_000_000)
    except Exception:
        return ""
    ct = r.headers.get("content-type", "")
    if r.status_code != 200 or (ct and "html" not in ct and "xml" not in ct):
        return ""
    import trafilatura  # lazy: heavy import
    try:
        return trafilatura.extract(r.text, url=url, include_comments=False, include_tables=False,
                                   favor_precision=True) or ""
    except Exception:
        return ""


def hn_comments(story_id, limit=8):
    item = net.get_json(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json") or {}
    out = []
    for kid in (item.get("kids") or [])[:20]:   # kids are in HN's ranked order
        c = net.get_json(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json") or {}
        txt = strip_html(c.get("text") or "")
        if c.get("dead") or c.get("deleted") or len(txt) < 40:
            continue
        out.append(txt[:700])
        if len(out) >= limit:
            break
    return out


def lobsters_comments(short_id, limit=6):
    d = net.get_json(f"https://lobste.rs/s/{short_id}.json")
    cs = sorted(d.get("comments") or [], key=lambda c: -(c.get("score") or 0))
    return [(c.get("comment_plain") or strip_html(c.get("comment", "")))[:600]
            for c in cs if len(c.get("comment_plain") or "") > 40][:limit]


def reddit_comments(permalink, limit=6):
    r = net.get(permalink.rstrip("/") + "/.rss?sort=top&limit=12")
    if r.status_code != 200:
        return []
    out = []
    for e in feedparser.parse(r.content).entries:
        if (e.get("id") or "").startswith("t3_"):
            continue
        txt = strip_html(e.content[0].value if e.get("content") else e.get("summary", ""))
        if len(txt) > 40:
            out.append(txt[:600])
        if len(out) >= limit:
            break
    return out


def discussion(sources):
    """Top comments from every discussion site the item appeared on."""
    out = []
    for s in sources:
        try:
            if s.get("source") == "Hacker News":
                out += [("HN", t) for t in hn_comments(s["source_id"])]
            elif s.get("source") == "Lobsters":
                out += [("Lobsters", t) for t in lobsters_comments(s["source_id"])]
            elif str(s.get("source", "")).startswith("r/") and s.get("discuss_url"):
                out += [(s["source"], t) for t in reddit_comments(s["discuss_url"])]
        except Exception:
            continue
    return out
