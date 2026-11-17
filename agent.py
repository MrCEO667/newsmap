"""Let a Claude session (e.g. Claude Code with Haiku) do NewsMap's AI work instead of the local Ollama model.

The session runs these commands; all reading/judging happens in the session, everything else here.

    .venv/bin/python agent.py collect        # download new items from all sources (no AI, a few minutes)
    .venv/bin/python agent.py guide          # the editorial rules + answer formats (read once)
    .venv/bin/python agent.py next           # next batch of work as JSON (triage -> summarize -> fixcheck)
    .venv/bin/python agent.py submit data/answers/answer.json   # answers for that batch (JSON, see guide)
    .venv/bin/python agent.py finish         # database fix checks + place new stories on the map
    .venv/bin/python agent.py status         # what is left

Answers are validated and stored exactly like the local pipeline stores them (same prompts, same
value score formula), so stories from either path look the same on the map.
"""
import json
import os
import sys
import time

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import config as C
import db
import patches
import pipeline as PL

CACHE = os.path.join(C.DATA_DIR, "agent_cache")
BATCH = {"triage": 20, "resort": 20, "summarize": 3, "rewrite": 3, "fixcheck": 3}
NOT_VULN = "Not a vulnerability"     # category for old stories that aren't about a flaw (hidden from the map)
RESORT_FROM = {"Vulnerabilities & exploits", "Security research & tools", "Privacy & surveillance",
               "AI safety & security", "Malware & threat actors", "Breaches & incidents"}
TEXT_CAP = 7000        # characters of article+comments per story handed to the session


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def conn():
    c = db.connect()
    c.executescript(patches.SCHEMA)
    return c


def window(now):
    return now - (C.BACKFILL_DAYS + 1) * 86400


# ---------------------------------------------------------------- task selection
def triage_rows(c, now, n):
    c.execute("UPDATE items SET status='rejected', error='too old' WHERE status='new' AND created_at<?", (window(now),))
    c.commit()
    return c.execute("SELECT * FROM items WHERE status='new' ORDER BY engagement DESC LIMIT ?", (n,)).fetchall()


def summarize_rows(c, now, n):
    return c.execute("SELECT * FROM items WHERE status='triaged' AND created_at>=? "
                     "ORDER BY interest DESC, engagement DESC, created_at DESC LIMIT ?", (window(now), n)).fetchall()


def fixcheck_rows(c, now, n):
    rows = c.execute(
        "SELECT i.*, p.status AS p_status, p.ai_at AS p_ai FROM items i LEFT JOIN patch_checks p ON p.item_id=i.id "
        "WHERE i.status IN ('done','low') AND i.category IN (%s) AND i.created_at>=? "
        "AND (p.status IS NULL OR p.status IN ('unknown','no_fix')) AND (p.ai_at IS NULL OR p.ai_at<?) "
        "ORDER BY i.created_at DESC" % ",".join("?" * len(patches.VULN_CATEGORIES)),
        (*patches.VULN_CATEGORIES, now - patches.RECHECK_DAYS * 86400, now - patches.RECHECK_H * 3600)).fetchall()
    return rows[:n], len(rows)


def resort_rows(c, n):
    """Stories from before the focus change: security topics, plus anything else that names a CVE."""
    rows = c.execute("SELECT * FROM items WHERE status IN ('done','low') AND category IN (%s) ORDER BY created_at DESC"
                     % ",".join("?" * len(C.LEGACY_CATEGORIES)), C.LEGACY_CATEGORIES).fetchall()
    rows = [r for r in rows if r["category"] in RESORT_FROM or patches.ids_for(r)]
    return rows[:n], len(rows)


def rewrite_rows(c, n):
    """Stories on the map whose brief was written by the old local model: rewrite them in plain English."""
    rows = c.execute("SELECT * FROM items WHERE status IN ('done','low') AND category IN (%s) "
                     "AND instr(summary, '\"by\": \"claude\"')=0 ORDER BY status='low', value_score DESC, created_at DESC"
                     % ",".join("?" * len(C.CATEGORY_NAMES)), C.CATEGORY_NAMES).fetchall()
    return rows[:n], len(rows)


