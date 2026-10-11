"""Daily news digest: RSS fetch → story grouping → per-region writers → HTML → Gmail SMTP send.
Model per stage is configurable ([llm.*]). Config: config.toml. Usage: digest.py [--no-send | --install]. Tests: uv run pytest"""
import difflib
import fcntl
import json
import math
import os
import plistlib
import re
import shutil
import signal
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import tomllib
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
from functools import cache, partial
from html import escape as _esc, unescape
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource

# ---------- config ----------
APP = Path(__file__).resolve().parent
LOGS, SENT = APP / "logs", APP / "sent"
STATE, LEDGER = APP / "state.json", APP / "stories.jsonl"
CONFIG = Path(os.environ.get("NEWS_DIGEST_CONFIG", APP / "config.toml"))  # env override is for tests
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # index = date.weekday()
load_dotenv(APP / ".env", override=False)  # API keys, Gmail app password; real env wins


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mail(Strict):
    admin: str  # also the Gmail account that sends (SMTP login)
    recipients: list[str] = Field(min_length=1)
    shadow: bool  # admin only, "[SHADOW] " prefix

    @model_validator(mode="after")
    def admin_receives(self):
        if not self.shadow and self.admin not in self.recipients:
            raise ValueError("admin must be in recipients when shadow = false")
        return self


class Schedule(Strict):
    timezone: str
    tz_label: str
    hour: int = Field(ge=0, le=23)
    send_days: list[Literal[WEEKDAYS]] = Field(min_length=1)
    catchup_days: int = Field(ge=1)  # max digests per run; older due slots are skipped (0 would mean no cap)

    @field_validator("timezone")
    @classmethod
    def known_tz(cls, v):
        try:
            ZoneInfo(v)
        except (ValueError, KeyError):
            raise ValueError(f"unknown timezone {v!r}") from None
        return v


class Region(Strict):
    name: str
    color: str = Field(pattern=r"^#([0-9a-fA-F]{3}){1,2}$")
    rule: str


class Tiers(Strict):
    lead: int = Field(ge=0)
    main: int = Field(ge=0)
    brief: int = Field(ge=0)


class News(Strict):
    sources: str
    reader: str
    counterpoint_desc: str = "outlets whose framing is shown for contrast"  # who the C tier is, in prompts + legend
    low_quality_desc: str = "tabloids, clickbait, forums"
    proximity: str
    significance: str
    key_examples: str
    score_examples: list[str]
    categories: list[str]
    threshold: int
    threshold_long: int
    caps: Tiers  # stories per region per tier
    link_caps: Tiers  # links per story
    regions: list[Region] = Field(min_length=1)

    @field_validator("regions")
    @classmethod
    def unique_names(cls, v):
        if len({r.name for r in v}) < len(v):
            raise ValueError("region names must be unique")
        return v


class Stage(Strict):
    backend: Literal["claude-cli", "openai"] = "claude-cli"
    model: str
    effort: str = ""
    budget_usd: float = Field(gt=0)  # claude-cli only
    timeout_s: int = Field(gt=0)
    base_url: str | None = None  # openai only, as are the two below
    api_key_env: str = "OPENAI_API_KEY"
    max_output_tokens: int = Field(16000, gt=0)


# Source of truth for stage defaults; config.example.toml shows them commented out.
STAGE_DEFAULTS = {"group": {"model": "sonnet", "effort": "low", "budget_usd": 0.90, "timeout_s": 600},
                  "write": {"model": "haiku", "effort": "", "budget_usd": 0.15, "timeout_s": 300}}


class Llm(Strict):
    group: Stage
    write: Stage

    @model_validator(mode="before")
    @classmethod
    def stage_defaults(cls, v):
        """Partial [llm.<stage>] tables keep that stage's own defaults."""
        return {**v, **{k: {**d, **v.get(k, {})} for k, d in STAGE_DEFAULTS.items()}} if isinstance(v, dict) else v


class Paths(Strict):
    claude: str | None = None  # default: claude on PATH, else ~/.local/bin/claude


class Settings(BaseSettings):
    """CONFIG toml, overridden by NEWS_DIGEST_<SECTION>__<KEY> env vars."""
    model_config = SettingsConfigDict(env_prefix="NEWS_DIGEST_", env_nested_delimiter="__", extra="forbid")
    mail: Mail
    schedule: Schedule
    paths: Paths = Paths()
    llm: Llm = Field(default_factory=Llm)
    news: News

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return init_settings, env_settings, TomlConfigSettingsSource(settings_cls, CONFIG)


if not CONFIG.exists():
    sys.exit(f"news-digest: {CONFIG} not found. Copy config.example.toml to config.toml and edit it.")
try:
    SETTINGS = Settings()
except tomllib.TOMLDecodeError as e:
    sys.exit(f"news-digest: {CONFIG}: {e}")
except ValidationError as e:
    sys.exit(f"news-digest: invalid {CONFIG}:\n" + "\n".join(
        f"  {'.'.join(map(str, err['loc'])) or '(top)'}: {err['msg']}" for err in e.errors()))
SHADOW, ADMIN, RECIPIENTS = SETTINGS.mail.shadow, SETTINGS.mail.admin, SETTINGS.mail.recipients
TZ, UTC, HOUR = ZoneInfo(SETTINGS.schedule.timezone), timezone.utc, SETTINGS.schedule.hour
SEND_DAYS = {WEEKDAYS.index(d) for d in SETTINGS.schedule.send_days}
CATCHUP = SETTINGS.schedule.catchup_days
LLM = SETTINGS.llm.model_dump()  # {stage: {key: value}}
CLAUDE = SETTINGS.paths.claude or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
NEWS = SETTINGS.news
SOURCES = APP / NEWS.sources
REGIONS = [r.name for r in NEWS.regions]
RCOL = {r.name: r.color for r in NEWS.regions}
CATEGORIES = set(NEWS.categories)
CAPS, LINK_CAP = NEWS.caps.model_dump(), NEWS.link_caps.model_dump()
LIMITS = {"lead": "80–120 words", "main": "≤45 words", "brief": "headline only, ≤18 words"}
TRUSTED, COUNTERPOINT, LOW_QUALITY = "T", "C", "LQ"  # sources.json tiers
OPINION, TITLES_ONLY, MIXED, FRAMING = "OP", "TO", "M", "F"  # sources.json flags
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
KEY = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){1,5}$")
OPINION_URL = re.compile(r"/(opinion|opinions|commentary|comment|op-ed|editorial|columnists?|analysis)/", re.I)
OPINION_TITLE = re.compile(r"^(opinion|analysis|comment|editorial|op-ed|letters?)\b\s*[:|\-–]|\|\s*opinion\b", re.I)
AFP_NOISE = re.compile(r"AFP Fact Check|AFP Inside|^Fact Check", re.I)
PAY_RANK, TIER_RANK = {"": 0, "soft": 1, "hard": 2}, {TRUSTED: 0, COUNTERPOINT: 1, LOW_QUALITY: 2}
RUN = {"calls": [], "warnings": [], "errors": [], "slots": []}  # this run's log


