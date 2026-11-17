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
