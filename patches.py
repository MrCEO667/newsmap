"""Patch tracker: for vulnerability stories, find out whether a fix exists, and keep re-checking until it does.

Where the answer comes from, in order:
  1. OSV.dev (free, no key): fixed package versions, fixing commits, Linux distro patches (Red Hat, SUSE, ...).
     Each CVE's linked GitHub advisory (GHSA) is read too, for npm / PyPI / Go / Maven fixed versions.
  2. NVD: only when OSV has nothing. References that NVD tagged "Patch".
  3. CISA KEV: is it being exploited in the wild, and the official "required action".
  4. Local AI (pipeline runs only): when the databases have no answer, or the story names no CVE, the model
     reads the article and says whether it mentions a fix / fixed version, and which product is affected.
     For stories without a CVE, NVD is then searched for that product (results shown as "possibly related").

Statuses: fixed | partial (fix for some products/versions, not all) | no_fix | unknown.
Results are stored in the patch_checks table. Anything not fixed is checked again every RECHECK_H hours
while the story is younger than RECHECK_DAYS days.

    python patches.py                 # check everything that is due (databases only, no AI)
    python patches.py --ai            # same, plus the AI fallback
    python patches.py --id <item-id>  # check one story now
"""
import argparse
import json
import os
import re
import time

import config as C
import db
import net

