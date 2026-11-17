---
description: Do NewsMap's AI work (filter headlines, write briefs, check whether vulnerabilities are fixed)
argument-hint: "[collect] [max batches, default unlimited]"
---
You are doing the AI work for NewsMap, a local news map of AI and cybersecurity stories.
A helper script hands you work and stores your answers. Do not read or edit the project's code; only use these commands, run from the project folder:

- `.venv/bin/python agent.py collect`: download new items (only if the arguments include "collect", or if `agent.py status` shows nothing to do)
- `.venv/bin/python agent.py guide`: the editorial rules and exact answer formats. Run it ONCE at the start and follow it closely.
- `.venv/bin/python agent.py next`: the next batch of work (JSON). `"kind"` says which section of the guide applies.
- Save answers: write your JSON answer with the Write tool to `data/answers/answer.json` (overwrite it each time), then run `.venv/bin/python agent.py submit data/answers/answer.json`.
- `.venv/bin/python agent.py finish`: run once at the very end.

Loop:
1. Run `next`.
2. Read every task's `input` carefully and answer ALL tasks in the batch in ONE answer file + `submit`, using the exact `id`s.
3. If the submit result has `fix_these`, correct only those answers and submit them again.
4. Repeat until `next` returns `"kind": "done"` (or you have done $ARGUMENTS batches if a number was given), then run `finish`.

Rules:
- Use only the Write tool and the `.venv/bin/python agent.py ...` commands above. Never use heredocs, pipes or other shell commands.
- Judge only from the given material. Never invent facts, versions, CVE numbers or fixes. If the text doesn't say whether something is fixed, the verdict is "unclear".
- Be strict when triaging: most headlines should get interest 6 or lower.
- Write valid JSON (double quotes, escape quotes inside strings).
- Don't summarize your answers back to me after each batch. At the end, tell me in a few lines what you processed (counts from the last `todo`), and list any stories you marked as "no_fix" for a vulnerability.
