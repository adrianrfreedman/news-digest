import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

APP = Path(__file__).resolve().parent.parent
os.environ["NEWS_DIGEST_CONFIG"] = str(APP / "config.example.toml")  # never the local config.toml

import digest  # noqa: E402
from digest import LOGS, TZ, UTC, allowed_to, due_slots, guard_send, validate, window  # noqa: E402


def at(*a):
    return datetime(*a, tzinfo=TZ)


def oct_day(d):
    return date(2026, 10, d)


def days(r):
    return [s.date() for s in r[0]]


def hours(slot):
    start, end = window(slot)
    return (end - start).total_seconds() / 3600


class TestDueSlots:
    def test_normal_thursday(self):
        assert days(due_slots(at(2026, 10, 8, 8), at(2026, 10, 7, 7))) == [oct_day(8)]

    def test_already_sent_is_noop(self):
        assert due_slots(at(2026, 10, 8, 9), at(2026, 10, 8, 7)) == ([], 0)

    def test_before_seven(self):
        assert due_slots(at(2026, 10, 8, 6, 59), at(2026, 10, 7, 7)) == ([], 0)

    def test_saturday_wake_sends_friday_only(self):
        assert days(due_slots(at(2026, 10, 10, 12), at(2026, 10, 8, 7))) == [oct_day(9)]

    def test_week_asleep_caps_catchup(self):
        r = due_slots(at(2026, 10, 11, 9), at(2026, 10, 4, 7))
        assert days(r) == [oct_day(8), oct_day(9), oct_day(11)]
        assert r[1] == 3

    @pytest.mark.parametrize("now,expected", [((2026, 10, 8, 18), 8), ((2026, 10, 10, 18), 9)])
    def test_first_run_sends_latest_slot(self, now, expected):
        assert days(due_slots(at(*now), None)) == [oct_day(expected)]


    def test_saturday_never_due(self):
        assert due_slots(at(2026, 10, 10, 23), at(2026, 10, 9, 7)) == ([], 0)

    def test_custom_send_days(self, monkeypatch):
        monkeypatch.setattr(digest, "SEND_DAYS", {0, 2, 4})  # Mon, Wed, Fri
        assert days(due_slots(at(2026, 10, 12, 8), at(2026, 10, 7, 7))) == [oct_day(9), oct_day(12)]


class TestWindow:
    def test_weekday_24h(self):
        assert hours(at(2026, 10, 8, 7)) == 24

    def test_sunday_48h(self):
        assert hours(at(2026, 10, 11, 7)) == 48
        assert window(at(2026, 10, 11, 7))[0] == at(2026, 10, 9, 7).astimezone(UTC)

    def test_sunday_across_dst_end(self):
        assert hours(at(2026, 10, 25, 7)) == 49

    def test_starts_at_previous_send_day(self, monkeypatch):
        monkeypatch.setattr(digest, "SEND_DAYS", {0, 2, 4})
        assert hours(at(2026, 10, 12, 7)) == 72

    def test_window_days_flags_long_windows(self):
        assert len(digest.window_days(*window(at(2026, 10, 8, 7)))) == 1
        assert len(digest.window_days(*window(at(2026, 10, 25, 7)))) == 2


class TestConfig:
    def test_example_config_loads(self):
        assert digest.ADMIN == "you@example.com" and digest.SHADOW
        assert digest.SEND_DAYS == {0, 1, 2, 3, 4, 6} and digest.HOUR == 7
        assert digest.REGIONS == list(digest.RCOL)

    def test_missing_config_exits_with_message(self, tmp_path):
        env = dict(os.environ, NEWS_DIGEST_CONFIG=str(tmp_path / "nope.toml"))
        r = subprocess.run([sys.executable, "-c", "import digest"], cwd=APP, env=env, capture_output=True, text=True)
        assert r.returncode == 1 and "config.example.toml" in r.stderr