def warn(msg):
    RUN["warnings"].append(msg)
    print("WARN", msg, file=sys.stderr)


# ---------- slots ----------
def slot_at(day):
    return datetime.combine(day, dtime(HOUR), tzinfo=TZ)


def due_slots(now, last_sent):
    """Unsent scheduled slots up to now, oldest first, newest CATCHUP kept → (slots, n_skipped).
    First run (no state) → latest slot only."""
    slots, day = [], now.astimezone(TZ).date()
    while day > now.astimezone(TZ).date() - timedelta(days=30):  # ponytail: 30-day lookback; fine while CATCHUP < 30
        s = slot_at(day)
        if last_sent and s <= last_sent:
            break
        if s <= now and day.weekday() in SEND_DAYS:
            slots.append(s)
            if not last_sent:
                break
        day -= timedelta(days=1)
    slots.reverse()
    return slots[-CATCHUP:], max(0, len(slots) - CATCHUP)


def window(slot):
    """Previous scheduled slot → this slot, in local wall time so DST is honoured."""
    day = slot.astimezone(TZ).date() - timedelta(days=1)
    while day.weekday() not in SEND_DAYS:
        day -= timedelta(days=1)
    return slot_at(day).astimezone(UTC), slot.astimezone(UTC)


def window_days(start, end):
    """Local days the window spans (>1 after a day off)."""
    s, e = start.astimezone(TZ).date(), end.astimezone(TZ).date()
    return [s + timedelta(days=n) for n in range((e - s).days)]


def window_hours(start, end):
    return round((end - start).total_seconds() / 3600)


def is_long_window(start, end):
    return len(window_days(start, end)) > 1


# ---------- fetch ----------
@cache
def https():
    """HTTPS handler trusting macOS system roots too: /etc/ssl/cert.pem lacks newer ones (ISRG Root X2)."""
    ctx = ssl.create_default_context()
    pem = subprocess.run(["security", "find-certificate", "-a", "-p", "/System/Library/Keychains/SystemRootCertificates.keychain"],
                         capture_output=True, text=True).stdout
    if pem:
        ctx.load_verify_locations(cadata=pem)
    return urllib.request.HTTPSHandler(context=ctx)


def opener():
    """Fresh cookie jar per call (GN decode needs its cookies kept between requests)."""
    return urllib.request.build_opener(https(), urllib.request.HTTPCookieProcessor())


def http_get(url, timeout=30):
    with opener().open(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"}), timeout=timeout) as r:
        return r.read()


def wait_network():
    """Probe until the network is up (after wake); add a grace period if it wasn't up at first."""
    for n in range(40):  # ponytail: 40 × 15s ≈ 10 min, then the run fails into the 15-min retry; lengthen if wakes keep failing
        try:
            socket.create_connection(("news.google.com", 443), 5).close()
            if n:
                time.sleep(30)
            return
        except OSError:
            time.sleep(15)
    raise RuntimeError("network not ready after ~10 min")


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(s or ""))).strip()


def norm(title):
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


def txt(el, *tags):
    for t in tags:
        f = el.find(t)
        if f is not None and (f.text or "").strip():
            return f.text.strip()
    return ""


def parse_date(s):
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def parse_feed(body):
    root, atom = ET.fromstring(body), "{http://www.w3.org/2005/Atom}"
    out = [(txt(i, "title"), txt(i, "link", "guid"), txt(i, "description"),
            txt(i, "pubDate", "{http://purl.org/dc/elements/1.1/}date"), txt(i, "source")) for i in root.iter("item")]
    for e in root.iter(atom + "entry"):
        link = e.find(atom + "link")
        out.append((txt(e, atom + "title"), link.get("href") if link is not None else "",
                    txt(e, atom + "summary", atom + "content"), txt(e, atom + "published", atom + "updated"), ""))
    return out


def fetch_feed(feed, start, end, when_h, scale, gn_locales):
    """→ (feed, items in window or None on error, raw item count, status, seconds)."""
    is_gn = "gn" in feed
    if is_gn:
        q = urllib.parse.quote(f"{feed['gn']} when:{when_h}h")
        url = f"https://news.google.com/rss/search?q={q}&{gn_locales[feed['loc']]}"
    else:
        url = feed["url"]
    t0 = time.time()
    try:
        try:
            raw = parse_feed(http_get(url))
        except Exception:  # noqa: BLE001 — one retry
            time.sleep(2)
            raw = parse_feed(http_get(url))
    except Exception as e:  # noqa: BLE001
        return feed, None, 0, f"{type(e).__name__}: {e}", time.time() - t0
    flags = feed.get("flags", "").split()
    items = []
    for title, link, desc, pub, source in raw:
        d = parse_date(pub) if pub else None
        if d is None or not (start <= d < end):
            continue
        title = clean(title)
        if is_gn:
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            desc = ""  # GN description repeats the title
        else:
            link = link.split("?")[0]
        if feed["outlet"] == "AFP" and AFP_NOISE.search(title):
            continue
        op = OPINION in flags or bool(OPINION_TITLE.search(title)) or (not is_gn and bool(OPINION_URL.search(link)))
        items.append({"outlet": feed["outlet"], "tier": feed["tier"], "region_hint": feed["region"],
                      "flags": [x for x in flags if x != OPINION] + ([OPINION] if op else []),
                      "pay": feed.get("pay", ""), "group": feed.get("group", feed["outlet"]), "gn": is_gn,
                      "published": d.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"), "title": title,
                      "snippet": clean(desc)[:160],  # ponytail: 160-char snippet; upgrade: full text for chosen stories
                      "url": link})
    cap = (40 if is_gn else 30) * scale  # ponytail: items per feed per window-day, bounds grouping input; raise if a key feed clips
    return feed, items[:cap], len(raw), f"{len(raw)} items, {len(items)} in window", time.time() - t0


