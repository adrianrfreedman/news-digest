import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

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