class TestLlmConfig:
    def test_args_include_effort_only_when_set(self):
        args, timeout = digest.cli_args("group")
        assert args == ["--model", "sonnet", "--max-budget-usd", "0.9", "--effort", "low"] and timeout == 600
        args, _ = digest.cli_args("write")
        assert "--effort" not in args and args[1] == "haiku"

    def test_long_window_scales_budget_and_timeout(self):
        args, timeout = digest.cli_args("group", 2)
        assert args[3] == "1.8" and timeout == 1200

    def test_partial_override_keeps_defaults(self, monkeypatch):
        monkeypatch.setitem(digest.LLM, "write", {**digest.Llm().model_dump()["write"], "model": "sonnet"})
        args, timeout = digest.cli_args("write")
        assert args[1] == "sonnet" and timeout == 300

    def test_no_tools_strips_tool_access(self):
        args, _ = digest.tool_less_args("write")
        assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args


class TestSettings:
    def load(self, monkeypatch, tmp_path, *subs, path=APP / "config.example.toml"):
        """Settings from `path` with (regex, replacement) substitutions applied."""
        text = path.read_text()
        for pat, rep in subs:
            text, n = re.subn(pat, rep, text)
            assert n, pat
        cfg = tmp_path / "c.toml"
        cfg.write_text(text)
        monkeypatch.setattr(digest, "CONFIG", cfg)
        return digest.Settings()

    def test_example_validates(self, monkeypatch, tmp_path):
        assert self.load(monkeypatch, tmp_path).schedule.hour == 7

    @pytest.mark.parametrize("pat,rep,match", [
        (r"hour = 7", "hour = 7\nhuor = 7", "huor"),
        (r"hour = 7", "hour = 24", "schedule.hour"),
        (r"Europe/London", "Mars/Olympus", "unknown timezone"),
        (r'send_days = \[', 'send_days = ["Sabbath", ', "send_days"),
        (r"shadow = true", "shadow = false", "admin must be in recipients"),
        (r'"#1f4e8c"', '"blue"', "color"),
        (r'name = "World"', 'name = "UK"', "unique"),
        (r"catchup_days = 3", "catchup_days = 0", "catchup_days"),
    ])
    def test_rejects_bad_config(self, monkeypatch, tmp_path, pat, rep, match):
        subs = [(r"(?m)^recipients = .*", 'recipients = ["x@example.com"]'), (pat, rep)]
        with pytest.raises(ValidationError, match=match):
            self.load(monkeypatch, tmp_path, *subs)

    def test_partial_stage_override_keeps_stage_defaults(self, monkeypatch, tmp_path):
        s = self.load(monkeypatch, tmp_path, (r"\Z", '\n[llm.write]\nmodel = "x"\n'))
        assert s.llm.write.model == "x" and s.llm.write.budget_usd == 0.15 and s.llm.write.timeout_s == 300
        assert s.llm.group.model == "sonnet"

    def test_missing_llm_section_uses_defaults(self, monkeypatch, tmp_path):
        s = self.load(monkeypatch, tmp_path)
        assert s.llm.group.model == "sonnet" and s.llm.send.budget_usd == 0.30

    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEWS_DIGEST_SCHEDULE__HOUR", "8")
        assert self.load(monkeypatch, tmp_path).schedule.hour == 8

    def test_local_config_validates(self, monkeypatch, tmp_path):
        if not (APP / "config.toml").exists():
            pytest.skip("no local config.toml")
        self.load(monkeypatch, tmp_path, path=APP / "config.toml")


class FakeOpenAI:
    """Stands in for openai.OpenAI; records the request."""
    last = {}

    def __init__(self, **kw):
        FakeOpenAI.last = {"client": kw}
        self.chat = self
        self.completions = self

    def create(self, **kw):
        FakeOpenAI.last["request"] = kw
        from openai.types.chat import ChatCompletion
        return ChatCompletion.model_validate({
            "id": "x", "object": "chat.completion", "created": 0, "model": kw["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok": 1}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})