def fetch_all(start, end):
    src = json.loads(SOURCES.read_text())
    feeds = src["feeds"]
    when_h = max(24, math.ceil((datetime.now(UTC) - start).total_seconds() / 3600))  # GN `when:` is relative to now
    scale = max(1, round((end - start).total_seconds() / 86400))
    fetch = partial(fetch_feed, start=start, end=end, when_h=when_h, scale=scale, gn_locales=src["gn_locales"])
    with ThreadPoolExecutor(max_workers=min(24, len(feeds))) as pool:
        results = list(pool.map(fetch, feeds))
    items, seen_url, seen_title, report = [], set(), set(), []
    for feed, feed_items, n_raw, status, secs in sorted(results, key=lambda r: "gn" in r[0]):  # native copies win dedupe
        name = f"{feed['outlet']}{' GN' if 'gn' in feed else ''}"
        report.append({"feed": name, "status": status, "s": round(secs, 1),
                       "n": len(feed_items) if feed_items is not None else None})
        if feed_items is None:
            warn(f"feed error: {name}: {status}")
        elif not n_raw:  # empty or dead; 0-in-window with older items is just a quiet feed
            warn(f"feed empty: {name}: 0 items")
        for it in feed_items or []:
            key = (it["group"], norm(it["title"]))
            if it["url"] in seen_url or key in seen_title:
                continue
            seen_url.add(it["url"])
            seen_title.add(key)
            items.append(it)
    return items, report


# ---------- model calls ----------
def claude(args, stdin, timeout, tag, run_dir, env=None):
    """claude -p with a hard timeout (kills the process group). Returns (result text, stats)."""
    cmd = [CLAUDE, "-p", "--output-format", "json", "--no-session-persistence", *args]
    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=APP, start_new_session=True, env=env)
    try:
        out, err = p.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        RUN["calls"].append({"tag": tag, "timeout": timeout})
        raise RuntimeError(f"{tag}: timed out after {timeout}s")
    (run_dir / f"raw-{tag}.json").write_text(out + ("\n--- stderr ---\n" + err if err else ""))
    try:
        res = json.loads(out)
    except ValueError:
        raise RuntimeError(f"{tag}: exit {p.returncode}, no JSON: {(err or out)[-300:]}")
    u = res.get("usage") or {}
    stat = {"tag": tag, "exit": p.returncode, "subtype": res.get("subtype"), "is_error": res.get("is_error"),
            "wall_s": round(time.time() - t0, 1), "cost_usd": round(res.get("total_cost_usd") or 0, 4),
            "models": list((res.get("modelUsage") or {})),
            "in_tok": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
            "out_tok": u.get("output_tokens"), "denials": len(res.get("permission_denials") or [])}
    RUN["calls"].append(stat)
    return res.get("result") or "", stat


def parse_json(text):
    # ponytail: first "{" to last "}" skips prose/fences around one object; upgrade: schema-constrained output
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def cli_args(stage, scale=1):
    """CLI args and timeout for a stage; scale multiplies budget and timeout (long windows)."""
    c = LLM[stage]
    args = ["--model", c["model"], "--max-budget-usd", str(c["budget_usd"] * scale)]
    return args + (["--effort", c["effort"]] if c["effort"] else []), c["timeout_s"] * scale


def tool_less_args(stage, scale=1):
    args, timeout = cli_args(stage, scale)
    return args + ["--tools", "", "--strict-mcp-config", "--permission-mode", "dontAsk"], timeout


def openai_call(stage, prompt, tag, run_dir, scale=1):
    """One chat completion against any OpenAI-compatible API (OpenRouter, OpenAI, Gemini, Ollama, ...)."""
    from openai import OpenAI  # lazy: only needed for this backend
    c = LLM[stage]
    key = os.environ.get(c["api_key_env"])
    if not key:
        raise RuntimeError(f"{tag}: ${c['api_key_env']} not set (put it in .env)")
    t0 = time.time()
    resp = OpenAI(base_url=c["base_url"], api_key=key, timeout=c["timeout_s"] * scale, max_retries=1).chat.completions.create(
        model=c["model"], messages=[{"role": "user", "content": prompt}], max_tokens=c["max_output_tokens"],
        **({"reasoning_effort": c["effort"]} if c["effort"] else {}))
    (run_dir / f"raw-{tag}.json").write_text(resp.model_dump_json(indent=1))
    u = resp.usage
    RUN["calls"].append({"tag": tag, "backend": "openai", "models": [resp.model], "wall_s": round(time.time() - t0, 1),
                         "cost_usd": round(getattr(u, "cost", None) or 0, 4),  # only some providers report cost
                         "in_tok": u.prompt_tokens if u else None, "out_tok": u.completion_tokens if u else None,
                         "finish": resp.choices[0].finish_reason})
    return resp.choices[0].message.content or ""


def complete(stage, prompt, tag, run_dir, scale=1):
    """Tool-less completion for the group/write stages via the configured backend."""
    if LLM[stage]["backend"] == "openai":
        return openai_call(stage, prompt, tag, run_dir, scale)
    args, timeout = tool_less_args(stage, scale)
    return claude(args, prompt, timeout, tag, run_dir)[0]


# ---------- ledger (stories.jsonl) ----------
def load_ledger(day):
    """Latest line per key from the 7 days before `day`."""
    lo, latest = (day - timedelta(days=7)).isoformat(), {}
    if LEDGER.exists():
        for ln in LEDGER.read_text().splitlines():
            r = json.loads(ln)
            if lo <= r["date"] < day.isoformat() and r["date"] >= latest.get(r["story"], {}).get("date", ""):
                latest[r["story"]] = r
    return latest


def check_keys(stories, ledger):
    """Warn-only story-key self-check."""
    seen, old = set(), list(ledger)
    for s in stories:
        k = s.get("key") or ""
        if not KEY.match(k):
            warn(f"bad story key {k!r}")
        if k in seen:
            warn(f"duplicate story key {k}")
        seen.add(k)
        for p in s.get("parents") or []:
            if p not in ledger:
                warn(f"{k}: unknown parent {p}")
        if k not in ledger:
            near = difflib.get_close_matches(k, old, n=1, cutoff=0.8)
            if near:
                warn(f"new key {k} ~ old {near[0]} (near-miss)")
    last = max((r["date"] for r in ledger.values()), default="")
    prev = {k for k, r in ledger.items() if r["date"] == last}
    if len(prev) >= 10 and not prev & seen:
        warn("0 carry-overs from the last digest: story memory ignored?")


