"""NewsMap settings. Edit values here; no code changes needed."""
import os
import shutil
import sys

IS_WIN = sys.platform == "win32"

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "newsmap.db")
LOG_PATH = os.path.join(DATA_DIR, "worker.log")
LOCK_PATH = os.path.join(DATA_DIR, "worker.lock")
FEEDS_CACHE = os.path.join(DATA_DIR, "hn-popular-blogs.opml")

SERVER_PORT = int(os.environ.get("NEWSMAP_PORT", "8765"))

# ---- Local AI (Ollama) -------------------------------------------------------
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_EXE = r"D:\Ollama\ollama.exe" if IS_WIN else (shutil.which("ollama") or "/usr/local/bin/ollama")
LLM_MODEL = os.environ.get("NEWSMAP_LLM_MODEL",
                           "hf.co/DreamFast/qwen3-8b-heretic:Q4_K_M" if IS_WIN else "qwen3:8b")
EMBED_MODEL = "nomic-embed-text"
NUM_CTX = 8192            # context window; bigger = more VRAM
KEEP_ALIVE = "30m"        # stays loaded through short waits; every run unloads it explicitly when it ends
EMBED_ON_CPU = True       # keep the small embedding model off the GPU
EMBED_THREADS = 2

# ---- Be gentle with a busy PC ------------------------------------------------
MIN_FREE_VRAM_MB = 5800   # wait (don't fight) if other apps hold the GPU
MIN_FREE_COMMIT_MB = 150  # model loaded: release it if RAM+pagefile headroom falls below this
                          # (this PC sits at ~0.3 GB free with the model loaded; 0 = apps crash)
RECYCLE_MODEL_EVERY = 40     # reload the model every N summaries (its RAM grows ~1.5 GB/hour otherwise)
MODEL_LOAD_COMMIT_MB = 4800  # loading Qwen3-8B on Windows/CUDA commits ~4.2 GB (measured) + 0.5 GB spare
GPU_WAIT_CHECK_S = 20
PAUSE_BETWEEN_LLM_S = 0.4 # small breather between AI calls
FETCH_THREADS = 4         # parallel feed downloads

# ---- What counts as "recent" and how much to process -------------------------
BACKFILL_DAYS = 30
MIN_LOOKBACK_DAYS = 3     # updates re-check the last 3 days (scores grow late)
MAX_SUMMARIES_BACKFILL = 400
MAX_SUMMARIES_UPDATE = 90
TRIAGE_BATCH = 15
TRIAGE_MIN_INTEREST = 6   # stage 1 gate (headline level)
VALUE_MIN = 6             # stage 2 gate: weighted yes/no quality checklist (0-10) after reading

# ---- Sources -----------------------------------------------------------------
HTTP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) newsmap/0.1 (personal news reader)"
HN_MIN_POINTS = 100
LOBSTERS_TAGS = "ai,security,reversing,privacy"
LOBSTERS_MIN_SCORE = 8
REDDIT_SUBS = ["netsec", "MachineLearning", "LocalLLaMA", "ReverseEngineering", "blueteamsec"]
HF_MIN_UPVOTES = 20
OPML_URL = ("https://gist.githubusercontent.com/emschwartz/e6d2bf860ccc367fe37ff953ba6de66b"
            "/raw/hn-popular-blogs-2025.opml")
EXTRA_FEEDS = [
    ("AINews (smol.ai)", "https://news.smol.ai/rss.xml"),
    ("Import AI", "https://importai.substack.com/feed"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
    ("Project Zero", "https://googleprojectzero.blogspot.com/feeds/posts/default"),
    ("watchTowr Labs", "https://labs.watchtowr.com/rss/"),
    ("tl;dr sec", "https://tldrsec.com/feed"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
    ("Trail of Bits", "https://blog.trailofbits.com/feed/"),
    ("PortSwigger Research", "https://portswigger.net/research/rss"),
    ("oss-security", "https://seclists.org/rss/oss-sec.rss"),
    ("Full Disclosure", "https://seclists.org/rss/fulldisclosure.rss"),
]
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

# Sources whose own text is already the content (no article fetch needed)
SELF_CONTAINED = {"HF Papers", "CISA KEV", "GitHub Advisory"}

# ---- Map topics (name, color, group) -----------------------------------------
CATEGORIES = [
    ("AI models & releases", "#6ea8ff", "AI"),
    ("AI research", "#a78bfa", "AI"),
    ("AI agents & tools", "#22d3ee", "AI"),
    ("AI safety & security", "#f472b6", "AI"),
    ("AI industry & policy", "#818cf8", "AI"),
    ("Compute & hardware", "#2dd4bf", "AI"),
    ("Vulnerabilities & exploits", "#f87171", "Security"),
    ("Malware & threat actors", "#fb923c", "Security"),
    ("Breaches & incidents", "#facc15", "Security"),
    ("Security research & tools", "#fdba74", "Security"),
    ("Privacy & surveillance", "#a3e635", "Security"),
    ("Other tech", "#94a3b8", "Other"),
]
CATEGORY_NAMES = [c[0] for c in CATEGORIES]

SCHEDULE_TIMES = ["08:00", "20:00"]
