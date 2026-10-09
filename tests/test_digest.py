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
from digest import LOGS, TZ, allowed_to, due_slots, guard_send, utc, validate, window  # noqa: E402


def T(*a):
    return datetime(*a, tzinfo=TZ)


def D(d):
    return date(2026, 10, d)


def days(r):
    return [s.date() for s in r[0]]


def hours(slot):
    start, end = window(slot)
    return (end - start).total_seconds() / 3600


class TestDueSlots:
    def test_normal_thursday(self):
        assert days(due_slots(T(2026, 10, 8, 8), T(2026, 10, 7, 7))) == [D(8)]

    def test_already_sent_is_noop(self):
        assert due_slots(T(2026, 10, 8, 9), T(2026, 10, 8, 7)) == ([], 0)

    def test_before_seven(self):
        assert due_slots(T(2026, 10, 8, 6, 59), T(2026, 10, 7, 7)) == ([], 0)

    def test_saturday_wake_sends_friday_only(self):
        assert days(due_slots(T(2026, 10, 10, 12), T(2026, 10, 8, 7))) == [D(9)]

    def test_week_asleep_caps_catchup(self):
        r = due_slots(T(2026, 10, 11, 9), T(2026, 10, 4, 7))
        assert days(r) == [D(8), D(9), D(11)]
        assert r[1] == 3

    @pytest.mark.parametrize("now,expected", [((2026, 10, 8, 18), 8), ((2026, 10, 10, 18), 9)])
    def test_first_run_sends_latest_slot(self, now, expected):
        assert days(due_slots(T(*now), None)) == [D(expected)]


    def test_saturday_never_due(self):
        assert due_slots(T(2026, 10, 10, 23), T(2026, 10, 9, 7)) == ([], 0)

    def test_custom_send_days(self, monkeypatch):
        monkeypatch.setattr(digest, "SEND_DAYS", {0, 2, 4})  # Mon, Wed, Fri
        assert days(due_slots(T(2026, 10, 12, 8), T(2026, 10, 7, 7))) == [D(9), D(12)]


class TestWindow:
    def test_weekday_24h(self):
        assert hours(T(2026, 10, 8, 7)) == 24

    def test_sunday_48h(self):
        assert hours(T(2026, 10, 11, 7)) == 48
        assert window(T(2026, 10, 11, 7))[0] == utc(T(2026, 10, 9, 7))

    def test_sunday_across_dst_end(self):
        assert hours(T(2026, 10, 25, 7)) == 49

    def test_starts_at_previous_send_day(self, monkeypatch):
        monkeypatch.setattr(digest, "SEND_DAYS", {0, 2, 4})
        assert hours(T(2026, 10, 12, 7)) == 72

    def test_window_days_flags_long_windows(self):
        assert len(digest.window_days(*window(T(2026, 10, 8, 7)))) == 1
        assert len(digest.window_days(*window(T(2026, 10, 25, 7)))) == 2


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
    def test_example_matches_defaults(self):
        assert digest.LLM == digest.LLM_DEFAULTS

    def test_args_include_effort_only_when_set(self):
        args, timeout = digest.llm("group")
        assert args == ["--model", "sonnet", "--max-budget-usd", "0.9", "--effort", "low"] and timeout == 600
        args, _ = digest.llm("write")
        assert "--effort" not in args and args[1] == "haiku"

    def test_long_window_scales_budget_and_timeout(self):
        args, timeout = digest.llm("group", 2)
        assert args[3] == "1.8" and timeout == 1200

    def test_partial_override_keeps_defaults(self, monkeypatch):
        monkeypatch.setitem(digest.LLM, "write", {**digest.LLM_DEFAULTS["write"], "model": "sonnet"})
        args, timeout = digest.llm("write")
        assert args[1] == "sonnet" and timeout == 300

    def test_no_tools_strips_tool_access(self):
        args, _ = digest.no_tools("write")
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
        s = self.load(monkeypatch, tmp_path, (r"(?m)^\[llm\.write\][^\[]*", '[llm.write]\nmodel = "x"\n\n'))
        assert s.llm.write.model == "x" and s.llm.write.budget_usd == 0.15 and s.llm.write.timeout_s == 300
        assert s.llm.group.model == "sonnet"

    def test_missing_llm_section_uses_defaults(self, monkeypatch, tmp_path):
        s = self.load(monkeypatch, tmp_path, (r"(?m)^\[llm\.\w+\][^\[]*", ""))
        assert s.llm.model_dump() == digest.LLM_DEFAULTS

    def test_send_backend_openai_rejected(self, monkeypatch, tmp_path):
        with pytest.raises(ValidationError, match="claude-cli"):
            self.load(monkeypatch, tmp_path, (r'(\[llm\.send\].*\n)backend = "claude-cli"', '\\1backend = "openai"'))

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
        cfg = {**digest.LLM_DEFAULTS["write"], "backend": "openai", "model": "google/gemini-2.5-flash",
               "base_url": "https://openrouter.ai/api/v1", "api_key_env": "TEST_KEY", **kw}
        monkeypatch.setitem(digest.LLM, "write", cfg)
        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    def test_default_backend_is_claude_cli(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(digest, "claude", lambda args, stdin, timeout, tag, wd: (seen.update(args=args) or "out", {}))
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
        cfg.write_text((APP / "config.example.toml").read_text().replace(
            '[llm.send]                         # copies the rendered email into the Gmail connector\nbackend = "claude-cli"',
            '[llm.send]\nbackend = "openai"'))
        env = dict(os.environ, NEWS_DIGEST_CONFIG=str(cfg))
        r = subprocess.run([sys.executable, "-c", "import digest"], cwd=APP, env=env, capture_output=True, text=True)
        assert r.returncode == 1 and "llm.send" in r.stderr and "claude-cli" in r.stderr
        assert "Traceback" not in r.stderr


class TestPrompts:
    @pytest.fixture(autouse=True)
    def example_sources(self, monkeypatch):
        monkeypatch.setattr(digest, "SOURCES", APP / "sources.example.json")

    def test_group_prompt_fully_filled(self):
        p = digest.group_prompt(*window(T(2026, 10, 11, 7)))
        assert not re.findall(r"\{\{\w+\}\}", p)
        assert "48h ending 07:00" in p and "Sunday digest covering Friday and Saturday" in p
        assert '"region": "' + "|".join(digest.REGIONS) + '"' in p

    def test_weekday_group_prompt_has_no_long_note(self):
        assert "covering" not in digest.group_prompt(*window(T(2026, 10, 8, 7)))

    def test_writer_prompt_fully_filled(self):
        p = digest.writer_input("World", [], [], {})
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

    def test_rejects_counterpoint_inflation(self):
        many = [{"tier": "brief", "item_ids": [0, 1]} for _ in range(6)]
        cps = {"stories": [{"n": n, "headline": "h", "counterpoint": "c", "counterpoint_ids": [1]} for n in range(1, 7)]}
        assert any("counterpoints" in e for e in validate(many, cps, ITEMS))


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

    def test_hook_uses_current_interpreter(self):
        assert sys.executable in digest.GUARD and "/usr/bin/python3" not in digest.GUARD
