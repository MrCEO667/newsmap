"""NewsMap local web app: http://127.0.0.1:8765  (tiny stdlib server, ~20 MB RAM)."""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import datetime as dt
import json
import mimetypes
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np
import psutil

import config as C
import db

STATIC = os.path.join(C.ROOT, "static")
_cache = {}


def conn():
    return db.connect(init=False)


def latest_run(c):
    r = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None, False
    alive = False
    if r["state"] in ("running", "paused", "waiting") and r["pid"]:
        try:
            p = psutil.Process(r["pid"])
            alive = p.is_running() and "python" in p.name().lower()
        except psutil.Error:
            alive = False
    return r, alive


def next_scheduled():
    now = dt.datetime.now()
    cands = []
    for t in C.SCHEDULE_TIMES:
        h, m = map(int, t.split(":"))
        d = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if d <= now:
            d += dt.timedelta(days=1)
        cands.append(d)
    return int(min(cands).timestamp())


def status():
    c = conn()
    r, alive = latest_run(c)
    run = None
    if r:
        run = json.loads(r["status"] or "{}")
        run.update(id=r["id"], started_at=r["started_at"], finished_at=r["finished_at"], control=r["control"])
        state = r["state"]
        if state in ("running", "paused", "waiting") and not alive:
            state = "interrupted"
        run["state"] = state
    lib = dict(
        done=c.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0],
        waiting=c.execute("SELECT COUNT(*) FROM items WHERE status='triaged'").fetchone()[0],
        last_success=db.meta_get(c, "last_success"))
    c.close()
    return dict(running=alive, run=run, library=lib, next_scheduled=next_scheduled(), now=int(time.time()))


def graph():
    c = conn()
    since = int(time.time()) - (C.BACKFILL_DAYS + 1) * 86400
    sig = c.execute("SELECT COUNT(*), MAX(done_at) FROM items WHERE status IN ('done','low')").fetchone()
    key = (tuple(sig), since // 3600)
    if _cache.get("key") == key:
        c.close()
        return _cache["val"]
    rows = c.execute("SELECT id, summary, category, value_score, created_at, done_at, sources, engagement, "
                     "embedding, status FROM items WHERE status IN ('done','low') AND created_at>=? "
                     "ORDER BY created_at", (since,)).fetchall()
    c.close()
    nodes, vecs, vidx = [], [], []
    for r in rows:
        s = json.loads(r["summary"])
        srcs = json.loads(r["sources"])
        nodes.append(dict(id=r["id"], t=s["headline"], c=r["category"], v=r["value_score"], d=r["created_at"],
                          a=r["done_at"], s=sorted({x.get("source", "") for x in srcs}), g=s.get("tags", []),
                          e=r["engagement"], low=r["status"] == "low"))
        if r["embedding"]:
            vecs.append(np.frombuffer(r["embedding"], dtype=np.float32))
            vidx.append(len(nodes) - 1)
    links = []
    if len(vecs) > 1:
        M = np.vstack(vecs)
        M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
        S = M @ M.T
        np.fill_diagonal(S, -1)
        k = min(4, len(vecs) - 1)
        seen = set()
        for i in range(len(vecs)):
            top = np.argpartition(-S[i], k - 1)[:k] if k < len(vecs) else range(len(vecs))
            top = sorted(top, key=lambda j: -S[i, j])
            for rank, j in enumerate(top):
                sim = float(S[i, j])
                if rank > 0 and sim < 0.62:
                    continue
                a, b = sorted((vidx[i], vidx[j]))
                if (a, b) not in seen:
                    seen.add((a, b))
                    links.append(dict(source=nodes[a]["id"], target=nodes[b]["id"], s=round(sim, 3)))
    val = dict(nodes=nodes, links=links,
               categories=[dict(name=n, color=col, group=g) for n, col, g in C.CATEGORIES])
    _cache.update(key=key, val=val)
    return val


def item(iid):
    c = conn()
    r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    c.close()
    if not r:
        return None
    return dict(id=r["id"], url=r["url"], title=r["title"], created_at=r["created_at"], done_at=r["done_at"],
                sources=json.loads(r["sources"]), category=r["category"], value_score=r["value_score"],
                interest=r["interest"], status=r["status"], summary=json.loads(r["summary"] or "{}"))


def start_worker():
    c = conn()
    _, alive = latest_run(c)
    c.close()
    if alive:
        return False
    pyw = os.path.join(C.ROOT, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    subprocess.Popen([pyw, os.path.join(C.ROOT, "pipeline.py"), "--mode", "auto"], cwd=C.ROOT,
                     creationflags=0x00000008 | 0x00000200 | 0x00004000,  # detached, own group, below-normal
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    return True


def control(action):
    if action not in ("pause", "resume", "stop"):
        return False
    c = conn()
    r, alive = latest_run(c)
    if r and alive:
        c.execute("UPDATE runs SET control=? WHERE id=?", ("" if action == "resume" else action, r["id"]))
        c.commit()
    c.close()
    return bool(r and alive)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/status":
                return self._send(200, status())
            if u.path == "/api/graph":
                return self._send(200, graph())
            if u.path == "/api/item":
                it = item(q.get("id", [""])[0])
                return self._send(200 if it else 404, it or {"error": "not found"})
            path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, path.replace("static/", "", 1)))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                return self._send(200, f.read(), ctype)
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlsplit(self.path)
        if self.headers.get("Origin", "").rstrip("/") not in ("", f"http://127.0.0.1:{C.SERVER_PORT}",
                                                               f"http://localhost:{C.SERVER_PORT}"):
            return self._send(403, {"error": "forbidden"})
        if u.path == "/api/refresh":
            ok = start_worker()
            return self._send(200 if ok else 409, {"started": ok})
        if u.path == "/api/control":
            n = int(self.headers.get("Content-Length") or 0)
            action = json.loads(self.rfile.read(n) or b"{}").get("action", "")
            return self._send(200, {"ok": control(action)})
        return self._send(404, {"error": "not found"})


class ExclusiveServer(ThreadingHTTPServer):
    """On Windows SO_REUSEADDR lets several processes share a port; insist on exclusive use so a
    second launch exits instead of running a duplicate server."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    db.connect().close()   # make sure the schema exists
    try:
        srv = ExclusiveServer(("127.0.0.1", C.SERVER_PORT), Handler)
    except OSError:
        return  # already running
    srv.serve_forever()


if __name__ == "__main__":
    main()
