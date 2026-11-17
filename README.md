# NewsMap

A local 3D map of early AI and cybersecurity news. A local model (Qwen3-8B via Ollama) filters out noise and writes a one-page brief for each story worth reading.

**Open it:** double-click `NewsMap.cmd` (or go to http://127.0.0.1:8765 while the server runs).

## How it works
1. **Collect.** Gathers items from these sources:
   - Hacker News (100+ points)
   - Lobsters (ai/security/reversing/privacy)
   - Reddit (r/netsec, r/MachineLearning, r/LocalLLaMA, r/ReverseEngineering, r/blueteamsec)
   - Hugging Face Daily Papers (20+ upvotes)
   - CISA exploited CVEs and GitHub critical advisories
   - About 90 popular HN blogs (the list Karpathy shared)
   - AINews, Import AI, OpenAI, DeepMind, Project Zero, watchTowr, tl;dr sec, Krebs, Trail of Bits, PortSwigger, and the oss-security and Full Disclosure mailing lists
2. **Filter noise.** The AI rates every headline in batches. Anything off-topic, or scoring below 6/10, is dropped.
3. **Read & summarize.** Downloads the article plus the top comments from HN, Lobsters and Reddit, then writes the brief: TL;DR, key points, why it matters, insider notes from the discussion, and a value score. Stories that score below 6 after reading are hidden (tick "dropped" to see them).
4. **Map.** Small embeddings (computed on the CPU) place similar stories close together, grouped by topic.

## Fix tracker (is the vulnerability patched?)
For every vulnerability story, `patches.py` looks up whether a fix exists:
- **OSV.dev**: fixed package versions, fixing commits, Linux distro patches
- **NVD**: patch links and the vendor's advisory (only when OSV has nothing)
- **CISA KEV**: whether it is exploited in the wild, plus the official required action
- **AI fallback**: reads the article and vendor advisory when the databases have no answer

Each story shows a badge (Fixed / Partly fixed / No fix yet / Fix unknown / Exploited) and a "Fix status" section.
Use the fix-status menu at the top to filter. Anything not fixed is re-checked every 12 hours for 60 days.

## Using Claude (Haiku) instead of the local model (macOS)
On a small Mac the local model doesn't fit, so the AI work can be done by Claude Code instead:
- `agent.py` hands out work in batches (`collect`, `guide`, `next`, `submit`, `finish`, `status`) and saves the answers
  using the same prompts and scoring as the local pipeline.
- `.claude/commands/process-news.md` is the `/process-news` command that drives it. Briefs are written in plain
  English, with a "Words explained" box.
- Run it by hand: `claude --model haiku`, then `/process-news collect`
- Or without a window: `claude -p "/process-news collect 25" --model haiku --allowedTools "Bash(.venv/bin/python agent.py:*)" "Edit(./data/answers/**)"`
  (the author's home server runs this at 08:00 and 20:00, and when Refresh is pressed with `NEWSMAP_AI=claude`).

Environment variables: `NEWSMAP_PORT` (default 8765), `NEWSMAP_LLM_MODEL`, `NEWSMAP_AI=claude` (Refresh asks for a Claude run).

## Being gentle with the PC
- Runs at below-normal CPU priority. Feed downloads are spaced out per site.
- Before each AI call it checks GPU memory. If other apps hold more than ~2.2 GB, it **waits** instead of competing.
- The model is unloaded from VRAM as soon as the update finishes.
- Use Pause or Stop in the progress panel at any time. Work is saved after every item, and the next run continues where this one stopped.

## Schedule
`setup_schedule.ps1` registers the task "NewsMap update" at 08:00 and 20:00. If the PC was off at those times, the task runs at the next startup.

## Settings
Edit `config.py`:
- Sources and thresholds
- How many summaries per run
- The VRAM requirement
- Model names

Data lives in `data\newsmap.db`, and the log in `data\worker.log`.

## Testing a small run
    .venv\Scripts\python.exe pipeline.py --mode backfill --days 1 --sources hn,kev --max-summaries 3