def counts(c, now):
    t = c.execute("SELECT COUNT(*) FROM items WHERE status='new' AND created_at>=?", (window(now),)).fetchone()[0]
    s = c.execute("SELECT COUNT(*) FROM items WHERE status='triaged' AND created_at>=?", (window(now),)).fetchone()[0]
    f = fixcheck_rows(c, now, 0)[1]
    return {"triage": t, "resort": resort_rows(c, 0)[1], "summarize": s, "rewrite": rewrite_rows(c, 0)[1],
            "fixcheck": f}


# ---------------------------------------------------------------- material (cached so retries are instant)
def material(r):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, r["id"].replace(":", "_").replace("/", "_") + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        mat = PL.gather(r)
    except Exception:
        mat = {"text": r["snippet"] or "", "comments": []}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mat, f)
    return mat


def summarize_input(r):
    mat = material(r)
    mat = {"text": (mat.get("text") or "")[:TEXT_CAP], "comments": mat.get("comments") or []}
    return PL.build_prompt(r, mat)


# ---------------------------------------------------------------- commands
def cmd_collect():
    c = conn()
    now = int(time.time())
    last = db.meta_get(c, "last_full_collect")
    days = min(C.BACKFILL_DAYS, max(C.MIN_LOOKBACK_DAYS, (now - (last or 0)) / 86400 + 1))
    c.execute("UPDATE runs SET state='interrupted' WHERE state IN ('running','paused','waiting')")
    run_id = c.execute("INSERT INTO runs(started_at,mode,state,pid,status) VALUES(?,?,?,?,?)",
                       (now, "update", "running", os.getpid(), "{}")).lastrowid
    c.commit()
    P = PL.Progress(c, run_id, "update")
    P.log(f"Collecting the last {days:.1f} days for a Claude session (agent.py)")
    final = "done"
    try:
        PL.stage_collect(c, P, int(now - days * 86400), now, PL.ALL_SOURCES)
        db.meta_set(c, "last_full_collect", now)
    except Exception as e:
        final = "failed"
        P.warn(f"Collect failed: {e}")
    finally:
        P.s.update(state=final, current="")
        P.save(True)
        c.execute("UPDATE runs SET finished_at=?, state=? WHERE id=?", (int(time.time()), final, run_id))
        c.commit()
    st = P.s["stats"]
    out({"collected": final, "days": round(days, 1), "new_items": st.get("new", 0),
         "warnings": P.s["warnings"][-5:], "todo": counts(c, int(time.time()))})