ID_RE = re.compile(r"\b(CVE-\d{4}-\d{4,7}|GHSA(?:-[23456789cfghjmpqrvwx]{4}){3})\b", re.I)
VULN_CATEGORIES = set(C.CATEGORY_NAMES) | {"Vulnerabilities & exploits"}   # the whole map is about flaws now
RECHECK_H = 12
RECHECK_DAYS = 60
MAX_IDS = 6                    # a story listing 40 CVEs: check the first few
KEV_CACHE = os.path.join(C.DATA_DIR, "kev.json")
DISTROS = {"RHSA": "Red Hat", "RHBA": "Red Hat", "ALSA": "AlmaLinux", "RLSA": "Rocky Linux", "SUSE": "SUSE",
           "openSUSE": "openSUSE", "USN": "Ubuntu", "UBUNTU": "Ubuntu", "DSA": "Debian", "DLA": "Debian",
           "DEBIAN": "Debian", "ALPINE": "Alpine", "MGASA": "Mageia", "BIT": "Bitnami", "ELSA": "Oracle Linux",
           "CGA": "Chainguard", "MINI": "Minimus", "ECHO": "Echo", "AZL": "Azure Linux", "RXSA": "Rocky Linux"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS patch_checks(
  item_id TEXT PRIMARY KEY,
  checked_at INTEGER, status TEXT, exploited INTEGER DEFAULT 0,
  data TEXT DEFAULT '{}', ai_at INTEGER
);
"""


def connect():
    c = db.connect()
    c.executescript(SCHEMA)
    return c


# ---------------------------------------------------------------- which stories, which IDs
def ids_for(row):
    """CVE / GHSA IDs mentioned anywhere in a story (title, link, feed text, summary, source IDs)."""
    text = " ".join(str(row[k] or "") for k in ("title", "url", "snippet", "summary", "sources"))
    seen = []
    for m in ID_RE.findall(text):
        m = m.upper() if m.upper().startswith("CVE") else "GHSA" + m[4:].lower()
        if m not in seen:
            seen.append(m)
    return seen[:MAX_IDS]


def is_vuln_story(row):
    return row["category"] in VULN_CATEGORIES or bool(ids_for(row))


def due_items(c, now=None, ids=None):
    now = now or int(time.time())
    rows = c.execute(
        "SELECT i.*, p.checked_at AS p_checked, p.status AS p_status, p.ai_at AS p_ai FROM items i "
        "LEFT JOIN patch_checks p ON p.item_id = i.id WHERE i.status IN ('done','low') "
        + ("AND i.id IN (%s)" % ",".join("?" * len(ids)) if ids else "AND i.created_at >= ?"),
        tuple(ids) if ids else (now - RECHECK_DAYS * 86400,)).fetchall()
    out = []
    for r in rows:
        if not is_vuln_story(r):
            continue
        if ids or r["p_checked"] is None:
            out.append(r)
        elif r["p_status"] != "fixed" and now - r["p_checked"] >= RECHECK_H * 3600:
            out.append(r)
    return out


# ---------------------------------------------------------------- data sources
def _json(url, **kw):
    try:
        r = net.get(url, timeout=25, retries=1, **kw)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def osv(vid):
    return _json(f"https://api.osv.dev/v1/vulns/{vid}")


_kev = None


def kev():
    """CISA's 'known exploited' list as {CVE: entry}; downloaded at most every 12 h."""
    global _kev
    if _kev is not None:
        return _kev
    try:
        fresh = time.time() - os.path.getmtime(KEV_CACHE) < 12 * 3600
    except OSError:
        fresh = False
    d = None
    if not fresh:
        d = _json(C.KEV_URL, max_bytes=20_000_000)
        if d:
            with open(KEV_CACHE, "w", encoding="utf-8") as f:
                json.dump(d, f)
    if d is None:
        try:
            with open(KEV_CACHE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
    _kev = {v["cveID"]: v for v in d.get("vulnerabilities", [])}
    return _kev


def nvd(cve):
    d = _json(f"https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cve}")
    try:
        return d["vulnerabilities"][0]["cve"]
    except (TypeError, KeyError, IndexError):
        return None


def nvd_search(keywords, around_ts, limit=3):
    """CVEs in NVD matching a product name, published within ~4 months of the story."""
    import datetime as dt
    import urllib.parse as up
    f = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000")
    q = up.urlencode({"keywordSearch": keywords, "pubStartDate": f(around_ts - 90 * 86400),
                      "pubEndDate": f(min(around_ts + 30 * 86400, time.time())), "resultsPerPage": limit})
    d = _json("https://services.nvd.nist.gov/rest/json/cves/2.0?" + q)
    out = []
    for v in (d or {}).get("vulnerabilities", [])[:limit]:
        cve = v["cve"]
        desc = next((x["value"] for x in cve.get("descriptions", []) if x.get("lang") == "en"), "")
        out.append({"id": cve["id"], "desc": desc[:300]})
    return out


# ---------------------------------------------------------------- reading one ID
def _commit_url(repo, sha):
    repo = (repo or "").rstrip("/").removesuffix(".git")
    if re.match(r"https://(github\.com|gitlab\.com)/", repo) and re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
        return f"{repo}/commit/{sha}"
    return None


def _cvss(rec):
    for s in rec.get("severity") or []:
        m = re.search(r"CVSS:[34]\.\d/.*", s.get("score", ""))
        if m:
            return m.group(0)
    return None


def _merge_osv(rec, info):
    """Pull fix facts out of one OSV record into info."""
    info["sources"].add("OSV")
    if not info.get("summary"):
        info["summary"] = (rec.get("summary") or rec.get("details") or "")[:400]
    info["cvss"] = info.get("cvss") or _cvss(rec)
    for a in rec.get("affected") or []:
        pkg = a.get("package") or {}
        name = pkg.get("name")
        eco = (pkg.get("ecosystem") or "").split(":")[0]
        if name and eco:
            info["products"].add(f"{name} ({eco})")
        has_fix = False
        for rg in a.get("ranges") or []:
            for ev in rg.get("events") or []:
                if "fixed" not in ev:
                    continue
                has_fix = True
                if rg.get("type") == "GIT":
                    u = _commit_url(rg.get("repo"), ev["fixed"])
                    if u and len(info["commits"]) < 4 and u not in info["commits"]:
                        info["commits"].append(u)
                elif name:
                    fx = f"{name} ({eco}) {ev['fixed']}"
                    if fx not in info["fixed_in"] and len(info["fixed_in"]) < 12:
                        info["fixed_in"].append(fx)
        if name and eco:
            (info["pkg_fixed"] if has_fix else info["pkg_open"]).add(f"{name} ({eco})")
        elif has_fix:
            info["pkg_fixed"].add("source")
    for ref in rec.get("references") or []:
        if ref.get("type") == "FIX" and len(info["fix_links"]) < 5 and ref["url"] not in info["fix_links"]:
            info["fix_links"].append(ref["url"])
        elif ref.get("type") == "ADVISORY" and len(info["advisories"]) < 4:
            info["advisories"].append(ref["url"])
    for rel in (rec.get("related") or []) + (rec.get("aliases") or []):
        pre = rel.split("-")[0].split(":")[0]
        if pre in DISTROS:
            info["distros"].add(DISTROS[pre])
        if rel.startswith("GHSA-"):
            info["ghsa"].add(rel)


def check_id(vid):
    info = dict(id=vid, sources=set(), products=set(), fixed_in=[], commits=[], fix_links=[], advisories=[],
                distros=set(), ghsa=set(), pkg_fixed=set(), pkg_open=set(), summary="", cvss=None)
    rec = osv(vid)
    if rec:
        _merge_osv(rec, info)
        for g in sorted(info["ghsa"] - {vid})[:2]:      # GitHub advisory: npm/PyPI/Go/... fixed versions
            grec = osv(g)
            if grec:
                _merge_osv(grec, info)
    if vid.startswith("CVE-"):
        if not (info["fixed_in"] or info["commits"] or info["fix_links"] or info["distros"]):
            cve = nvd(vid)                                 # slow API: only when OSV had no answer
            if cve:
                info["sources"].add("NVD")
                for ref in cve.get("references") or []:
                    tags = ref.get("tags") or []
                    if "Patch" in tags and len(info["fix_links"]) < 5:
                        info["fix_links"].append(ref["url"])
                    elif ("Vendor Advisory" in tags or "Release Notes" in tags) and len(info["advisories"]) < 4 \
                            and ref["url"] not in info["advisories"]:
                        info["advisories"].append(ref["url"])
                if not info["summary"]:
                    info["summary"] = next((x["value"] for x in cve.get("descriptions", [])
                                            if x.get("lang") == "en"), "")[:400]
        k = kev().get(vid)
        if k:
            info["sources"].add("CISA KEV")
            info["kev"] = {"added": k.get("dateAdded"), "action": k.get("requiredAction"),
                           "due": k.get("dueDate"), "ransomware": k.get("knownRansomwareCampaignUse") == "Known",
                           "product": f"{k.get('vendorProject', '')} {k.get('product', '')}".strip()}
            if k.get("product"):
                info["products"].add(info["kev"]["product"])
    info["found"] = bool(info["sources"])
    info["status"] = id_status(info)
    for k in ("sources", "products", "distros", "ghsa", "pkg_fixed", "pkg_open"):
        info[k] = sorted(info[k])[:12]
    return info


def id_status(i):
    fixed = bool(i["fixed_in"] or i["commits"] or i["fix_links"] or i["distros"] or i["pkg_fixed"])
    if fixed and i["pkg_open"] - i["pkg_fixed"]:
        return "partial"
    if fixed:
        return "fixed"
    if i["pkg_open"]:
        return "no_fix"        # the database lists affected packages but no fixed version
    return "unknown"


def overall(per_id, ai=None):
    sts = [x["status"] for x in per_id]
    if sts and all(s == "fixed" for s in sts):
        return "fixed"
    if "fixed" in sts or "partial" in sts:
        return "partial"
    if ai and ai.get("verdict") == "fixed":
        return "fixed"
    if ai and ai.get("verdict") == "no_fix":
        return "no_fix"
    if "no_fix" in sts:
        return "no_fix"
    return "unknown"


# ---------------------------------------------------------------- AI fallback
AI_SYS = ("You read a security news story (and the vendor advisory, if given) and report, strictly from the text, whether the vulnerability it "
          "describes has been fixed. Do not guess: if the text does not say, answer 'unclear'.")
AI_SCHEMA = {"type": "object", "required": ["verdict", "product", "fixed_in", "evidence"], "properties": {
    "verdict": {"type": "string", "enum": ["fixed", "no_fix", "workaround_only", "unclear", "not_a_vulnerability"]},
    "product": {"type": "string", "description": "affected product or library name, short, empty if none"},
    "fixed_in": {"type": "string", "description": "version/update that fixes it, empty if not stated"},
    "evidence": {"type": "string", "description": "one short sentence from or about the text that supports the verdict"}}}


def ai_material(row, per_id):
    """The text the AI judges: story summary, article, database notes and the vendor's own advisory."""
    import sources as S
    summ = json.loads(row["summary"] or "{}")
    text = row["snippet"] or ""
    if len(text) < 1500:
        text = (S.fetch_article(row["url"]) or text)
    db_notes = "; ".join(f"{x['id']}: {x['summary'][:200]}" for x in per_id if x.get("summary"))
    adv = []                                  # the vendor's own advisory usually says which version fixes it
    for x in per_id:
        for u in (x.get("advisories") or [])[:1]:
            t = S.fetch_article(u)
            if t:
                adv.append(f"[{u}]\n{t[:2500]}")
        if len(adv) >= 2:
            break
    return "\n".join([
        f"TITLE: {row['title']}", f"URL: {row['url']}",
        f"SUMMARY: {summ.get('tldr', '')} {' '.join(summ.get('key_points') or [])}",
        f"DATABASE NOTES: {db_notes or '(none)'}", "", "ARTICLE (may be truncated):", text[:6000]]
        + (["", "VENDOR ADVISORIES:"] + adv if adv else []))


def clean_ai(o):
    v = o.get("verdict")
    if v not in AI_SCHEMA["properties"]["verdict"]["enum"]:
        v = "unclear"
    return {"verdict": {"workaround_only": "no_fix"}.get(v, v), "raw_verdict": v,
            "product": str(o.get("product") or "")[:80], "fixed_in": str(o.get("fixed_in") or "")[:120],
            "evidence": str(o.get("evidence") or "")[:300]}


def ai_check(row, per_id):
    import llm
    o = llm.chat_json(AI_SYS, ai_material(row, per_id), AI_SCHEMA, num_predict=220, temperature=0.1)
    return clean_ai(o)


def save_ai(c, row, ai):
    """Store an AI verdict given by someone else (e.g. a Claude session via agent.py) and recompute the status."""
    rec = get(c, row["id"]) or {"ids": [], "related": []}
    related = rec.get("related") or []
    if not rec.get("ids") and ai.get("product") and ai["verdict"] != "not_a_vulnerability" and not related:
        related = nvd_search(ai["product"], row["created_at"])
    status = overall(rec.get("ids") or [], ai)
    if not rec.get("ids") and ai["verdict"] == "not_a_vulnerability":
        status = "n/a"
    exploited = any(x.get("kev") for x in rec.get("ids") or [])
    data = {"ids": rec.get("ids") or [], "ai": ai, "related": related}
    now = int(time.time())
    c.execute("INSERT INTO patch_checks(item_id,checked_at,status,exploited,data,ai_at) VALUES(?,?,?,?,?,?) "
              "ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, exploited=excluded.exploited, "
              "data=excluded.data, ai_at=excluded.ai_at", (row["id"], now, status, int(exploited), json.dumps(data), now))
    c.commit()
    return status


# ---------------------------------------------------------------- one story
def check_item(c, row, use_ai=False, log=print):
    ids = ids_for(row)
    per_id = []
    for vid in ids:
        try:
            per_id.append(check_id(vid))
        except Exception as e:
            log(f"patch check {vid} failed: {str(e)[:80]}")
    prev = c.execute("SELECT data FROM patch_checks WHERE item_id=?", (row["id"],)).fetchone()
    prev = json.loads(prev["data"]) if prev else {}
    ai, related, ai_at = prev.get("ai"), prev.get("related", []), None
    need_ai = use_ai and row["category"] in VULN_CATEGORIES and overall(per_id) in ("unknown", "no_fix")
    if need_ai:
        try:
            ai = ai_check(row, per_id)
            ai_at = int(time.time())
            if not ids and ai.get("product") and ai["verdict"] != "not_a_vulnerability":
                related = nvd_search(ai["product"], row["created_at"])
        except Exception as e:
            log(f"AI patch check failed: {str(e)[:80]}")
    status = overall(per_id, ai)
    if not ids and ai and ai.get("verdict") == "not_a_vulnerability":
        status = "n/a"
    exploited = any(x.get("kev") for x in per_id)
    data = {"ids": per_id, "ai": ai, "related": related}
    c.execute("INSERT INTO patch_checks(item_id,checked_at,status,exploited,data,ai_at) VALUES(?,?,?,?,?,?) "
              "ON CONFLICT(item_id) DO UPDATE SET checked_at=excluded.checked_at, status=excluded.status, "
              "exploited=excluded.exploited, data=excluded.data, ai_at=COALESCE(excluded.ai_at, ai_at)",
              (row["id"], int(time.time()), status, int(exploited), json.dumps(data), ai_at))
    c.commit()
    return status


def get(c, item_id):
    try:
        r = c.execute("SELECT * FROM patch_checks WHERE item_id=?", (item_id,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    d = json.loads(r["data"])
    d.update(status=r["status"], exploited=bool(r["exploited"]), checked_at=r["checked_at"])
    return d


def all_status(c):
    try:
        return {r[0]: (r[1], r[2]) for r in c.execute("SELECT item_id, status, exploited FROM patch_checks")}
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", action="store_true", help="use the local AI when the databases have no answer")
    ap.add_argument("--id", action="append", help="check this story now (repeatable)")
    a = ap.parse_args()
    c = connect()
    rows = due_items(c, ids=a.id)
    print(f"{len(rows)} stories to check")
    for i, r in enumerate(rows, 1):
        st = check_item(c, r, use_ai=a.ai)
        print(f"[{i}/{len(rows)}] {st:8} {r['title'][:90]}", flush=True)


if __name__ == "__main__":
    main()
