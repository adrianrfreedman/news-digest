# News digest

Morning email (UK → World → US) built from RSS feeds by `claude -p` and sent via the claude.ai Gmail connector. launchd runs it Sun–Fri 07:00 local and at login/load; missed days catch up (max 3, oldest first). Spec: `../map.md` + `../issues/`.

## Files
- `digest.py` — the whole run: lock → due slots → fetch → Sonnet grouping → 3 Haiku writers (guarded) → GN link decode → render → send → ledger.
- `sources.json` — feeds per tier (T/C/LQ) and region; edit freely.
- `prompt-group.md`, `prompt-write.md` — grouping/scoring and writer prompts.
- `ai.newsdigest.daily.plist` — LaunchAgent.
- Runtime (git-ignored): `state.json` (last sent slot), `stories.jsonl` (7-day story memory), `sent/<date>.html`, `logs/run-*.json` (cost, timings, warnings, feeds) and `logs/<date>/` (raw model I/O).

## Run
- `/usr/bin/python3 digest.py --selftest` — slot/catch-up and writer-guard asserts.
- `/usr/bin/python3 digest.py --no-send` — full build to `logs/<date>/digest.html`, no send, no state.
- `/usr/bin/python3 digest.py` — what launchd runs; no-op if nothing is due.

## Shadow mode
`SHADOW = True` at the top of `digest.py`: sends to admin@example.com only, subject prefixed `[SHADOW] `. To go live: set `SHADOW = False` and uncomment the `RECIPIENTS` line with reader.

## Install / uninstall
- Install: `cp ai.newsdigest.daily.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.newsdigest.daily.plist`
- Status: `launchctl print gui/$(id -u)/ai.newsdigest.daily`
- Uninstall: `launchctl bootout gui/$(id -u)/ai.newsdigest.daily && rm ~/Library/LaunchAgents/ai.newsdigest.daily.plist`
