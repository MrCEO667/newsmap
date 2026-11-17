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
LOBSTERS_TAGS = "security,reversing"
LOBSTERS_MIN_SCORE = 8
REDDIT_SUBS = ["netsec", "ReverseEngineering", "blueteamsec"]
HF_MIN_UPVOTES = 20
OPML_URL = ("https://gist.githubusercontent.com/emschwartz/e6d2bf860ccc367fe37ff953ba6de66b"
            "/raw/hn-popular-blogs-2025.opml")
EXTRA_FEEDS = [
    # research teams that publish vulnerability write-ups
    ("Project Zero", "https://googleprojectzero.blogspot.com/feeds/posts/default"),
    ("watchTowr Labs", "https://labs.watchtowr.com/rss/"),
    ("Horizon3.ai", "https://www.horizon3.ai/feed/"),
    ("Rapid7", "https://www.rapid7.com/blog/rss/"),
    ("Qualys", "https://blog.qualys.com/feed"),
    ("GitHub Security Lab", "https://github.blog/tag/github-security-lab/feed/"),
    ("Cisco Talos", "https://blog.talosintelligence.com/rss/"),
    ("Tenable", "https://www.tenable.com/blog/feed"),
    ("Trail of Bits", "https://blog.trailofbits.com/feed/"),
    ("PortSwigger Research", "https://portswigger.net/research/rss"),
    ("tl;dr sec", "https://tldrsec.com/feed"),
    # security news
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("SecurityWeek", "https://www.securityweek.com/feed/"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
    # mailing lists where flaws are disclosed
    ("oss-security", "https://seclists.org/rss/oss-sec.rss"),
    ("Full Disclosure", "https://seclists.org/rss/fulldisclosure.rss"),
]
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

# Which collectors run (removed "hf": Hugging Face papers are AI research, not vulnerabilities)
SOURCES = ["hn", "lobsters", "reddit", "kev", "ghsa", "rss"]

# Sources whose own text is already the content (no article fetch needed)
SELF_CONTAINED = {"HF Papers", "CISA KEV", "GitHub Advisory"}

# ---- Map topics (name, color, group) -----------------------------------------
# NewsMap only tracks security flaws (vulnerabilities) and exploits. Categories say WHAT is affected.
CATEGORIES = [
    ("Browsers & phones", "#6ea8ff", "Devices"),           # Chrome, Safari, Firefox, iOS, Android, apps on them
    ("Windows, Mac & Linux", "#a78bfa", "Devices"),        # operating systems and their built-in parts
    ("Hardware & smart devices", "#2dd4bf", "Devices"),    # CPUs, chips, firmware, routers for home, cameras, IoT, cars
    ("Network & VPN gear", "#f87171", "Business"),         # firewalls, VPN gateways, enterprise routers (Cisco, Fortinet...)
    ("Business & cloud software", "#fb923c", "Business"),  # Exchange, SharePoint, SAP, Active Directory, SaaS, cloud
    ("Websites & web servers", "#facc15", "Web"),          # WordPress & plugins, CMS, web frameworks, web servers
    ("Open-source code & packages", "#a3e635", "Web"),     # libraries, npm/PyPI packages, supply-chain attacks
    ("AI tools & models", "#f472b6", "Web"),               # flaws in AI apps, agents, model servers, prompt injection
    ("Exploit techniques & research", "#22d3ee", "Research"),  # methods, tools, write-ups not about one product
]
# old topics from before the focus change; stories still in them are hidden until re-sorted
LEGACY_CATEGORIES = ["AI models & releases", "AI research", "AI agents & tools", "AI safety & security",
                     "AI industry & policy", "Compute & hardware", "Vulnerabilities & exploits",
                     "Malware & threat actors", "Breaches & incidents", "Security research & tools",
                     "Privacy & surveillance", "Other tech"]
CATEGORY_NAMES = [c[0] for c in CATEGORIES]

SCHEDULE_TIMES = ["08:00", "20:00"]
