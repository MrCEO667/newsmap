"""NewsMap worker: collect -> filter -> read & summarize -> map.

    pythonw pipeline.py --mode auto          (what the Refresh button and the schedule run)
    python  pipeline.py --mode backfill --days 2 --sources hn --max-summaries 3   (testing)
"""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")   # numpy barely used; avoid per-thread RAM buffers

import argparse
import datetime as dt
import json
try:
    import msvcrt
except ImportError:   # macOS / Linux
    msvcrt = None
    import fcntl
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import psutil

import config as C
import db
import llm
import patches
import sources as S

STAGES = [("collect", "Collect sources"), ("triage", "Filter noise (headline check)"),
          ("summarize", "Read & summarize"), ("patches", "Check for fixes"), ("map", "Place on map")]
DEFAULT_AVG = {"collect_task": 2.0, "triage_item": 1.0, "summarize_item": 22.0, "pass_rate": 0.15}
ALL_SOURCES = ["hn", "lobsters", "reddit", "hf", "kev", "ghsa", "rss"]


class StopRun(Exception):
    pass


# ============================================================== progress
class Progress:
    def __init__(self, c, run_id, mode):
        self.c, self.run_id, self.t0, self._last = c, run_id, time.time(), 0
        self.avg = {**DEFAULT_AVG, **(db.meta_get(c, "avg", {}) or {})}
        self.s = dict(mode=mode, stage="collect", state="running", current="", waiting=None,
                      stages={k: dict(label=l, state="pending", done=0, total=0, note="") for k, l in STAGES},
                      stats=dict(candidates=0, new=0, triaged=0, relevant=0, kept=0, low=0, failed=0),
                      log=[], warnings=[], eta_s=None, pct=0, elapsed_s=0, finish_at=None, cap=0,
                      untriaged_before=0, eligible_before=0)
        self.logf = open(C.LOG_PATH, "a", encoding="utf-8")

    def stage(self, key, total=0, note=""):
        self.s["stages"][key].update(state="active", total=total, done=0, note=note)
        self.s["stage"] = key
        self.save(True)

    def finish_stage(self, key, note=None):
        st = self.s["stages"][key]
        st["state"] = "done"
        if note is not None:
            st["note"] = note
        self.save(True)

    def step(self, key, n=1):
        self.s["stages"][key]["done"] += n
        self.save()

    def learn(self, k, value):
        self.avg[k] = 0.7 * self.avg.get(k, DEFAULT_AVG[k]) + 0.3 * value

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.s["log"] = (self.s["log"] + [line])[-40:]
        self.logf.write(line + "\n")
        self.logf.flush()
        self.save()

    def warn(self, msg):
        self.s["warnings"] = (self.s["warnings"] + [msg])[-200:]
        self.log("! " + msg)

    def eta(self):
        if self.s["state"] in ("done", "failed", "stopped"):
            return 0
        st, a, rem = self.s["stages"], self.avg, 0.0
        col = st["collect"]
        if col["state"] != "done":
            left = (col["total"] - col["done"]) if col["total"] else 110
            rem += max(left, 0) * a["collect_task"] / C.FETCH_THREADS
        tri = st["triage"]
        if tri["state"] == "done":
            pending = 0
        elif tri["state"] == "active":
            pending = max(tri["total"] - tri["done"], 0)
        else:
            pending = self.s["stats"]["new"] + self.s["untriaged_before"]
            if col["state"] != "done" and col["total"]:
                pending += (col["total"] - col["done"]) * 4      # ~4 fresh items per remaining feed
        rem += pending * a["triage_item"]
        summ = st["summarize"]
        if summ["state"] == "active":
            rem += max(summ["total"] - summ["done"], 0) * a["summarize_item"]
        elif summ["state"] != "done":
            est = self.s["eligible_before"] + pending * a["pass_rate"]
            rem += min(est, self.s["cap"]) * a["summarize_item"]
        if st["map"]["state"] != "done":
            rem += 5
        return int(rem)

    def save(self, force=False):
        now = time.time()
        if not force and now - self._last < 1.0:
            return
        self._last = now
        el = int(now - self.t0)
        eta = self.eta()
        self.s.update(elapsed_s=el, eta_s=eta, finish_at=int(now + eta),
                      pct=100 if self.s["state"] == "done" else int(100 * el / max(el + eta, 1)))
        self.c.execute("UPDATE runs SET status=?, state=? WHERE id=?",
                       (json.dumps(self.s), self.s["state"], self.run_id))
        self.c.commit()

    def _control(self):
        return self.c.execute("SELECT control FROM runs WHERE id=?", (self.run_id,)).fetchone()[0] or ""

    def check(self):
        ctl = self._control()
        if ctl == "stop":
            raise StopRun()
        if ctl == "pause":
            prev = self.s["state"]
            self.s["state"] = "paused"
            self.log("Paused by user")
            while True:
                time.sleep(2)
                ctl = self._control()
                if ctl == "stop":
                    raise StopRun()
                if ctl != "pause":
                    break
                self.t0 += 2          # paused time doesn't count as elapsed
                self.save(True)
            self.s["state"] = prev if prev != "paused" else "running"
            self.log("Resumed")

    def wait_gpu(self):
        """Wait (never fight) while other apps need the GPU or the PC is low on memory."""
        while True:
            self.check()
            ok, free = llm.gpu_ready()
            mem = llm.free_commit_mb()
            need_mem = C.MIN_FREE_COMMIT_MB if llm.model_loaded() else C.MODEL_LOAD_COMMIT_MB
            mem_ok = mem is None or mem >= need_mem
            if ok and mem_ok:
                if self.s["waiting"]:
                    self.s.update(waiting=None, state="running")
                    self.log("Resources are free again, continuing")
                return
            if ok and llm.model_loaded():
                # Our model is the biggest memory user on the PC: give it back instead of sitting on it.
                llm.unload(C.LLM_MODEL)
                self.log(f"PC ran low on memory ({mem / 1024:.1f} GB free): released the AI model "
                         "so other apps have room. Will reload when memory frees up.")
                time.sleep(5)
                continue
            if ok:
                msg = (f"PC is low on memory: {mem / 1024:.1f} GB free, needs {need_mem / 1024:.1f} GB to load "
                       "the AI model. Waiting so your other apps don't crash. Continues automatically.")
            else:
                msg = (f"GPU is busy with other apps: {free} MB free, needs {C.MIN_FREE_VRAM_MB} MB. "
                       "Continues automatically.")
            if self.s["waiting"] != msg:
                self.log(msg)
            self.s.update(state="waiting", waiting=msg)
            self.save(True)
            for _ in range(max(C.GPU_WAIT_CHECK_S // 2, 1)):
                time.sleep(2)
                self.t0 += 2
                self.check()


# ============================================================== helpers
def src_label(sources):
    parts = []
    for s in sources:
        p = s.get("source", "?")
        if s.get("engagement"):
            p += f" {s['engagement']} pts"
        if s.get("rank"):
            p += f" top #{s['rank']}"
        parts.append(p)
    return ", ".join(parts)


def snip(t, n):
    t = " ".join((t or "").split())
    return t[:n] + ("..." if len(t) > n else "")


# ============================================================== stage 1
def short_err(e):
    s = str(e)
    if "404" in s:
        return "feed not found (404)"
    if "403" in s:
        return "access blocked (403)"
    if "429" in s:
        return "rate-limited (429), will retry next run"
    if "NameResolution" in s or "getaddrinfo" in s:
        return "site offline (domain doesn't resolve)"
    if "Timeout" in type(e).__name__ or "timed out" in s:
        return "timed out"
    return f"{type(e).__name__}: {s[:80]}"


def stage_collect(c, P, since, now, only):
    fails = db.meta_get(c, "feed_fails", {}) or {}   # name -> {"n": consecutive failures, "t": last try}
    tasks = []
    want = lambda k: k in only
    if want("hn"):
        tasks.append(("Hacker News", S.collect_hn, (since, now)))
    if want("lobsters"):
        tasks.append(("Lobsters", S.collect_lobsters, (since, now)))
    if want("hf"):
        tasks.append(("HF Papers", S.collect_hf, (since, now)))
    if want("kev"):
        tasks.append(("CISA KEV", S.collect_kev, (since, now)))
    if want("ghsa"):
        tasks.append(("GitHub Advisories", S.collect_ghsa, (since, now)))
    if want("reddit"):
        for sub in C.REDDIT_SUBS:
            tasks.append((f"r/{sub}", S.collect_reddit, (sub, since, now)))
    skipped = []
    if want("rss"):
        for name, url in S.feed_list():
            f = fails.get(name)
            if f and f["n"] >= 3 and now - f["t"] < 7 * 86400:   # dead feed: retry weekly only
                skipped.append(name)
                continue
            tasks.append((name, S.collect_feed, (name, url, since, now)))
    if skipped:
        P.log(f"Skipping {len(skipped)} feeds that failed 3 runs in a row (retried weekly): {', '.join(skipped)}")

    P.stage("collect", len(tasks), f"{len(tasks)} sources & feeds")
    t0 = time.time()
    ex = ThreadPoolExecutor(C.FETCH_THREADS)
    try:
        futs = {ex.submit(fn, *args): name for name, fn, args in tasks}
        for f in as_completed(futs):
            name = futs[f]
            try:
                cands = f.result()
                fails.pop(name, None)
            except Exception as e:
                n = fails.get(name, {}).get("n", 0) + 1
                fails[name] = {"n": n, "t": now}
                P.warn(f"{name}: {short_err(e)}" + (" · will be skipped for a week" if n == 3 else ""))
                cands = []
            new = sum(db.upsert_candidate(c, cd) for cd in cands if cd.get("title"))
            c.commit()
            P.s["stats"]["candidates"] += len(cands)
            P.s["stats"]["new"] += new
            P.s["current"] = f"Collected {name}"
            if cands:
                P.log(f"{name}: {len(cands)} items ({new} new)")
            P.step("collect")
            P.check()
    except StopRun:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=False)
    db.meta_set(c, "feed_fails", fails)
    if tasks:
        P.learn("collect_task", (time.time() - t0) * C.FETCH_THREADS / len(tasks))
    P.finish_stage("collect", f"{P.s['stats']['candidates']} items seen, {P.s['stats']['new']} new")


# ============================================================== stage 2
TRIAGE_SYS = """You are the strict front-page editor of a private intelligence briefing for a professional who follows AI and cybersecurity and wants to learn important things EARLY, before they reach YouTube or mainstream news.

For the item decide:
- relevant: true only if it is mainly about AI/ML (models, research, agents, AI products, AI industry/policy, compute) or cybersecurity/privacy (vulnerabilities, exploits, malware, threat actors, breaches, security research/tools, surveillance). General programming, science, politics, business or culture news is NOT relevant unless AI or security is its core.
- category: the best fitting category.
- interest (0-10), judged by information value, not popularity:
  9-10: major and new: frontier model release, critical 0-day exploited in the wild, landmark paper, major breach, important leak.
  7-8: solid new technical information: original research, detailed write-up, notable release, credible first-hand account.
  5-6: useful but incremental, niche, or a re-report of known news.
  0-4: noise: memes, jokes, rants, hot takes without new facts, beginner tutorials, career or 'Ask' questions, marketing, listicles, corporate gossip/drama, space/science/politics news, speculation.
Be strict: most items should score 6 or lower."""

TRIAGE_SCHEMA = {"type": "object", "required": ["relevant", "category", "interest"], "properties": {
    "relevant": {"type": "boolean"}, "category": {"type": "string", "enum": C.CATEGORY_NAMES},
    "interest": {"type": "integer"}}}


def stage_triage(c, P, now):
    c.execute("UPDATE items SET status='rejected', error='too old' WHERE status='new' AND created_at<?",
              (now - (C.BACKFILL_DAYS + 1) * 86400,))
    rows = c.execute("SELECT id,title,sources,snippet FROM items WHERE status='new' "
                     "ORDER BY engagement DESC").fetchall()
    P.stage("triage", len(rows), f"{len(rows)} headlines to check")
    st = P.s["stats"]
    # One headline per call: batching made the 8B model mix up which score belongs to which line.
    # Ollama reuses the cached system prompt, so this costs about the same per item.
    for k, r in enumerate(rows):
        if k % 10 == 0:
            P.wait_gpu()
        P.s["current"] = f"Checking headline {k + 1} of {len(rows)}: {r['title'][:90]}"
        msg = f"[{src_label(json.loads(r['sources']))}] {r['title']}"
        if r["snippet"]:
            msg += "\n" + snip(r["snippet"], 300)
        t = time.time()
        try:
            x = llm.chat_json(TRIAGE_SYS, msg, TRIAGE_SCHEMA, num_predict=60)
        except Exception as e:
            P.warn(f"Headline check failed: {r['title'][:60]}: {str(e)[:80]}")
            c.execute("UPDATE items SET attempts=attempts+1, status=CASE WHEN attempts>=2 "
                      "THEN 'rejected' ELSE status END WHERE id=?", (r["id"],))
            P.step("triage")
            continue
        P.learn("triage_item", time.time() - t)
        interest = max(0, min(10, int(x.get("interest") or 0)))
        rel = bool(x.get("relevant"))
        cat = x.get("category") if x.get("category") in C.CATEGORY_NAMES else "Other tech"
        ok = rel and interest >= C.TRIAGE_MIN_INTEREST
        c.execute("UPDATE items SET relevant=?, category=?, interest=?, status=? WHERE id=?",
                  (int(rel), cat, interest, "triaged" if ok else "rejected", r["id"]))
        st["triaged"] += 1
        st["relevant"] += ok
        if k % 10 == 9:
            c.commit()
            P.check()
            time.sleep(C.PAUSE_BETWEEN_LLM_S)
        P.step("triage")
    c.commit()
    if st["triaged"] >= 30:
        P.learn("pass_rate", st["relevant"] / st["triaged"])
    P.finish_stage("triage", f"{st['relevant']} of {st['triaged']} look worth reading")


# ============================================================== stage 3
SUM_SYS = """You write one-page intelligence briefs for an expert in AI and cybersecurity who has no time to read the original. Use ONLY the provided material; never invent facts, numbers or names. If the article text is unavailable, work from the title, source notes and discussion, and mention that in value_reason.

Fields:
- headline: informative and specific, max ~90 characters, no clickbait (rewrite a vague original title).
- tldr: 1-2 sentences: what happened and the single most important takeaway.
- key_points: 3-7 one-sentence bullets with the concrete facts: who, what, numbers, versions, CVEs, affected products, methods, results.
- why_it_matters: 1-2 sentences on the practical impact for AI/security practitioners.
- insider_notes: 0-4 bullets of genuinely useful expert insight from the discussion comments (corrections, first-hand experience, caveats, informed skepticism, extra context). Empty list if the discussion adds nothing.
- entities: key companies, products, models, CVE IDs, threat actors, people.
- tags: 3-6 short lowercase topic tags.
- category: the MOST SPECIFIC fitting category:
  AI models & releases = a new model/version/benchmark result; AI research = papers, methods, findings;
  AI agents & tools = agent frameworks, coding assistants, AI dev tools; AI safety & security = jailbreaks, prompt injection, alignment, attacks on/with AI;
  AI industry & policy = companies, funding, regulation, business moves; Compute & hardware = GPUs, chips, datacenters, inference hardware;
  Vulnerabilities & exploits = a specific flaw, CVE, bug, RCE, bypass or exploit; Malware & threat actors = malware, ransomware, APT groups, campaigns;
  Breaches & incidents = a company/org was hacked, leaked data or had an outage from an attack; Security research & tools = defensive tools, techniques, research that is not one specific vulnerability;
  Privacy & surveillance = tracking, spyware, data collection, government surveillance; Other tech = none of the above.
- value_reason: one short sentence on what makes this valuable (or weak) for that expert."""

# Value = weighted yes/no checklist, asked in a separate short call. Small models collapse onto
# "safe" middle values when asked for 0-10 or 0-3 ratings; independent yes/no facts spread well.
CHECKS = {
    "new_information": (1.5, "Does it report something genuinely NEW (not a rehash, explainer, or old news)?"),
    "concrete_details": (1.5, "Does it give concrete specifics: numbers, versions, CVE IDs, benchmarks, code, affected products?"),
    "technical_depth": (1.5, "Does it explain HOW or WHY at an expert technical level (not just what happened)?"),
    "primary_source": (1.5, "Is it a primary source: the researcher/vendor/lab's own write-up, paper, advisory or release?"),
    "broad_impact": (1.5, "Does it affect a widely used product/model or a large share of AI/security practitioners?"),
    "actionable": (1.0, "Can the reader act on it now: patch, mitigate, try a tool/model, change a practice?"),
    "expert_discussion": (1.0, "Do the discussion comments add real expert insight (corrections, first-hand experience)?"),
    "opinion_or_speculation": (-2.0, "Is it mainly opinion, prediction or speculation rather than facts?"),
    "promotional": (-2.0, "Is it mainly marketing or self-promotion?"),
}
CHECK_SYS = ("You are a strict fact-checker for an AI/cybersecurity intelligence brief. Answer each yes/no "
             "question about the item honestly and independently. Say yes only when the material clearly "
             "supports it.\n\n" + "\n".join(f"- {k}: {q}" for k, (w, q) in CHECKS.items()))
CHECK_SCHEMA = {"type": "object", "required": list(CHECKS), "properties": {k: {"type": "boolean"} for k in CHECKS}}


def score_checks(prompt):
    x = llm.chat_json(CHECK_SYS, prompt, CHECK_SCHEMA, num_predict=200)
    checks = {k: bool(x.get(k)) for k in CHECKS}
    value = max(0, min(10, round(sum(w for k, (w, _) in CHECKS.items() if checks[k]))))
    return checks, value

_STR_LIST = {"type": "array", "items": {"type": "string"}}
SUM_SCHEMA = {"type": "object", "required": [
    "headline", "tldr", "key_points", "why_it_matters", "insider_notes", "entities", "tags",
    "category", "value_reason"], "properties": {
    "headline": {"type": "string"}, "tldr": {"type": "string"}, "key_points": _STR_LIST,
    "why_it_matters": {"type": "string"}, "insider_notes": _STR_LIST, "entities": _STR_LIST,
    "tags": _STR_LIST, "category": {"type": "string", "enum": C.CATEGORY_NAMES},
    "value_reason": {"type": "string"}}}


def gather(r):
    """Runs in a background thread while the AI works on the previous item."""
    src = json.loads(r["sources"])
    names = {s.get("source") for s in src}
    snippet = r["snippet"] or ""
    if names & C.SELF_CONTAINED or len(snippet) > 1500:
        text = snippet
    else:
        text = S.fetch_article(r["url"])
        if len(text) < 300 and snippet:
            text = (snippet + "\n\n" + text).strip()
    return {"text": text, "comments": S.discussion(src)}


def build_prompt(r, mat):
    src = json.loads(r["sources"])
    date = dt.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
    out = [f"SOURCES: {src_label(src)}", f"URL: {r['url'] or '(none)'}", f"PUBLISHED: {date}",
           f"ORIGINAL TITLE: {r['title']}", ""]
    if mat["text"]:
        out += ["CONTENT (may be truncated):", mat["text"][:11000]]
    else:
        out += ["CONTENT: (article text unavailable: use the title, sources and discussion)"]
    if mat["comments"]:
        out.append("\nTOP DISCUSSION COMMENTS:")
        total = 0
        for site, t in mat["comments"]:
            if total > 5000:
                break
            out.append(f"- [{site}] {t}")
            total += len(t)
    return "\n".join(out)


def clean_summary(o, r):
    lst = lambda k, n: [str(x).strip() for x in (o.get(k) or []) if str(x).strip()][:n]
    return {
        "headline": (o.get("headline") or r["title"]).strip()[:140],
        "tldr": (o.get("tldr") or "").strip(),
        "key_points": lst("key_points", 8), "why_it_matters": (o.get("why_it_matters") or "").strip(),
        "insider_notes": lst("insider_notes", 5), "entities": lst("entities", 12),
        "tags": [t.lower() for t in lst("tags", 6)],
        "category": o.get("category") if o.get("category") in C.CATEGORY_NAMES else (r["category"] or "Other tech"),
        "value_reason": (o.get("value_reason") or "").strip(),
    }


def embed_text(s):
    return f"{s['headline']}. {s['tldr']} Tags: {', '.join(s['tags'])}"


def stage_summarize(c, P, now, cap, mode="update"):
    if mode == "update":
        # keep the map fresh: stale backlog only survives if its headline looked important
        n = c.execute("UPDATE items SET status='rejected', error='backlog expired' WHERE status='triaged' "
                      "AND created_at<? AND interest<8", (now - 7 * 86400,)).rowcount
        c.commit()
        if n:
            P.log(f"Expired {n} older backlog items (older than 7 days, headline interest below 8)")
    rows = c.execute("SELECT * FROM items WHERE status='triaged' AND created_at>=? "
                     "ORDER BY interest DESC, engagement DESC, created_at DESC LIMIT ?",
                     (now - (C.BACKFILL_DAYS + 1) * 86400, cap)).fetchall()
    left = c.execute("SELECT COUNT(*) FROM items WHERE status='triaged'").fetchone()[0] - len(rows)
    P.stage("summarize", len(rows), f"best {len(rows)} first" + (f", {left} wait for next run" if left > 0 else ""))
    st = P.s["stats"]
    ex = ThreadPoolExecutor(2)
    futs = {}

    def prefetch(k):
        if k < len(rows) and k not in futs:
            futs[k] = ex.submit(gather, rows[k])

    try:
        for k, r in enumerate(rows):
            t_iter = time.time()
            for j in (k, k + 1, k + 2):
                prefetch(j)
            P.check()
            P.s["current"] = f"Reading: {r['title'][:120]}"
            P.save()
            try:
                mat = futs.pop(k).result(timeout=240)
            except Exception:
                mat = {"text": r["snippet"] or "", "comments": []}
            P.wait_gpu()
            P.s["current"] = f"Summarizing: {r['title'][:120]}"
            P.save(True)
            try:
                prompt = build_prompt(r, mat)
                o = clean_summary(llm.chat_json(SUM_SYS, prompt, SUM_SCHEMA, num_predict=1100), r)
                o["checks"], o["value_score"] = score_checks(prompt)
                status = "done" if o["value_score"] >= C.VALUE_MIN else "low"
                # embeddings happen in the map stage, after the big model is unloaded (never 2 models in RAM)
                c.execute("UPDATE items SET summary=?, value_score=?, category=?, status=?, embedding=NULL, "
                          "done_at=?, error=NULL WHERE id=?",
                          (json.dumps(o), o["value_score"], o["category"], status, int(time.time()), r["id"]))
                st["kept" if status == "done" else "low"] += 1
                P.log(f"{'KEEP' if status == 'done' else 'drop'} {o['value_score']}/10  {o['headline'][:90]}")
            except StopRun:
                raise
            except Exception as e:
                c.execute("UPDATE items SET attempts=attempts+1, error=?, status=CASE WHEN attempts>=1 "
                          "THEN 'error' ELSE status END WHERE id=?", (str(e)[:300], r["id"]))
                st["failed"] += 1
                P.warn(f"Summary failed: {r['title'][:60]}: {str(e)[:100]}")
            c.commit()
            P.learn("summarize_item", time.time() - t_iter)
            P.step("summarize")
            if (k + 1) % C.RECYCLE_MODEL_EVERY == 0 and k + 1 < len(rows):
                llm.unload(C.LLM_MODEL)   # the runner's RAM creeps up over many prompts; reset it
                P.log(f"Recycled the AI model after {k + 1} stories to release accumulated memory")
                time.sleep(3)
            time.sleep(C.PAUSE_BETWEEN_LLM_S)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    P.finish_stage("summarize", f"{st['kept']} kept, {st['low']} dropped as shallow")


# ============================================================== stage 3b
def stage_patches(c, P):
    """Vulnerability stories: is there a fix yet? Databases first, the AI only when they have no answer."""
    c.executescript(patches.SCHEMA)
    rows = patches.due_items(c)
    P.stage("patches", len(rows), "looking up fixes in OSV, NVD and CISA KEV")
    counts = {}
    for r in rows:
        P.check()
        P.s["current"] = f"Fix check: {snip(r['title'], 70)}"
        if r["category"] in patches.VULN_CATEGORIES:
            P.wait_gpu()
        try:
            st = patches.check_item(c, r, use_ai=True, log=P.warn)
            counts[st] = counts.get(st, 0) + 1
        except StopRun:
            raise
        except Exception as e:
            P.warn(f"Fix check failed: {short_err(e)}")
        P.step("patches")
    names = {"fixed": "fixed", "partial": "partly fixed", "no_fix": "no fix yet", "unknown": "unknown"}
    P.finish_stage("patches", ", ".join(f"{n} {names.get(k, k)}" for k, n in counts.items()) or "nothing due")


# ============================================================== stage 4
def stage_map(c, P):
    rows = c.execute("SELECT id, summary FROM items WHERE status IN ('done','low') AND embedding IS NULL").fetchall()
    P.stage("map", len(rows), "linking similar stories (small model on CPU)")
    llm.unload(C.LLM_MODEL)            # free the big model's ~6 GB before loading the embedder
    time.sleep(3)
    while rows and (llm.free_commit_mb() or 99999) < C.MIN_FREE_COMMIT_MB + 500:   # embedder needs ~0.5 GB
        P.s.update(state="waiting", waiting="PC is low on memory; waiting before linking stories.")
        P.save(True)
        time.sleep(10)
        P.check()
    P.s.update(state="running", waiting=None)
    for r in rows:
        P.check()
        try:
            vec = np.asarray(llm.embed(embed_text(json.loads(r["summary"]))), dtype=np.float32).tobytes()
            c.execute("UPDATE items SET embedding=? WHERE id=?", (vec, r["id"]))
            c.commit()
        except Exception as e:
            P.warn(f"Embedding failed: {str(e)[:80]}")
        P.step("map")
    P.finish_stage("map")


# ============================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "backfill", "update"])
    ap.add_argument("--days", type=float)
    ap.add_argument("--max-summaries", type=int)
    ap.add_argument("--sources", help="comma list of: " + ",".join(ALL_SOURCES))
    ap.add_argument("--scheduled", action="store_true")
    args = ap.parse_args()

    os.makedirs(C.DATA_DIR, exist_ok=True)
    lockf = open(C.LOCK_PATH, "a+")
    for attempt in range(16):   # a just-stopped run may still be shutting down: give it up to ~30 s
        try:
            if msvcrt:
                msvcrt.locking(lockf.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(2)
    else:
        print("Another update is already running.")
        return
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if C.IS_WIN else 10)
    except Exception:
        pass

    c = db.connect()
    now = int(time.time())
    last = db.meta_get(c, "last_full_collect")
    # stay in backfill mode until one full 30-day backfill has completed
    mode = args.mode if args.mode != "auto" else ("update" if db.meta_get(c, "backfill_done") else "backfill")
    if args.days:
        days = args.days
    elif mode == "backfill":
        days = C.BACKFILL_DAYS
    else:
        days = min(C.BACKFILL_DAYS, max(C.MIN_LOOKBACK_DAYS, (now - (last or 0)) / 86400 + 1))
    cap = args.max_summaries or (C.MAX_SUMMARIES_BACKFILL if mode == "backfill" else C.MAX_SUMMARIES_UPDATE)
    only = [s.strip() for s in args.sources.split(",")] if args.sources else ALL_SOURCES

    c.execute("UPDATE runs SET state='interrupted' WHERE state IN ('running','paused','waiting')")
    run_id = c.execute("INSERT INTO runs(started_at,mode,state,pid,status) VALUES(?,?,?,?,?)",
                       (now, mode, "running", os.getpid(), "{}")).lastrowid
    c.commit()
    P = Progress(c, run_id, mode)
    P.s.update(cap=cap, days=round(days, 1), scheduled=args.scheduled,
               untriaged_before=c.execute("SELECT COUNT(*) FROM items WHERE status='new'").fetchone()[0],
               eligible_before=c.execute("SELECT COUNT(*) FROM items WHERE status='triaged'").fetchone()[0])
    P.log(f"Update started ({mode}: last {days:.1f} days, up to {cap} summaries)")
    final = "done"
    try:
        if not llm.ensure_ollama():
            raise RuntimeError("Ollama is not running and could not be started")
        recent = last and now - last < 3600 and set(only) == set(ALL_SOURCES) and not args.days
        if recent:   # restarted shortly after a full collection: resume instead of re-downloading
            P.stage("collect", 0, f"sources were collected {int((now - last) / 60)} min ago, reusing them")
            P.finish_stage("collect")
            P.log("Resuming: sources were collected less than an hour ago, skipping straight to filtering")
        else:
            stage_collect(c, P, int(now - days * 86400), now, only)
            if set(only) == set(ALL_SOURCES):
                db.meta_set(c, "last_full_collect", now)
        stage_triage(c, P, now)
        stage_summarize(c, P, now, cap, mode)
        stage_patches(c, P)
        stage_map(c, P)
        db.meta_set(c, "last_success", int(time.time()))
        if mode == "backfill" and set(only) == set(ALL_SOURCES) and not args.days:
            db.meta_set(c, "backfill_done", True)
        P.log(f"Finished: {P.s['stats']['kept']} new stories on the map")
    except StopRun:
        final = "stopped"
        P.log("Stopped by user. Progress is saved; the next run continues where this one stopped.")
    except Exception as e:
        final = "failed"
        P.warn(f"Update failed: {e}")
        P.logf.write(traceback.format_exc())
    finally:
        llm.unload_all()   # give the VRAM back to your other projects
        P.s.update(state=final, current="", waiting=None)
        for st in P.s["stages"].values():
            if st["state"] == "active":
                st["state"] = "done" if final == "done" else final
        db.meta_set(c, "avg", P.avg)
        P.save(True)
        c.execute("UPDATE runs SET finished_at=?, state=? WHERE id=?", (int(time.time()), final, run_id))
        c.commit()


if __name__ == "__main__":
    main()