# ---------- grouping + selection ----------
def item_line(i, items, trim):
    """Grouping lines are trimmed: C/LQ headline only, Trusted snippet ≤100 chars."""
    it = items[i]
    snip = it["snippet"]
    if trim:
        snip = snip[:100].rstrip() if it["tier"] == TRUSTED else ""  # ponytail: cuts input ~25%; untrim if grouping misses context
    reg = f" | {it['region_hint']}" if trim else ""
    return (f"[{i}] {' '.join([it['tier']] + it['flags'])} | {it['outlet']}{reg} | "
            f"{it['published'][5:16].replace('T', ' ')} | {it['title']}" + (f" — {snip}" if snip else ""))


# ---------- prompts ----------
def fill(name, **kw):
    """Prompt template with {{KEY}}s filled from config + kw."""
    feeds = json.loads(SOURCES.read_text())["feeds"]
    kw = {"READER": NEWS.reader, "COUNTERPOINT_DESC": NEWS.counterpoint_desc, "LOW_QUALITY_DESC": NEWS.low_quality_desc, "PROXIMITY": NEWS.proximity, "SIGNIFICANCE": NEWS.significance,
          "KEY_EXAMPLES": NEWS.key_examples, "SCORE_EXAMPLES": " ".join(NEWS.score_examples),
          "CATEGORIES": ", ".join(NEWS.categories), "REGION_ENUM": "|".join(REGIONS),
          "REGION_RULES": "\n".join(f"- **{r.name}** — {r.rule}" for r in NEWS.regions),
          "FRAMING": ", ".join(dict.fromkeys(f["outlet"] for f in feeds if FRAMING in f.get("flags", "").split())),
          "TIME": f"{HOUR:02d}:00 {SETTINGS.schedule.tz_label}", **kw}
    text = (APP / name).read_text()
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def group_prompt(start, end):
    names = [f"{d:%A}" for d in window_days(start, end)]
    note = f"; {end.astimezone(TZ):%A} digest covering {', '.join(names[:-1])} and {names[-1]}" if len(names) > 1 else ""
    return fill("prompt-group.md", WINDOW_START=start.astimezone(TZ).strftime("%a %Y-%m-%d %H:%M"),
                WINDOW_END=end.astimezone(TZ).strftime("%a %Y-%m-%d %H:%M"),
                HOURS=window_hours(start, end), LONG_NOTE=note)


def group(items, start, end, ledger, run_dir):
    prompt = group_prompt(start, end)
    memory = "\n".join(f"{k}|{r['region']}|{r['date']}|{r['headline']}|{r.get('state', '')}"
                       for k, r in sorted(ledger.items(), key=lambda kv: (kv[1]["region"], kv[1]["date"]), reverse=True))
    body = (prompt + "\n## Already sent (last 7 days)\n" + (memory or "(none yet)") + "\n\n## Items\n"
            + "\n".join(item_line(i, items, True) for i in range(len(items))) + "\n")
    res = complete("group", body, "group", run_dir, 2 if is_long_window(start, end) else 1)
    return parse_json(res)["stories"]


def news_ids(ids, items):
    """Ids of news (non-opinion) items."""
    return [i for i in ids if OPINION not in items[i]["flags"]]


def support(ids, items):
    """Source-tier support class (code, not model)."""
    news = news_ids(ids, items)
    trusted = [i for i in news if items[i]["tier"] == TRUSTED]
    if trusted:
        if (all(MIXED in items[i]["flags"] for i in trusted)
                and len({items[i]["group"] for i in news if items[i]["tier"] != LOW_QUALITY}) < 2):
            return "uncorroborated", sorted({items[i]["outlet"] for i in trusted})
        return "trusted", []
    if any(items[i]["tier"] == COUNTERPOINT for i in news):
        return "counterpoint-only", []
    return ("unconfirmed" if news else "opinion-only"), []


def pick_links(ids, items, cap):
    """Free first, Trusted first, one per outlet; never opinion; titles-only outlets last resort."""
    news = news_ids(ids, items)
    usable = sorted([i for i in news if TITLES_ONLY not in items[i]["flags"]] or news,
                    key=lambda i: (PAY_RANK[items[i]["pay"]], TIER_RANK[items[i]["tier"]]))
    out, seen = [], set()
    for i in usable:
        if items[i]["outlet"] not in seen:
            seen.add(items[i]["outlet"])
            out.append(i)
    return out[:cap]


def select(stories, items, threshold):
    """Boost, threshold, support filter, caps → {region: [story]} in Lead/Main/Brief order."""
    kept = []
    for s in stories:
        s["item_ids"] = [i for i in s.get("item_ids", []) if isinstance(i, int) and 0 <= i < len(items)]
        base = s.get("base") if isinstance(s.get("base"), int) else 0
        boost = base >= 5 and any(c in CATEGORIES for c in s.get("categories", []))
        s["base"], s["final"] = base, min(10, base + 1) if boost else base
        s["support"], s["mixed_outlets"] = support(s["item_ids"], items)
        if not s["item_ids"] or s.get("region") not in REGIONS:
            continue
        if s["final"] < max(threshold, SUPPORT_LABEL[s["support"]][2]):
            continue
        s["n_outlets"] = len({items[i]["outlet"] for i in s["item_ids"]})
        kept.append(s)
    out = {}
    for r in REGIONS:
        rs = sorted([s for s in kept if s["region"] == r], key=lambda s: (-s["final"], -s["base"], -s["n_outlets"]))
        lead = [s for s in rs if s["final"] >= 9]
        main = lead[CAPS["lead"]:] + [s for s in rs if 7 <= s["final"] <= 8]
        lead = lead[:CAPS["lead"]]
        brief = main[CAPS["main"]:] + [s for s in rs if s["final"] <= 6]
        main = main[:CAPS["main"]]
        never_drop = [s for s in brief if s["final"] >= 9]
        brief = never_drop + [s for s in brief if s["final"] < 9][:max(0, CAPS["brief"] - len(never_drop))]
        for tier, grp in (("lead", lead), ("main", main), ("brief", brief)):
            for s in grp:
                s["tier"] = tier
                s["link_ids"] = pick_links(s["item_ids"], items, LINK_CAP[tier])
        out[r] = lead + main + brief
    return out