class TestBackends:
    def openai_stage(self, monkeypatch, **kw):
        cfg = {**digest.Llm().model_dump()["write"], "backend": "openai", "model": "google/gemini-2.5-flash",
               "base_url": "https://openrouter.ai/api/v1", "api_key_env": "TEST_KEY", **kw}
        monkeypatch.setitem(digest.LLM, "write", cfg)
        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    def test_default_backend_is_claude_cli(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(digest, "claude", lambda args, stdin, timeout, tag, run_dir, env=None: (seen.update(args=args) or "out", {}))
        assert digest.complete("write", "p", "t", tmp_path) == "out"
        assert seen["args"][1] == "haiku"

    def test_openai_backend(self, monkeypatch, tmp_path):
        self.openai_stage(monkeypatch, effort="low")
        monkeypatch.setenv("TEST_KEY", "k")
        assert digest.complete("write", "prompt", "t", tmp_path) == '{"ok": 1}'
        req, client = FakeOpenAI.last["request"], FakeOpenAI.last["client"]
        assert req["model"] == "google/gemini-2.5-flash" and req["reasoning_effort"] == "low"
        assert req["messages"] == [{"role": "user", "content": "prompt"}]
        assert client["base_url"] == "https://openrouter.ai/api/v1" and client["api_key"] == "k"
        assert (tmp_path / "raw-t.json").exists()

    def test_openai_backend_without_effort_omits_it(self, monkeypatch, tmp_path):
        self.openai_stage(monkeypatch)
        monkeypatch.setenv("TEST_KEY", "k")
        digest.complete("write", "p", "t", tmp_path)
        assert "reasoning_effort" not in FakeOpenAI.last["request"]

    def test_missing_api_key_fails_clearly(self, monkeypatch, tmp_path):
        self.openai_stage(monkeypatch)
        monkeypatch.delenv("TEST_KEY", raising=False)
        with pytest.raises(RuntimeError, match="TEST_KEY"):
            digest.complete("write", "p", "t", tmp_path)

    def test_send_must_use_claude_cli(self, tmp_path):
        cfg = tmp_path / "c.toml"
        cfg.write_text((APP / "config.example.toml").read_text() + '\n[llm.send]\nbackend = "openai"\n')
        env = dict(os.environ, NEWS_DIGEST_CONFIG=str(cfg))
        r = subprocess.run([sys.executable, "-c", "import digest"], cwd=APP, env=env, capture_output=True, text=True)
        assert r.returncode == 1 and "llm.send" in r.stderr and "claude-cli" in r.stderr
        assert "Traceback" not in r.stderr


class TestPrompts:
    @pytest.fixture(autouse=True)
    def example_sources(self, monkeypatch):
        monkeypatch.setattr(digest, "SOURCES", APP / "sources.example.json")

    def test_group_prompt_fully_filled(self):
        p = digest.group_prompt(*window(at(2026, 10, 11, 7)))
        assert not re.findall(r"\{\{\w+\}\}", p)
        assert "48h ending 07:00" in p and "Sunday digest covering Friday and Saturday" in p
        assert '"region": "' + "|".join(digest.REGIONS) + '"' in p

    def test_weekday_group_prompt_has_no_long_note(self):
        assert "covering" not in digest.group_prompt(*window(at(2026, 10, 8, 7)))

    def test_writer_prompt_fully_filled(self):
        p = digest.writer_input("World", [], [], {}, oct_day(9))
        assert not re.findall(r"\{\{\w+\}\}", p)
        assert "flag `F`: " in p and "**World**" in p


ITEMS = [{"tier": "T", "flags": []}, {"tier": "C", "flags": []}, {"tier": "T", "flags": ["OP"]}]
SECTION = [{"tier": "main", "item_ids": [0, 1, 2]}, {"tier": "brief", "item_ids": [0]}]


def writer_out(**kw):
    s1 = dict({"n": 1, "headline": "H", "summary": "S", "counterpoint": "X frames it as Y",
               "counterpoint_ids": ["1"], "analysis": [{"id": 2, "gist": "g"}]}, **kw)
    return {"stories": [s1, {"n": 2, "headline": "short brief"}]}


class TestValidate:
    def test_valid_output_coerces_digit_strings(self):
        w = writer_out()
        assert validate(SECTION, w, ITEMS) == []
        assert w["stories"][0]["counterpoint_ids"] == [1]

    @pytest.mark.parametrize("kw", [
        {"counterpoint_ids": [9]},                        # invented id
        {"counterpoint_ids": ["x1"]},                     # non-integer id
        {"counterpoint_ids": [0]},                        # counterpoint cites a Trusted item
        {"analysis": []},                                 # opinion item but no Analysis
        {"analysis": [{"id": 0, "gist": "g"}]},           # Analysis on a news item
        {"summary": None},                                # main without summary
    ])
    def test_rejects_rule_breaks(self, kw):
        assert validate(SECTION, writer_out(**kw), ITEMS)

    def test_rejects_missing_story(self):
        assert validate(SECTION, {"stories": writer_out()["stories"][:1]}, ITEMS)

    def test_rejects_long_brief(self):
        w = writer_out()
        w["stories"][1]["headline"] = " ".join(["w"] * 19)
        assert validate(SECTION, w, ITEMS)

    @pytest.mark.parametrize("n,cps,ok", [(2, 1, True), (2, 2, False), (3, 1, True), (3, 2, False),
                                          (4, 2, True), (6, 2, True), (6, 3, False)])
    def test_counterpoint_cap_one_in_three(self, n, cps, ok):
        section = [{"tier": "brief", "item_ids": [0, 1]} for _ in range(n)]
        w = {"stories": [{"n": k, "headline": "h", "counterpoint": "c" if k <= cps else None, "counterpoint_ids": [1]}
                         for k in range(1, n + 1)]}
        assert (not any("counterpoints" in e for e in validate(section, w, ITEMS))) == ok


SEND = "mcp__claude_ai_Gmail__send_message"


class TestGuardSend:
    def test_allows_configured_recipients(self):
        assert guard_send({"tool_name": SEND, "tool_input": {"to": allowed_to(), "subject": "s"}}) is None

    @pytest.mark.parametrize("ti", [
        {"to": ["x@evil.test"]},
        {"to": allowed_to() + ["x@evil.test"]},
        {"to": allowed_to(), "bcc": ["x@evil.test"]},
        {"to": allowed_to(), "cc": ["x@evil.test"]},
        {"to": allowed_to(), "attachments": [{"content": "eA=="}]},
    ])
    def test_blocks_other_recipients_and_fields(self, ti):
        assert guard_send({"tool_name": SEND, "tool_input": ti})

    def test_blocks_other_tools(self):
        assert guard_send({"tool_name": "Bash", "tool_input": {"command": "ls"}})

    def test_read_limited_to_app_dir(self):
        assert guard_send({"tool_name": "Read", "tool_input": {"file_path": str(LOGS / "a.html")}}) is None
        assert guard_send({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}})
        assert guard_send({"tool_name": "Read", "tool_input": {"file_path": str(digest.APP) + "-evil/x"}})

    def test_shadow_mode_is_admin_only(self, monkeypatch):
        monkeypatch.setattr(digest, "SHADOW", True)
        monkeypatch.setattr(digest, "RECIPIENTS", [digest.ADMIN, "reader@example.com"])
        assert allowed_to() == [digest.ADMIN]
        assert guard_send({"tool_name": SEND, "tool_input": {"to": [digest.ADMIN, "reader@example.com"]}})

    def test_live_mode_sends_to_recipients(self, monkeypatch):
        monkeypatch.setattr(digest, "SHADOW", False)
        monkeypatch.setattr(digest, "RECIPIENTS", [digest.ADMIN, "reader@example.com"])
        assert guard_send({"tool_name": SEND, "tool_input": {"to": ["reader@example.com", digest.ADMIN]}}) is None

    def test_live_mode_allows_admin_only_mail(self, monkeypatch):
        monkeypatch.setattr(digest, "SHADOW", False)
        monkeypatch.setattr(digest, "RECIPIENTS", [digest.ADMIN, "reader@example.com"])
        assert guard_send({"tool_name": SEND, "tool_input": {"to": [digest.ADMIN]}}) is None
        assert guard_send({"tool_name": SEND, "tool_input": {"to": ["reader@example.com"]}})

    def hook(self, tmp_path, ev):
        marker = tmp_path / "m"
        env = dict(os.environ, NEWS_DIGEST_SEND_MARKER=str(marker))
        r = subprocess.run([sys.executable, str(APP / "digest.py"), "--guard-send"], input=json.dumps(ev),
                           cwd=APP, env=env, capture_output=True, text=True, check=False)
        return r.returncode, marker.exists()

    def test_hook_marks_allowed_send(self, tmp_path):
        assert self.hook(tmp_path, {"tool_name": SEND, "tool_input": {"to": allowed_to()}}) == (0, True)

    @pytest.mark.parametrize("ev,code", [
        ({"tool_name": SEND, "tool_input": {"to": ["x@evil.test"]}}, 2),
        ({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, 2),
        ({"tool_name": "Read", "tool_input": {"file_path": str(APP / "README.md")}}, 0),
    ])
    def test_hook_leaves_no_marker_otherwise(self, tmp_path, ev, code):
        assert self.hook(tmp_path, ev) == (code, False)


class TestSend:
    def fake(self, monkeypatch, result="", touch=False, fail=False):
        """Stub claude(): optionally touch the marker (guard allowed the send), then return `result` or raise."""
        seen = {}

        def claude(args, stdin, timeout, tag, run_dir, env=None):
            seen.update(args=args, prompt=stdin, env=env)
            if touch:
                Path(env["NEWS_DIGEST_SEND_MARKER"]).touch()
            if fail:
                raise RuntimeError("send: timed out after 300s")
            return result, {}
        monkeypatch.setattr(digest, "claude", claude)
        return seen

    def test_id_is_success(self, monkeypatch, tmp_path):
        seen = self.fake(monkeypatch, "`0123456789abcdef`", touch=True)
        assert digest.send(tmp_path / "x.html", "s", "htmlBody", tmp_path, "send", ["a@b.c"]) == "0123456789abcdef"
        assert 'to=["a@b.c"]' in seen["prompt"] and str(tmp_path / "send.attempted") == seen["env"]["NEWS_DIGEST_SEND_MARKER"]

    @pytest.mark.parametrize("fail", [False, True])
    def test_never_attempted_is_retryable(self, monkeypatch, tmp_path, fail):
        self.fake(monkeypatch, "FAILED: Gmail not connected", fail=fail)
        with pytest.raises(RuntimeError) as e:
            digest.send(tmp_path / "x.html", "s", "htmlBody", tmp_path, "send", ["a@b.c"])
        assert not isinstance(e.value, digest.SendUncertain)

    @pytest.mark.parametrize("fail", [False, True])
    def test_attempted_without_id_is_uncertain(self, monkeypatch, tmp_path, fail):
        self.fake(monkeypatch, "FAILED: timeout", touch=True, fail=fail)
        with pytest.raises(digest.SendUncertain):
            digest.send(tmp_path / "x.html", "s", "htmlBody", tmp_path, "send", ["a@b.c"])

    def test_stale_marker_is_cleared(self, monkeypatch, tmp_path):
        (tmp_path / "send.attempted").touch()
        self.fake(monkeypatch, "FAILED")
        with pytest.raises(RuntimeError) as e:
            digest.send(tmp_path / "x.html", "s", "htmlBody", tmp_path, "send", ["a@b.c"])
        assert not isinstance(e.value, digest.SendUncertain)

    def test_ops_mail_goes_to_admin_only(self, monkeypatch, tmp_path):
        monkeypatch.setattr(digest, "LOGS", tmp_path)
        monkeypatch.setattr(digest, "SHADOW", False)
        monkeypatch.setattr(digest, "RECIPIENTS", [digest.ADMIN, "reader@example.com"])
        seen = self.fake(monkeypatch, "0123456789abcdef")
        digest.text_email("[news-digest] health", ["w"], "health-x")
        assert f"to={json.dumps([digest.ADMIN])}" in seen["prompt"]

    def test_hook_uses_current_interpreter(self):
        assert sys.executable in digest.GUARD and "/usr/bin/python3" not in digest.GUARD


class TestStoryKey:
    @pytest.mark.parametrize("key", ["rail-talks", "uk-iranian-drone-plot-b1-withdrawal"])
    def test_accepts_two_to_six_words(self, key):
        assert digest.KEY.match(key)

    @pytest.mark.parametrize("key", ["rail", "a-b-c-d-e-f-g", "Rail-Talks", "rail_talks"])
    def test_rejects_bad_keys(self, key):
        assert not digest.KEY.match(key)


SINCE_LEDGER = {"a-b": {"date": "2026-10-08", "state": "s"}, "c-d": {"date": "2026-10-02", "state": "t"}}


class TestSinceDay:
    @pytest.mark.parametrize("story,expected", [
        ({"key": "a-b"}, ("Thursday", "s")),
        ({"key": "c-d"}, ("Fri 2 Oct", "t")),                    # 6 days back: a weekday alone is ambiguous
        ({"key": "new-one", "parents": ["a-b"]}, ("Thursday", "s")),
        ({"key": "new-one"}, (None, None)),
    ])
    def test_label(self, story, expected):
        assert digest.since_day(story, SINCE_LEDGER, oct_day(9)) == expected

    def test_five_days_back_keeps_weekday(self):
        assert digest.since_day({"key": "c-d"}, SINCE_LEDGER, oct_day(7))[0] == "Friday"


def rss(*pub_dates):
    items = "".join(f"<item><title>t{n}</title><link>https://x.test/{n}</link><pubDate>{d}</pubDate></item>"
                    for n, d in enumerate(pub_dates))
    return f"<rss><channel>{items}</channel></rss>".encode()


class TestFetch:
    def run(self, monkeypatch, tmp_path, body):
        src = tmp_path / "s.json"
        src.write_text(json.dumps({"gn_locales": {}, "feeds": [
            {"outlet": "X", "tier": "T", "region": "World", "url": "https://x.test/feed"}]}))
        monkeypatch.setattr(digest, "SOURCES", src)
        monkeypatch.setattr(digest, "http_get", lambda url, timeout=30: body)
        monkeypatch.setitem(digest.RUN, "warnings", [])
        items, _ = digest.fetch_all(*window(at(2026, 10, 8, 7)))
        return items, digest.RUN["warnings"]

    def test_empty_feed_warns(self, monkeypatch, tmp_path):
        assert self.run(monkeypatch, tmp_path, rss()) == ([], ["feed empty: X: 0 items"])

    def test_quiet_feed_does_not_warn(self, monkeypatch, tmp_path):
        assert self.run(monkeypatch, tmp_path, rss("Mon, 05 Oct 2026 10:00:00 GMT")) == ([], [])

    def test_error_warns(self, monkeypatch, tmp_path):
        _, warnings = self.run(monkeypatch, tmp_path, b"not xml")
        assert len(warnings) == 1 and warnings[0].startswith("feed error: X: ParseError")


class Stop(Exception):
    pass


def stop(*a):
    raise Stop


class TestItemFloor:
    def run(self, monkeypatch, tmp_path, n):
        monkeypatch.setattr(digest, "LOGS", tmp_path)
        monkeypatch.setattr(digest, "fetch_all", lambda start, end: ([{"title": "x"}] * n, []))
        monkeypatch.setattr(digest, "group", stop)
        monkeypatch.setitem(digest.RUN, "warnings", [])
        monkeypatch.setitem(digest.RUN, "slots", [])
        digest.run_slot(at(2026, 10, 8, 7), 0, True)

    def test_zero_items_aborts(self, monkeypatch, tmp_path):
        with pytest.raises(RuntimeError, match="0 items"):
            self.run(monkeypatch, tmp_path, 0)

    def test_few_items_warns_and_continues(self, monkeypatch, tmp_path):
        with pytest.raises(Stop):
            self.run(monkeypatch, tmp_path, 5)
        assert digest.RUN["warnings"] == ["2026-10-08: only 5 items fetched"]


class TestSelect:
    @pytest.mark.parametrize("items,base,kept", [
        ([{"tier": "T", "flags": []}], 6, True),
        ([{"tier": "LQ", "flags": []}], 7, False),                # unconfirmed needs final ≥ 8
        ([{"tier": "LQ", "flags": []}], 8, True),
        ([{"tier": "T", "flags": ["OP"]}], 10, False),            # opinion-only never kept
    ])
    def test_support_filter(self, items, base, kept):
        items = [dict(it, outlet="X", group="X", pay="") for it in items]
        out = digest.select([{"region": "World", "base": base, "item_ids": [0]}], items, 6)
        assert bool(out["World"]) == kept

    def test_label_names_uncorroborated_outlets(self):
        s = {"support": "uncorroborated", "mixed_outlets": ["A", "B"], "since": "Thursday"}
        assert ">Since Thursday.<" in digest.story_label(s) and ">Reported by A, B.<" in digest.story_label(s)