def cmd_guide():
    checks = {k: q for k, (w, q) in PL.CHECKS.items()}
    print(f"""# NewsMap work guide

Work comes in three kinds. `agent.py next` hands out the next batch; answer it with `agent.py submit`.
Answer only from the given material. Never invent facts. Keep every answer's "id" exactly as given.

## WRITING STYLE (overrides anything below that says "expert")
The reader is NOT technical: a curious person who wants to understand what happened and whether it affects them.
- Write every text you produce in simple, everyday English: short sentences, common words, no jargon.
- If a technical term can't be avoided, explain it in a few plain words in brackets the first time,
  e.g. "a zero-day (a flaw attackers used before the maker knew about it)".
- Say plainly who is affected and what a normal person should do (e.g. "update the app to version 3.2").
- For each story also give a "glossary": up to 6 terms from your text, each with a one-line plain meaning.

## 1. triage (batches of {BATCH['triage']} headlines)
{PL.TRIAGE_SYS}

Categories (use exactly one of these strings): {json.dumps(C.CATEGORY_NAMES)}

Answer:
{{"kind": "triage", "answers": [{{"id": "...", "relevant": true, "category": "Vulnerabilities & exploits", "interest": 7}}, ...]}}
Only items that are relevant AND interest >= {C.TRIAGE_MIN_INTEREST} go on to be summarized.

## 1b. resort (batches of {BATCH['resort']} older stories, headline + short summary)
NewsMap now tracks ONLY security flaws (vulnerabilities) and exploits. For each older story decide:
- vulnerability: true only if it is mainly about a specific security flaw or exploit (a vulnerability, CVE,
  security bug, zero-day, patch, exploit, or a technical write-up on finding/exploiting flaws). Otherwise false.
- category (only when vulnerability is true): what is affected, one of the categories above.

Answer:
{{"kind": "resort", "answers": [{{"id": "...", "vulnerability": true, "category": "Network & VPN gear"}}, {{"id": "...", "vulnerability": false}}]}}

## 2. summarize (batches of {BATCH['summarize']} full stories)
{PL.SUM_SYS}

Also answer these yes/no checks for each story (they compute its value score; be strict):
{json.dumps(checks, indent=1)}

Answer:
{{"kind": "summarize", "answers": [{{"id": "...", "headline": "...", "tldr": "...", "key_points": ["..."],
  "why_it_matters": "...", "insider_notes": [], "entities": ["..."], "tags": ["..."],
  "category": "...", "value_reason": "...",
  "glossary": [{{"term": "CVE", "meaning": "an ID number given to a publicly known security flaw"}}],
  "checks": {{{", ".join(f'"{k}": false' for k in checks)}}}}}]}}
If a story's material is empty or unusable, answer {{"id": "...", "skip": true}} for it.

## 2b. rewrite (batches of {BATCH['rewrite']} older stories)
These stories already have a brief from an older, weaker AI model. Write a NEW brief from the given material,
exactly like 'summarize' (same fields, plain English, glossary, checks), but answer with "kind": "rewrite".
If the material is empty or unusable, answer {{"id": "...", "skip": true}} (the old brief is kept).

## 3. fixcheck (batches of {BATCH['fixcheck']} vulnerability stories)
{patches.AI_SYS}
verdict is one of: {json.dumps(patches.AI_SCHEMA['properties']['verdict']['enum'])}
- product: affected product or library name, short; empty if none
- fixed_in: version or update that fixes it; empty if the text doesn't say
- evidence: one short, plain-English sentence from or about the text that supports the verdict

Answer:
{{"kind": "fixcheck", "answers": [{{"id": "...", "verdict": "fixed", "product": "...", "fixed_in": "...", "evidence": "..."}}]}}
""")


def cmd_next(kind=None):
    os.makedirs(os.path.dirname(ANSWER_FILE), exist_ok=True)
    c = conn()
    now = int(time.time())
    if kind in (None, "triage"):
        rows = triage_rows(c, now, BATCH["triage"])
        if rows:
            tasks = []
            for r in rows:
                msg = f"[{PL.src_label(json.loads(r['sources']))}] {r['title']}"
                if r["snippet"]:
                    msg += "\n" + PL.snip(r["snippet"], 300)
                tasks.append({"id": r["id"], "input": msg})
            return out({"kind": "triage", "todo": counts(c, now), "tasks": tasks})
    if kind in (None, "resort"):
        rows, _ = resort_rows(c, BATCH["resort"])
        if rows:
            tasks = []
            for r in rows:
                sm = json.loads(r["summary"] or "{}")
                tasks.append({"id": r["id"], "input": f"{sm.get('headline') or r['title']}\n{sm.get('tldr', '')}"})
            return out({"kind": "resort", "todo": counts(c, now), "tasks": tasks})
    if kind in (None, "summarize"):
        rows = summarize_rows(c, now, BATCH["summarize"])
        if rows:
            return out({"kind": "summarize", "todo": counts(c, now),
                        "tasks": [{"id": r["id"], "input": summarize_input(r)} for r in rows]})
    if kind in (None, "rewrite"):
        rows, _ = rewrite_rows(c, BATCH["rewrite"])
        if rows:
            return out({"kind": "rewrite", "how": "Same rules and answer format as 'summarize' (guide section 2), "
                        "but use \"kind\": \"rewrite\". Plain English + glossary.", "todo": counts(c, now),
                        "tasks": [{"id": r["id"], "input": summarize_input(r)} for r in rows]})
    if kind in (None, "fixcheck"):
        rows, _ = fixcheck_rows(c, now, BATCH["fixcheck"])
        if rows:
            tasks = []
            for r in rows:
                if r["p_status"] is None:            # databases first (fast); the session only judges what's left
                    patches.check_item(c, r, use_ai=False)
                    if c.execute("SELECT status FROM patch_checks WHERE item_id=?", (r["id"],)).fetchone()[0] \
                            not in ("unknown", "no_fix"):
                        continue
                rec = patches.get(c, r["id"]) or {}
                tasks.append({"id": r["id"], "input": patches.ai_material(r, rec.get("ids") or [])})
            if tasks:
                return out({"kind": "fixcheck", "todo": counts(c, now), "tasks": tasks})
            return cmd_next("fixcheck")
    out({"kind": "done", "message": "Nothing left. Run: .venv/bin/python agent.py finish"})