# ---------- writers ----------
def short_date(d):
    return f"{d:%a} {d.day} {d:%b}"


def since_day(s, ledger, day):
    """(last-seen label, state) or (None, None) if new. A week back reads "Thu 1 Oct", not an ambiguous weekday."""
    prev = ledger.get(s.get("key")) or next((ledger[p] for p in s.get("parents") or [] if p in ledger), None)
    if not prev:
        return None, None
    seen = date.fromisoformat(prev["date"])
    return (short_date(seen) if (day - seen).days >= 6 else f"{seen:%A}"), prev.get("state", "")


def writer_input(region, section, items, ledger, day, feedback=""):
    blocks = []
    for n, s in enumerate(section, 1):
        support_note = s["support"] + (f" — attribute to {', '.join(s['mixed_outlets'])}" if s["mixed_outlets"] else "")
        since, state = since_day(s, ledger, day)
        memory_note = f"continuing since {since} — readers know: {state}" if since else "new"
        blocks.append(f"### Story {n} — {s['tier']} ({LIMITS[s['tier']]})\nWorking headline: {s['headline']}\n"
                      f"SUPPORT: {support_note}\nMEMORY: {memory_note}\n"
                      + "\n".join(item_line(i, items, False) for i in s["item_ids"]))
    fix_note = "\n\nYour previous answer failed these checks — fix them:\n- " + "\n- ".join(feedback) if feedback else ""
    return fill("prompt-write.md", REGION=region) + "\n" + "\n\n".join(blocks) + fix_note + "\n"


def as_id(v):
    return int(v) if isinstance(v, str) and v.strip().isdigit() else v


def validate(section, w, items):
    """Writer guards. Coerces digit-string ids in place; returns a list of errors (empty = ok)."""
    errs, n_counterpoints = [], 0
    got = {x.get("n"): x for x in (w.get("stories") or []) if isinstance(x, dict)}
    for n, s in enumerate(section, 1):
        x = got.get(n)
        if not x or not str(x.get("headline") or "").strip():
            errs.append(f"story {n}: missing or no headline")
            continue
        ids = set(s["item_ids"])
        x["counterpoint_ids"] = [as_id(v) for v in x.get("counterpoint_ids") or []]
        x["analysis"] = [dict(a, id=as_id(a.get("id"))) for a in x.get("analysis") or [] if isinstance(a, dict)]
        bad = [v for v in x["counterpoint_ids"] + [a["id"] for a in x["analysis"]] if not isinstance(v, int) or v not in ids]
        if bad:
            errs.append(f"story {n}: ids {bad} are not integer ids of this story's items")
        if x.get("counterpoint"):
            n_counterpoints += 1
            if not any(v in ids and items[v]["tier"] == COUNTERPOINT for v in x["counterpoint_ids"] if isinstance(v, int)):
                errs.append(f"story {n}: counterpoint must cite a C item of this story")
        if any(a["id"] in ids and OPINION not in items[a["id"]]["flags"] for a in x["analysis"] if isinstance(a["id"], int)):
            errs.append(f"story {n}: analysis ids must be OP items")
        if any(OPINION in items[i]["flags"] for i in s["item_ids"]) and not x["analysis"]:
            errs.append(f"story {n}: has OP items but no analysis entry")
        if s["tier"] == "brief" and len(str(x["headline"]).split()) > 18:
            errs.append(f"story {n}: brief headline over 18 words")
        if s["tier"] != "brief" and not (x.get("summary") or x.get("bullets")):
            errs.append(f"story {n}: {s['tier']} needs a summary")
    if n_counterpoints > math.ceil(len(section) / 3):  # prompt: at most one story in three
        errs.append(f"{n_counterpoints} counterpoints for {len(section)} stories — framing differences only, most stories null")
    return errs


def write_section(region, section, items, ledger, day, run_dir):
    """Region writer + guards; retries once with feedback, then None (section fallback)."""
    feedback = []
    for attempt in (1, 2):
        try:
            res = complete("write", writer_input(region, section, items, ledger, day, feedback),
                           f"write-{region}-{attempt}", run_dir)
            w = parse_json(res)
            feedback = validate(section, w, items)
        except Exception as e:  # noqa: BLE001
            feedback = [f"{type(e).__name__}: {e}"]
        if not feedback:
            return {x["n"]: x for x in w["stories"]}
        warn(f"{region} writer attempt {attempt} failed guards: {'; '.join(feedback)[:400]}")
    return None


# ---------- Google News link resolution ----------
def gn_decode(gn_url):
    """GN article id → publisher URL via Google's undocumented batchexecute RPC."""
    # ponytail: GN RSS carries no publisher URL (<description> links back to GN, <source url> is the homepage;
    # checked 2026-10-09), hence this decode. If Google changes it, links fall back to GN redirects.
    art = urllib.parse.urlparse(gn_url).path.rsplit("/", 1)[-1]
    op = opener()
    page = op.open(urllib.request.Request(f"https://news.google.com/rss/articles/{art}", headers={"User-Agent": UA}),
                   timeout=20).read().decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
    inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                                        None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        art, int(ts), sg], separators=(",", ":"))
    data = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))})
    text = op.open(urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=data.encode(),
                                          headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}),
                   timeout=20).read().decode("utf-8", "replace")
    for row in json.loads(text.split("\n", 1)[1]):
        if row[:2] == ["wrb.fr", "Fbv4je"] and row[2]:
            url = json.loads(row[2])[1]
            if url.startswith("http"):
                return url
    raise ValueError("no URL in response")


def decode_gn_item(item):
    """Swap the item's GN redirect for the publisher URL in place; returns a failure note or None."""
    try:
        item["url"] = gn_decode(item["url"])
    except Exception as e:  # noqa: BLE001
        return f"{item['outlet']}: {type(e).__name__}"
    return None


def resolve_links(ids, items):
    """Replace GN redirect URLs for the given (rendered) items; failures keep the GN link."""
    gn = [items[i] for i in sorted(set(ids)) if items[i]["gn"]]
    with ThreadPoolExecutor(max_workers=12) as pool:
        fails = [f for f in pool.map(decode_gn_item, gn) if f]
    if fails:
        warn(f"GN link decode failed {len(fails)}/{len(gn)}: {', '.join(fails[:5])}")


