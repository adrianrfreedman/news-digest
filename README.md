# News digest

Morning email, one section per configured region, built from RSS feeds by `claude -p` and sent via Gmail SMTP. Runs on macOS, Linux and Windows: the OS scheduler fires it on the configured days/hour, and missed days catch up when the computer is back on (oldest first, capped).

## Files
- `digest.py` — the whole run: lock → due slots → fetch → story grouping → per-region writers (guarded) → GN link decode → render → send → ledger. Model per stage is set in `[llm.*]` (defaults: Sonnet groups, Haiku writes).
- `config.example.toml` — template for `config.toml`: recipients, shadow mode, schedule, model per stage (`[llm.group|write]`, defaults shown commented out: backend `claude-cli` or any OpenAI-compatible API via the `openai` SDK, model, effort, budget, timeout; API keys in git-ignored `.env`, see `.env.example`), reader, regions, scoring.
- `sources.example.json` — template for `sources.json`: feeds per tier (T/C/LQ) and region.
- `templates/` — Jinja2 templates: `digest.html.j2` (the email, autoescaped, inline styles) and `prompt-group.md`, `prompt-write.md` (prompts, filled from config; an undefined variable raises). `tests/snapshots/` pins their rendered output; delete a file and rerun pytest to regenerate it.
- Local, git-ignored: `config.toml`, `sources.json`, `state.json` (last sent slot), `stories.jsonl` (7-day story memory), `sent/`, `logs/`.

## Config
`config.toml` is validated at startup (pydantic-settings): unknown keys or bad values exit with a list of errors. Any key can be overridden by env var `NEWS_DIGEST_<SECTION>__<KEY>` (e.g. `NEWS_DIGEST_SCHEDULE__HOUR=8`); `NEWS_DIGEST_CONFIG` points at another config file.

## Setup
1. `cp config.example.toml config.toml && cp sources.example.json sources.json && cp .env.example .env`, then edit all three. `admin` must be a Gmail account; its [app password](https://myaccount.google.com/apppasswords) is read from the OS keyring (`uv run keyring set smtp.gmail.com <admin>`; macOS Keychain, Windows Credential Locker or Linux Secret Service) or `GMAIL_APP_PASSWORD` in `.env`.
2. `uv sync` (Python 3.14). Install [Claude Code](https://claude.com/claude-code) and log in once interactively; the job runs `claude -p` (unless every stage uses `backend = "openai"`).
3. `uv run python digest.py --install` — schedules the run from config in the computer's local time (set `[schedule].timezone` to match). Re-run after changing the schedule.

| OS | Scheduler | Missed slot | Status | Uninstall |
|---|---|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/ai.newsdigest.daily.plist` | runs at load/login | `launchctl print gui/$(id -u)/ai.newsdigest.daily` | `launchctl bootout gui/$(id -u)/ai.newsdigest.daily && rm ~/Library/LaunchAgents/ai.newsdigest.daily.plist` |
| Linux | systemd user timer `~/.config/systemd/user/ai.newsdigest.daily.timer` | `Persistent=true` (next boot); `loginctl enable-linger` to run while logged out | `systemctl --user list-timers ai.newsdigest.daily.timer` | `systemctl --user disable --now ai.newsdigest.daily.timer` |
| Windows | Task Scheduler task `ai.newsdigest.daily` (runs `pythonw`, no console) | `StartWhenAvailable` | `schtasks /Query /TN ai.newsdigest.daily` | `schtasks /Delete /TN ai.newsdigest.daily /F` |

On a headless Linux box without Secret Service, put the Gmail password in `.env` instead of the keyring.

## Run
- `uv run pytest` — slots/windows, config, prompts, writer guards, SMTP send (uses the example config).
- `uv run ruff check . && uv run ruff format --check .` — lint and format (CI runs both plus pytest on macOS, Linux and Windows for every PR).
- `uv run python digest.py --no-send` — full build to `logs/<date>/digest.html`; no email, no state change.
- `uv run python digest.py` — what the scheduler runs; no-op if nothing is due.

## Shadow mode
`shadow = true` in `config.toml`: sends to `admin` only, subject prefixed `[SHADOW] `. To go live: set `shadow = false` and list every reader in `recipients`. Health and failure emails always go to `admin` only.