def _row(c, iid):
    return c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()


def submit_triage(c, a):
    r = _row(c, a["id"])
    if not r or r["status"] != "new":
        return "not waiting for triage"
    if a.get("category") not in C.CATEGORY_NAMES:
        return f"category must be one of {C.CATEGORY_NAMES}"
    try:
        interest = max(0, min(10, int(a.get("interest"))))
    except (TypeError, ValueError):
        return "interest must be an integer 0-10"
    rel = bool(a.get("relevant"))
    ok = rel and interest >= C.TRIAGE_MIN_INTEREST
    c.execute("UPDATE items SET relevant=?, category=?, interest=?, status=? WHERE id=?",
              (int(rel), a["category"], interest, "triaged" if ok else "rejected", r["id"]))
    return "triaged" if ok else "rejected"


def submit_resort(c, a):
    r = _row(c, a["id"])
    if not r or r["category"] not in C.LEGACY_CATEGORIES:
        return "not waiting for re-sorting"
    if not a.get("vulnerability"):
        c.execute("UPDATE items SET category=? WHERE id=?", (NOT_VULN, r["id"]))
        return "hidden (not about a flaw)"
    if a.get("category") not in C.CATEGORY_NAMES:
        return f"category must be one of {C.CATEGORY_NAMES}"
    sm = json.loads(r["summary"] or "{}")
    sm["category"] = a["category"]
    c.execute("UPDATE items SET category=?, summary=? WHERE id=?", (a["category"], json.dumps(sm), r["id"]))
    return "moved to " + a["category"]


def submit_rewrite(c, a):
    r = _row(c, a["id"])
    if not r or r["status"] not in ("done", "low"):
        return "not waiting for a rewrite"
    if a.get("skip"):     # article gone: keep the old brief, but mark it so it isn't handed out again
        sm = json.loads(r["summary"] or "{}")
        sm["by"] = "claude-skipped"
        c.execute("UPDATE items SET summary=? WHERE id=?", (json.dumps(sm), r["id"]))
        return "kept old brief"
    return submit_summarize(c, a, rewrite=True)


def submit_summarize(c, a, rewrite=False):
    r = _row(c, a["id"])
    if not r or (r["status"] != "triaged" and not rewrite):
        return "not waiting for a summary"
    if a.get("skip"):
        c.execute("UPDATE items SET attempts=attempts+1, error='skipped by agent', status=CASE WHEN attempts>=1 "
                  "THEN 'error' ELSE status END WHERE id=?", (r["id"],))
        return "skipped"
    missing = [k for k in PL.SUM_SCHEMA["required"] if not a.get(k) and k not in ("insider_notes",)]
    if missing:
        return f"missing fields: {missing}"
    if a["category"] not in C.CATEGORY_NAMES:
        return f"category must be one of {C.CATEGORY_NAMES}"
    ch = a.get("checks") or {}
    if set(PL.CHECKS) - set(ch):
        return f"checks missing: {sorted(set(PL.CHECKS) - set(ch))}"
    o = PL.clean_summary(a, r)
    o["checks"] = {k: bool(ch[k]) for k in PL.CHECKS}
    o["value_score"] = max(0, min(10, round(sum(w for k, (w, _) in PL.CHECKS.items() if o["checks"][k]))))
    o["by"] = "claude"
    o["glossary"] = [{"term": str(g.get("term", ""))[:60], "meaning": str(g.get("meaning", ""))[:240]}
                     for g in (a.get("glossary") or [])[:6] if isinstance(g, dict) and g.get("term") and g.get("meaning")]
    status = "done" if o["value_score"] >= C.VALUE_MIN else "low"
    if rewrite:   # keep its place on the map (embedding) and its original date
        c.execute("UPDATE items SET summary=?, value_score=?, category=?, status=?, error=NULL WHERE id=?",
                  (json.dumps(o), o["value_score"], o["category"], status, r["id"]))
    else:
        c.execute("UPDATE items SET summary=?, value_score=?, category=?, status=?, embedding=NULL, done_at=?, "
                  "error=NULL WHERE id=?", (json.dumps(o), o["value_score"], o["category"], status, int(time.time()), r["id"]))
    return f"{'kept' if status == 'done' else 'dropped'} ({o['value_score']}/10)"


