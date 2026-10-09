from datetime import date, datetime

import pytest

import digest
from digest import LOGS, TZ, allowed_to, due_slots, guard_send, utc, validate, window


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


class TestWindow:
    def test_weekday_24h(self):
        assert hours(T(2026, 10, 8, 7)) == 24

    def test_sunday_48h(self):
        assert hours(T(2026, 10, 11, 7)) == 48
        assert window(T(2026, 10, 11, 7))[0] == utc(T(2026, 10, 9, 7))

    def test_sunday_across_dst_end(self):
        assert hours(T(2026, 10, 25, 7)) == 49


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
        assert allowed_to() == [digest.ADMIN]