# ---------- render (inline styles, since Gmail strips <style>; one element per line) ----------
INK, MUTED = "#1c1e21", "#5f6670"
PLUM, GREY, AMBER = "#6d4c8d", "#8a9099", "#a15c07"
SERIF = "font-family:Charter,'Iowan Old Style',Georgia,serif;"
SANS = "font-family:Helvetica,Arial,sans-serif;"
S_LEAD = "margin:0 0 28px;padding:2px 0 2px 14px;border-left:4px solid {c};font-size:17px;line-height:1.6;"
S_H1 = "margin:0 0 6px;font-size:22px;line-height:1.25;font-weight:700;"
S_H2 = "margin:0 0 4px;font-size:18px;line-height:1.3;font-weight:700;"
S_META = f"margin-top:6px;{SANS}font-size:13px;color:{MUTED};"
S_CP = f"margin:10px 0 0;padding:8px 12px;background:#f3eff8;{SANS}font-size:14px;line-height:1.5;color:#4a3763;"
S_SMALL = f"{SANS}font-size:13px;color:{MUTED};"
S_TAG = SANS + "font-size:12px;font-weight:700;color:{c};"
TIER_LINK_COLOR = {COUNTERPOINT: PLUM, LOW_QUALITY: GREY}
# support class → (tag before the headline, tag colour, min final score to keep); {outlets} = mixed_outlets
SUPPORT_LABEL = {
    "trusted": ("", None, 0),
    "uncorroborated": ("Reported by {outlets}", MUTED, 0),
    "counterpoint-only": ("Counterpoint outlets only", PLUM, 0),
    "unconfirmed": ("Unconfirmed", AMBER, 8),
    "opinion-only": ("", None, 11),  # never kept
}


def esc(t):
    return _esc(t or "", quote=False)


def link_html(item, color=None):
    style = f' style="color:{color};"' if color else ""
    return f'<a href="{_esc(item["url"])}"{style}>{esc(item["outlet"])}</a>'


def story_links(s, items):
    return ", ".join(("⚠&nbsp;" if items[i]["tier"] == LOW_QUALITY else "") + link_html(items[i], TIER_LINK_COLOR.get(items[i]["tier"]))
                     + (" (paywall)" if items[i]["pay"] == "hard" else "") for i in s["link_ids"])


def analysis_html(s, items):
    return "\n".join(f'<div style="{S_META}">Analysis: {link_html(items[x["id"]], MUTED)} {esc((x.get("gist") or "")[:1].lower() + (x.get("gist") or "")[1:])}</div>'
                     for x in s.get("analysis") or [])


def story_label(s):
    tag, color, _ = SUPPORT_LABEL[s["support"]]
    tags = [(f"Since {s['since']}", MUTED)] if s.get("since") else []
    if tag:
        tags.append((tag.format(outlets=", ".join(s["mixed_outlets"])), color))
    return "".join(f'<span style="{S_TAG.format(c=c)}">{esc(t)}.</span> ' for t, c in tags)


def counterpoint_html(s):
    return f'<p style="{S_CP}"><b>Counterpoint.</b> {esc(s["counterpoint"])}</p>' if s.get("counterpoint") else ""


def story_html(s, color, items):
    if s["tier"] == "brief":
        extra = "\n".join(x for x in [counterpoint_html(s), analysis_html(s, items)] if x)
        return (f'<div style="margin:0 0 14px;">{story_label(s)}{esc(s["headline"])} <span style="{S_SMALL}">{story_links(s, items)}</span>'
                + (f"\n{extra}" if extra else "") + "</div>")
    bullets = "".join(f"<li>{esc(b)}</li>" for b in s.get("bullets") or [])
    bullets = f'\n<ul style="margin:8px 0 0 20px;padding:0;">{bullets}</ul>' if bullets else ""
    lead = s["tier"] == "lead"
    return "\n".join(x for x in [
        f'<div style="{S_LEAD.format(c=color)}">' if lead else '<div style="margin:0 0 26px;">',
        f'<p style="{S_H1 if lead else S_H2}">{story_label(s)}{esc(s["headline"])}</p>',
        f'<div>{esc(s.get("summary"))}</div>{bullets}', counterpoint_html(s), f'<div style="{S_META}">{story_links(s, items)}</div>',
        analysis_html(s, items), "</div>"] if x)


def section_html(name, stories, items, hours):
    c, n = RCOL[name], len(stories or [])
    count = f"{n} stor{'y' if n == 1 else 'ies'}" if n else ""
    head = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:36px 0 20px;">'
            f'<tr><td style="background:{c};padding:10px 16px;{SERIF}font-size:24px;font-weight:700;color:#ffffff;">{name}</td>'
            f'<td align="right" style="background:{c};padding:10px 16px;{SANS}font-size:13px;color:#ffffff;">{count}</td></tr></table>')
    if stories is None:
        return f'{head}\n<p style="margin:0;color:{MUTED};font-style:italic;">Couldn\'t build this section today.</p>'
    if not stories:
        return f'{head}\n<p style="margin:0;color:{MUTED};font-style:italic;">Nothing major in the last {hours}h.</p>'
    out = [head] + [story_html(s, c, items) for s in stories if s["tier"] != "brief"]
    briefs = [story_html(s, c, items) for s in stories if s["tier"] == "brief"]
    if briefs:
        out += [f'<p style="margin:30px 0 12px;padding-top:12px;border-top:2px solid {c};{SANS}font-size:15px;font-weight:700;color:{c};">In brief</p>',
                '<div style="line-height:1.45;">', *briefs, "</div>"]
    return "\n".join(out)


def render(sections, items, slot, start, skipped):
    local, st = slot.astimezone(TZ), start.astimezone(TZ)
    day = f"{local:%A} {local.day} {local:%B}"
    ampm = f"{HOUR % 12 or 12}{'am' if HOUR < 12 else 'pm'}"
    hours = window_hours(start, slot)
    allst = [s for r in REGIONS for s in sections[r] or []]
    words = sum(len((x or "").split()) for s in allst for x in [s["headline"], s.get("summary"), s.get("counterpoint"), *(s.get("bullets") or [])])
    contents = ", ".join(f'<b style="color:{RCOL[r]};">{r}</b> {len(sections[r] or [])}' for r in REGIONS)
    note = f"<br>{skipped} earlier day{'s' if skipped != 1 else ''} skipped (the Mac was off or asleep)." if skipped else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Morning digest, {day}</title></head>