def submit_fixcheck(c, a):
    r = _row(c, a["id"])
    if not r:
        return "unknown id"
    if a.get("verdict") not in patches.AI_SCHEMA["properties"]["verdict"]["enum"]:
        return f"verdict must be one of {patches.AI_SCHEMA['properties']['verdict']['enum']}"
    return "fix status: " + patches.save_ai(c, r, patches.clean_ai(a))


ANSWER_FILE = os.path.join(C.DATA_DIR, "answers", "answer.json")


def cmd_submit():
    path = sys.argv[2] if len(sys.argv) > 2 else None
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            return out({"error": f"can't read {path}: {e}"})
    else:
        raw = sys.stdin.read()
    try:
        d = json.loads(raw)
    except ValueError as e:
        return out({"error": f"not valid JSON: {e}"})
    kind = d.get("kind")
    fn = {"triage": submit_triage, "resort": submit_resort, "rewrite": submit_rewrite, "summarize": submit_summarize, "fixcheck": submit_fixcheck}.get(kind)
    if not fn:
        return out({"error": "kind must be triage, resort, summarize, rewrite or fixcheck"})
    c = conn()
    res = {}
    for a in d.get("answers") or []:
        try:
            res[a.get("id", "?")] = fn(c, a)
        except Exception as e:
            res[a.get("id", "?")] = f"error: {e}"
    c.commit()
    bad = {k: v for k, v in res.items() if v.startswith(("missing", "category", "interest", "checks", "verdict", "error"))}
    out({"saved": res, "fix_these": bad or None, "todo": counts(c, int(time.time())),
         "next": "run agent.py next" if not bad else "fix the listed answers and submit them again"})


def cmd_finish():
    c = conn()
    log = []
    rows = patches.due_items(c)
    for r in rows:
        patches.check_item(c, r, use_ai=False)
    log.append(f"database fix check: {len(rows)} stories")
    todo = c.execute("SELECT id, summary FROM items WHERE status IN ('done','low') AND embedding IS NULL").fetchall()
    import llm
    import numpy as np
    placed = 0
    if todo and llm.ensure_ollama():
        for r in todo:
            try:
                vec = np.asarray(llm.embed(PL.embed_text(json.loads(r["summary"]))), dtype=np.float32).tobytes()
            except Exception as e:
                log.append(f"linking stories needs the small model: ollama pull {C.EMBED_MODEL} ({str(e)[:80]})")
                break
            c.execute("UPDATE items SET embedding=? WHERE id=?", (vec, r["id"]))
            placed += 1
        c.commit()
        llm.unload(C.EMBED_MODEL)
    log.append(f"linked on the map: {placed} of {len(todo)} new stories")
    db.meta_set(c, "last_success", int(time.time()))
    out({"finished": log, "todo": counts(c, int(time.time()))})


def cmd_status():
    c = conn()
    out({"todo": counts(c, int(time.time())),
         "on_map": c.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0],
         "last_collect": db.meta_get(c, "last_full_collect")})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"collect": cmd_collect, "guide": cmd_guide, "next": lambda: cmd_next(sys.argv[2] if len(sys.argv) > 2 else None),
     "submit": cmd_submit, "finish": cmd_finish, "status": cmd_status}.get(cmd, cmd_status)()
