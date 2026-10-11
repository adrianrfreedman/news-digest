# News digest

Morning email, one section per configured region, built from RSS feeds by `claude -p` and sent via Gmail SMTP. launchd runs it on the configured days/hour and at load; missed days catch up (oldest first, capped).

## Files
- `digest.py` — the whole run: lock → due slots → fetch → story grouping → per-region writers (guarded) → GN link decode → render → send → ledger. Model per stage is set in `[llm.*]` (defaults: Sonnet groups, Haiku writes).
- `config.example.toml` — template for `config.toml`: recipients, shadow mode, schedule, model per stage (`[llm.group|write]`, defaults shown commented out: backend `claude-cli` or any OpenAI-compatible API via the `openai` SDK, model, effort, budget, timeout; API keys in git-ignored `.env`, see `.env.example`), reader, regions, scoring.
- `sources.example.json` — template for `sources.json`: feeds per tier (T/C/LQ) and region.
- `prompt-group.md`, `prompt-write.md` — prompt templates; `{{...}}` filled from config.
- Local, git-ignored: `config.toml`, `sources.json`, `state.json` (last sent slot), `stories.jsonl` (7-day story memory), `sent/`, `logs/`.

## Config
`config.toml` is validated at startup (pydantic-settings): unknown keys or bad values exit with a list of errors. Any key can be overridden by env var `NEWS_DIGEST_<SECTION>__<KEY>` (e.g. `NEWS_DIGEST_SCHEDULE__HOUR=8`); `NEWS_DIGEST_CONFIG` points at another config file.

## Setup
1. `cp config.example.toml config.toml && cp sources.example.json sources.json && cp .env.example .env`, then edit all three. `admin` must be a Gmail account; its [app password](https://myaccount.google.com/apppasswords) is read from the OS keyring (`uv run keyring set smtp.gmail.com <admin>`; macOS Keychain, Windows Credential Locker or Linux Secret Service) or `GMAIL_APP_PASSWORD` in `.env`.
2. `uv sync` (Python 3.14; runtime deps: `pydantic-settings`, `python-dotenv`, `openai`).
3. `uv run python digest.py --install` — writes `~/Library/LaunchAgents/ai.newsdigest.daily.plist` from config and loads it. Re-run after changing the schedule.

## Run
- `uv run pytest` — slots/windows, config, prompts, writer guards, SMTP send (uses the example config).
- `uv run ruff check . && uv run ruff format --check .` — lint and format (CI runs both plus pytest on every PR).
- `uv run python digest.py --no-send` — full build to `logs/<date>/digest.html`; no email, no state change.
- `uv run python digest.py` — what launchd runs; no-op if nothing is due.
- Status: `launchctl print gui/$(id -u)/ai.newsdigest.daily`
- Uninstall: `launchctl bootout gui/$(id -u)/ai.newsdigest.daily && rm ~/Library/LaunchAgents/ai.newsdigest.daily.plist`

## Shadow mode
`shadow = true` in `config.toml`: sends to `admin` only, subject prefixed `[SHADOW] `. To go live: set `shadow = false` and list every reader in `recipients`. Health and failure emails always go to `admin` only.