<body style="margin:0;padding:0;background:#ffffff;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
<tr><td align="center" style="padding:24px 16px 32px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">
<tr><td style="text-align:left;{SERIF}font-size:16px;line-height:1.55;color:{INK};">
<p style="margin:0;{SANS}font-size:14px;font-weight:700;color:{MUTED};">Morning digest</p>
<p style="margin:2px 0 8px;font-size:32px;line-height:1.15;font-weight:700;">{day}</p>
<p style="margin:0;{SANS}font-size:14px;line-height:1.5;color:{MUTED};">News from {ampm} {st:%A} to {ampm} {local:%A}, {SETTINGS.schedule.tz_label}.<br>{len(allst)} stories, about {max(1, round(words / 238))} minutes: {contents}.{note}</p>
{chr(10).join(section_html(r, sections[r], items, hours) for r in REGIONS)}
<p style="margin:36px 0 0;padding-top:12px;border-top:1px solid #e3e5e8;{SANS}font-size:12px;line-height:1.6;color:{MUTED};">
Blue links are trusted outlets. <span style="color:{PLUM};">Plum</span> links and Counterpoint notes come from {NEWS.counterpoint_desc}.
⚠ marks a low-quality source. Free links come first; "(paywall)" marks a hard paywall.
Summaries are machine-written from feed headlines and snippets.{" Shadow mode." if SHADOW else ""}</p>
</td></tr></table>
</td></tr></table>
</body></html>
"""


# ---------- send ----------
SMTP_HOST, SMTP_PASSWORD_ENV = "smtp.gmail.com", "GMAIL_APP_PASSWORD"


class SendUncertain(RuntimeError):
    """The connection broke after the message was handed over: it may have gone out, so never retry it."""


def allowed_to():
    to = [ADMIN] if SHADOW else RECIPIENTS
    assert not SHADOW or to == [ADMIN]
    return to


def smtp_password():
    """$GMAIL_APP_PASSWORD, else the login Keychain's internet password for smtp.gmail.com / ADMIN."""
    if pw := os.environ.get(SMTP_PASSWORD_ENV):
        return pw
    r = subprocess.run(["security", "find-internet-password", "-s", SMTP_HOST, "-a", ADMIN, "-w"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"no Gmail app password: set ${SMTP_PASSWORD_ENV} or add a Keychain internet password "
                           f"for server {SMTP_HOST}, account {ADMIN}")
    return r.stdout.strip()


def send(subject, body, to, html=False):
    """Gmail SMTP as ADMIN. Returns the Message-ID. Raises SendUncertain if it may have sent, else RuntimeError (safe to retry)."""
    password = smtp_password()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = ADMIN, ", ".join(to), subject
    msg["Message-ID"] = make_msgid(domain="news-digest")
    msg.set_content(body, subtype="html" if html else "plain")
    try:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=60, context=ssl.create_default_context())
        smtp.login(ADMIN, password)
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(f"SMTP connect/login: {e}") from e
    try:
        with smtp:
            smtp.send_message(msg)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
        raise RuntimeError(f"SMTP rejected: {e}") from e  # explicit refusal: nothing sent
    except (OSError, smtplib.SMTPException) as e:
        raise SendUncertain(f"SMTP: {e}") from e
    return msg["Message-ID"]


def notify(msg):
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title \"News digest\""],
                   capture_output=True, timeout=30)


# ---------- run ----------
def write_state(slot, **extra):
    STATE.write_text(json.dumps({"last_sent": slot.isoformat(), **extra}) + "\n")


