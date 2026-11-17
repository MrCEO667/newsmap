"""Local AI via Ollama, with a GPU-politeness gate."""
import json
import subprocess
import time

import requests

import config as C

NO_WINDOW = 0x08000000 if C.IS_WIN else 0   # creationflags must be 0 off Windows


def ollama_up():
    try:
        requests.get(C.OLLAMA_URL + "/api/version", timeout=3)
        return True
    except requests.RequestException:
        return False


def ensure_ollama():
    if ollama_up():
        return True
    try:
        subprocess.Popen([C.OLLAMA_EXE, "serve"], creationflags=(NO_WINDOW | 0x00000008) if C.IS_WIN else 0,
                         start_new_session=not C.IS_WIN,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        return False
    for _ in range(30):
        time.sleep(1)
        if ollama_up():
            return True
    return False


def gpu_free_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        total, used = [int(x) for x in out.strip().splitlines()[0].split(",")]
        return total - used
    except Exception:
        return None


def free_commit_mb():
    """Windows 'available commit' (RAM + page file headroom): when this hits 0, apps crash."""
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return int(m.ullAvailPageFile / 2 ** 20)
    except Exception:
        return None


def unload(name):
    try:
        requests.post(C.OLLAMA_URL + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


def model_loaded(name=C.LLM_MODEL):
    try:
        ps = requests.get(C.OLLAMA_URL + "/api/ps", timeout=5).json()
        return any(m.get("name") == name or m.get("model") == name for m in ps.get("models", []))
    except Exception:
        return False


def gpu_ready():
    """(ok, free_mb). OK if our model is already loaded or enough VRAM is free."""
    if model_loaded():
        return True, None
    free = gpu_free_mb()
    return (free is None or free >= C.MIN_FREE_VRAM_MB), free


def _post(path, payload, timeout, retries=3):
    """POST to Ollama; on 5xx / connection drops (e.g. a model runner crashed because RAM
    was momentarily full) wait and retry instead of failing the item."""
    for attempt in range(retries + 1):
        try:
            r = requests.post(C.OLLAMA_URL + path, json=payload, timeout=timeout)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout):
            if attempt >= retries:
                raise
            ensure_ollama()
            time.sleep(15 * (attempt + 1))


def chat_json(system, user, schema, num_predict=900, temperature=0.2):
    payload = {
        "model": C.LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "think": False, "stream": False, "format": schema, "keep_alive": C.KEEP_ALIVE,
        "options": {"num_ctx": C.NUM_CTX, "temperature": temperature, "num_predict": num_predict},
    }
    content = _post("/api/chat", payload, timeout=900)["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # small models occasionally stop early or return nothing: one retry with more room
        payload["options"].update(num_predict=int(num_predict * 1.6), temperature=max(temperature, 0.4))
        content = _post("/api/chat", payload, timeout=900)["message"]["content"]
        return json.loads(content)


def embed(text):
    opts = {"num_thread": C.EMBED_THREADS, "num_ctx": 2048}   # nomic's real max; bigger only wastes RAM
    if C.EMBED_ON_CPU:
        opts["num_gpu"] = 0
    return _post("/api/embed", {
        "model": C.EMBED_MODEL, "input": ["search_document: " + text[:2000]],
        "keep_alive": C.KEEP_ALIVE, "options": opts}, timeout=300)["embeddings"][0]


def unload_all():
    for m in (C.LLM_MODEL, C.EMBED_MODEL):
        try:
            requests.post(C.OLLAMA_URL + "/api/generate", json={"model": m, "keep_alive": 0}, timeout=30)
        except Exception:
            pass
