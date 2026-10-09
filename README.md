# News digest

Morning email, one section per configured region, built from RSS feeds by `claude -p` and sent via the claude.ai Gmail connector. launchd runs it on the configured days/hour and at load; missed days catch up (oldest first, capped).

## Files
- `digest.py` — the whole run: lock → due slots → fetch → Sonnet grouping → Haiku writers (guarded) → GN link decode → render → send → ledger.
- `config.example.toml` — template for `config.toml`: recipients, shadow mode, schedule, reader, regions, scoring.
- `sources.example.json` — template for `sources.json`: feeds per tier (T/C/LQ) and region.
- `prompt-group.md`, `prompt-write.md` — prompt templates; `{{...}}` filled from config.
- Local, git-ignored: `config.toml`, `sources.json`, `state.json` (last sent slot), `stories.jsonl` (7-day story memory), `sent/`, `logs/`.

## Setup
1. `cp config.example.toml config.toml && cp sources.example.json sources.json`, then edit both.
2. `uv sync` (Python 3.14, stdlib only at runtime).
3. `uv run python digest.py --install` — writes `~/Library/LaunchAgents/ai.newsdigest.daily.plist` from config and loads it. Re-run after changing the schedule.

## Run
- `uv run pytest` — slots/windows, config, prompts, writer and send guards (uses the example config).
- `uv run python digest.py --no-send` — full build to `logs/<date>/digest.html`; no email, no state change.
- `uv run python digest.py` — what launchd runs; no-op if nothing is due.
- Status: `launchctl print gui/$(id -u)/ai.newsdigest.daily`
- Uninstall: `launchctl bootout gui/$(id -u)/ai.newsdigest.daily && rm ~/Library/LaunchAgents/ai.newsdigest.daily.plist`

## Shadow mode
`shadow = true` in `config.toml`: sends to `admin` only, subject prefixed `[SHADOW] `. To go live: set `shadow = false` and list every reader in `recipients`.