def run_slot(slot, skipped, no_send):
    t0, start, end = time.time(), *window(slot)
    day = slot.astimezone(TZ).date()
    run_dir = LOGS / day.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    slot_log = {"slot": slot.isoformat(), "window_h": window_hours(start, end), "skipped": skipped}
    RUN["slots"].append(slot_log)

    items, slot_log["feeds"] = fetch_all(start, end)
    slot_log["items"], slot_log["fetch_s"] = len(items), round(time.time() - t0, 1)
    (run_dir / "items.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
    if not items:
        raise RuntimeError("0 items fetched (network down?)")
    if len(items) < 100:  # ponytail: fixed floor (1.2–1.5k is normal); upgrade: compare to recent runs' counts
        warn(f"{day}: only {len(items)} items fetched")

    ledger = load_ledger(day)
    t1 = time.time()
    stories = group(items, start, end, ledger, run_dir)
    slot_log["group_s"], slot_log["grouped"] = round(time.time() - t1, 1), len(stories)
    sections = select(stories, items, NEWS.threshold_long if is_long_window(start, end) else NEWS.threshold)
    check_keys([s for r in REGIONS for s in sections[r]], ledger)

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {r: pool.submit(write_section, r, sections[r], items, ledger, day, run_dir) for r in REGIONS if sections[r]}
        drafts = {r: f.result() for r, f in futs.items()}
    slot_log["write_s"] = round(time.time() - t1, 1)
    written = {}
    for r in REGIONS:
        if r in drafts and drafts[r] is None:
            written[r] = None
            warn(f"{day} {r}: section fallback (writer failed twice)")
            continue
        written[r] = []
        for n, s in enumerate(sections[r], 1):
            w = drafts[r][n]
            written[r].append(dict(s, **{k: w.get(k) for k in ("headline", "short", "summary", "bullets", "counterpoint",
                                                               "counterpoint_ids", "analysis", "state")},
                                   since=since_day(s, ledger, day)[0]))
    rendered = [s for r in REGIONS for s in written[r] or []]
    t1 = time.time()
    resolve_links([i for s in rendered for i in s["link_ids"] + [a["id"] for a in s.get("analysis") or []]], items)
    slot_log["resolve_s"] = round(time.time() - t1, 1)

    html = render(written, items, slot, start, skipped)
    path = run_dir / "digest.html"
    path.write_text(html)
    (run_dir / "digest.json").write_text(json.dumps(written, indent=1, ensure_ascii=False))
    slot_log["html_bytes"] = len(html.encode())
    if slot_log["html_bytes"] > 100_000:
        warn(f"{day}: HTML {slot_log['html_bytes']} bytes (Gmail clips >102KB)")
    tops = [(written[r][0].get("short") or " ".join(written[r][0]["headline"].split()[:6])) for r in REGIONS if written[r]]
    subject = ("[SHADOW] " if SHADOW else "") + " · ".join([short_date(slot.astimezone(TZ))] + tops)
    slot_log["subject"] = subject
    if no_send:
        slot_log["sent"] = False
        print(f"--no-send: {path} ({slot_log['html_bytes']} bytes) subject: {subject}")
        return

    t1 = time.time()
    try:
        slot_log["message_id"] = send(subject, html, allowed_to(), html=True)
    except SendUncertain:
        # may have arrived: mark the slot done so no later run resends it
        write_state(slot, message_id=None, uncertain=True)
        raise
    slot_log["send_s"] = round(time.time() - t1, 1)
    # confirmed send → ledger, archive, state
    with LEDGER.open("a") as f:
        for s in rendered:
            f.write(json.dumps({"date": day.isoformat(), "region": s["region"], "story": s.get("key"),
                                **({"parents": s["parents"]} if s.get("parents") else {}),
                                "headline": s["headline"], "state": s.get("state"), "tier": s["tier"],
                                "base": s["base"], "final": s["final"], "reason": s.get("reason"),
                                "links": [items[i]["url"] for i in s["link_ids"]]}, ensure_ascii=False) + "\n")
    SENT.mkdir(exist_ok=True)
    (SENT / f"{day}.html").write_text(html)
    write_state(slot, message_id=slot_log["message_id"])
    slot_log["total_s"] = round(time.time() - t0, 1)
    print(f"sent {day}: {slot_log['message_id']} ({slot_log['html_bytes']} bytes, {slot_log['total_s']}s)")


def run_due(no_send, final_attempt):
    last = json.loads(STATE.read_text())["last_sent"] if STATE.exists() else None
    slots, skipped = due_slots(datetime.now(TZ), datetime.fromisoformat(last) if last else None)
    if not slots:
        RUN["note"] = f"nothing due (last sent {last})"
        print(RUN["note"])
        return
    if skipped:
        warn(f"{skipped} earlier day(s) skipped (catch-up cap {CATCHUP})")
    wait_network()
    for n, slot in enumerate(slots):
        try:
            run_slot(slot, skipped if n == 0 else 0, no_send)
        except SendUncertain:
            raise
        except Exception as e:  # noqa: BLE001
            if not final_attempt or slot == slots[-1]:
                raise
            # a catch-up day that fails twice must not block newer days
            warn(f"{slot.date()}: catch-up day skipped after failing twice ({type(e).__name__}: {e})")
            write_state(slot, message_id=None, skipped=True)


def text_email(subject, lines, tag):
    body = "\n".join(lines) + "\n"
    (LOGS / f"{tag}.txt").write_text(body)
    return send(subject, body, [ADMIN])  # ops mail: admin only, even when live


def main():
    LOGS.mkdir(exist_ok=True)
    lock = open(LOGS / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another run holds the lock; exiting")
        return
    no_send = "--no-send" in sys.argv
    t0, stamp = time.time(), datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    RUN["started"] = datetime.now(TZ).isoformat()
    try:
        for attempt in (1,) if no_send else (1, 2):
            try:
                run_due(no_send, attempt == 2 or no_send)
                break
            except Exception as e:  # noqa: BLE001
                RUN["errors"].append(traceback.format_exc())
                print(traceback.format_exc(), file=sys.stderr)
                if no_send:
                    raise
                if attempt == 1 and not isinstance(e, SendUncertain):
                    warn(f"run failed ({type(e).__name__}: {e}); retrying in 15 min")
                    time.sleep(900)
                    continue
                notify(f"Digest failed: {e}"[:200])
                head = ("The SMTP connection broke mid-send: the digest may or may not have arrived. "
                        "The day is marked done so it won't be resent." if isinstance(e, SendUncertain)
                        else f"Run {stamp} failed twice.")
                try:
                    text_email("[news-digest] FAILED", [head, "", RUN["errors"][-1]], f"failure-{stamp}")
                except Exception as e2:  # noqa: BLE001
                    RUN["errors"].append(f"failure email: {e2}")
        # ponytail: weekly (Fri) and monthly (1st) summaries deferred; build them from logs/run-*.json.
        sent = [s for s in RUN["slots"] if s.get("message_id")]
        if RUN["warnings"] and sent and not no_send:
            try:
                text_email(f"[news-digest] health {date.today():%a %-d %b}: {len(RUN['warnings'])} warning(s)",
                           RUN["warnings"] + ["", f"cost ${sum(c.get('cost_usd', 0) for c in RUN['calls']):.2f}, "
                                              f"{round(time.time() - t0)}s, sent: {', '.join(s['message_id'] for s in sent)}"],
                           f"health-{stamp}")
            except Exception as e:  # noqa: BLE001
                RUN["errors"].append(f"health email: {e}")
    finally:
        RUN["wall_s"] = round(time.time() - t0, 1)
        RUN["cost_usd"] = round(sum(c.get("cost_usd", 0) for c in RUN["calls"]), 4)
        (LOGS / f"run-{stamp}.json").write_text(json.dumps(RUN, indent=1, ensure_ascii=False))
        print(f"log: logs/run-{stamp}.json cost ${RUN['cost_usd']} wall {RUN['wall_s']}s")


def install():
    """Write the LaunchAgent from config and (re)load it. launchd fires in the Mac's local time."""
    label, uid = "ai.newsdigest.daily", os.getuid()
    plist = Path.home() / f"Library/LaunchAgents/{label}.plist"
    LOGS.mkdir(exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": label, "ProgramArguments": [sys.executable, str(APP / "digest.py")], "WorkingDirectory": str(APP),
        "StartCalendarInterval": [{"Weekday": (d + 1) % 7, "Hour": HOUR, "Minute": 0} for d in sorted(SEND_DAYS)],
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": os.environ["PATH"], "HOME": str(Path.home()),
                                 "LANG": os.environ.get("LANG", "en_US.UTF-8")},
        "StandardOutPath": str(LOGS / "launchd.out"), "StandardErrorPath": str(LOGS / "launchd.err")}))
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    for _ in range(5):  # bootstrap can fail briefly right after bootout
        if subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)]).returncode == 0:
            return print(f"installed and loaded {plist}")
        time.sleep(1)
    sys.exit(f"launchctl bootstrap failed for {plist}")


if __name__ == "__main__":
    install() if "--install" in sys.argv else main()
