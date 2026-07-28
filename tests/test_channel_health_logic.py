"""Tests for the configured-channel health rules.

The anchor case is the real incident these rules exist for: the bios channel
was fine by every check the dashboard had — id resolved, text channel, bot held
Administrator — and yet no member could see it, because ``@everyone`` was
denied View Channel and the one role granting it back had been deleted.
"""

from __future__ import annotations

import pytest

from bot_modules.services.channel_health_logic import (
    ChannelSnapshot,
    diagnose_all,
    diagnose_category,
    diagnose_channel,
    group_by_channel,
    grouped_issue_to_dict,
    issue_to_dict,
)


def test_registry_supplies_the_channels_to_check():
    """The sweep is driven by settings_registry, so a feature that registers a
    channel setting is covered automatically — including the one this exists
    for. A duplicate key would check the same channel twice."""
    from bot_modules.services.channel_health import configured_channel_settings

    entries = configured_channel_settings()
    keys = [key for (key, _label, _panel) in entries]

    assert "bios_channel_id" in keys
    assert len(keys) == len(set(keys))
    assert all(label and panel for (_key, label, panel) in entries)


def _snap(**over) -> ChannelSnapshot:
    """A healthy channel; override one thing per test to break it."""
    base = dict(
        key="bios_channel_id",
        label="Bios channel",
        panel="Config → Bios",
        channel_id=1526280601051594772,
        exists=True,
        channel_name="👋│about-us",
        accepts_messages=True,
        human_viewers=145,
        total_humans=172,
        bot_can_view=True,
        bot_can_send=True,
        bot_can_embed=True,
    )
    base.update(over)
    return ChannelSnapshot(**base)  # type: ignore[arg-type]


# ── The incident ──────────────────────────────────────────────────────


def test_channel_nobody_can_view_is_reported():
    """The bios outage: healthy in every other respect, invisible to all."""
    issues = diagnose_channel(_snap(human_viewers=0))
    assert [i.code for i in issues] == ["nobody_can_view"]
    assert issues[0].severity == "error"
    assert "172 members" in issues[0].message


def test_bot_administrator_does_not_mask_an_invisible_channel():
    """The bot could post the whole time — that must not read as healthy."""
    issues = diagnose_channel(
        _snap(human_viewers=0, bot_can_view=True, bot_can_send=True, bot_can_embed=True)
    )
    assert [i.code for i in issues] == ["nobody_can_view"]


# ── No false positives ────────────────────────────────────────────────


def test_healthy_channel_has_no_issues():
    assert diagnose_channel(_snap()) == []


@pytest.mark.parametrize(
    "viewers,total",
    [
        pytest.param(5, 172, id="staff-only-mod-log"),
        pytest.param(12, 172, id="staff-only-welcome-procedure"),
        pytest.param(38, 172, id="gated-welcome-chat"),
        pytest.param(1, 172, id="single-viewer-still-reaches-someone"),
    ],
)
def test_deliberately_restricted_channels_are_not_flagged(viewers, total):
    """Measured against a live server: a share threshold would flag all of
    these, and every one of them is locked down on purpose."""
    assert diagnose_channel(_snap(human_viewers=viewers, total_humans=total)) == []


def test_unknown_membership_does_not_report_invisibility():
    """An unpopulated member cache is not evidence the channel is hidden."""
    assert diagnose_channel(_snap(human_viewers=0, total_humans=0)) == []


# ── The other rules ───────────────────────────────────────────────────


def test_missing_channel_short_circuits():
    """A dangling id makes every other question unanswerable — say one thing."""
    issues = diagnose_channel(
        _snap(exists=False, human_viewers=0, bot_can_send=False, accepts_messages=False)
    )
    assert [i.code for i in issues] == ["missing"]


def test_wrong_channel_type_is_reported():
    issues = diagnose_channel(_snap(accepts_messages=False))
    assert [i.code for i in issues] == ["wrong_type"]


@pytest.mark.parametrize(
    "over,named",
    [
        pytest.param({"bot_can_view": False}, "View Channel", id="view"),
        pytest.param({"bot_can_send": False}, "Send Messages", id="send"),
        pytest.param({"bot_can_embed": False}, "Embed Links", id="embed"),
    ],
)
def test_bot_missing_each_posting_permission(over, named):
    issues = diagnose_channel(_snap(**over))
    assert [i.code for i in issues] == ["bot_cannot_post"]
    assert named in issues[0].message


def test_all_missing_permissions_are_named_together():
    issues = diagnose_channel(
        _snap(bot_can_view=False, bot_can_send=False, bot_can_embed=False)
    )
    msg = issues[0].message
    assert "View Channel" in msg and "Send Messages" in msg and "Embed Links" in msg


def test_one_channel_can_have_several_problems():
    issues = diagnose_channel(_snap(bot_can_send=False, human_viewers=0))
    assert [i.code for i in issues] == ["bot_cannot_post", "nobody_can_view"]


# ── Ordering and serialisation ────────────────────────────────────────


def test_diagnose_all_sorts_worst_first():
    issues = diagnose_all(
        [
            _snap(key="a_channel_id", human_viewers=0),
            _snap(key="b_channel_id", exists=False),
            _snap(key="c_channel_id", bot_can_send=False),
            _snap(key="d_channel_id"),  # healthy — contributes nothing
        ]
    )
    assert [i.code for i in issues] == ["missing", "bot_cannot_post", "nobody_can_view"]


def test_diagnose_all_on_healthy_channels_is_empty():
    assert diagnose_all([_snap(key="a_channel_id"), _snap(key="b_channel_id")]) == []


def test_issue_serialises_snowflake_as_string():
    """Ids over 2^53 must not leave the API as bare numbers."""
    payload = issue_to_dict(diagnose_channel(_snap(human_viewers=0))[0])
    assert payload["channel_id"] == "1526280601051594772"
    assert isinstance(payload["channel_id"], str)


# ── The measurement the rules run on ──────────────────────────────────


class _FakeMember:
    def __init__(self, member_id: int, *, bot: bool = False, sees: bool = True) -> None:
        self.id = member_id
        self.bot = bot
        self.sees = sees


class _FakeChannel:
    """Only needs ``permissions_for`` — that's all the counter touches."""

    def permissions_for(self, member: _FakeMember):
        return type("P", (), {"view_channel": member.sees})()


class _FakeGuild:
    def __init__(self, members) -> None:
        self.members = members


def _count(members):
    from bot_modules.services.channel_health import _human_viewers

    return _human_viewers(_FakeGuild(members), _FakeChannel())  # type: ignore[arg-type]


def test_viewer_count_excludes_bots():
    """Bots always keep their own access — counting them would hide the fault."""
    viewers, total = _count(
        [
            _FakeMember(1, sees=True),
            _FakeMember(2, bot=True, sees=True),
            _FakeMember(3, sees=False),
        ]
    )
    assert viewers > 0
    assert total == 2  # the bot is not one of "your members"


def test_viewer_count_stops_at_one_but_still_totals_everyone():
    """Only zero-vs-some is ever asked, so the scan short-circuits — but the
    total has to stay accurate, since the warning quotes it."""
    members = [_FakeMember(i, sees=True) for i in range(50)]
    viewers, total = _count(members)
    assert viewers == 1
    assert total == 50


def test_viewer_count_reports_zero_against_a_real_total():
    """The bios outage, as measured: members exist, none of them can see it."""
    members = [_FakeMember(i, sees=False) for i in range(5)]
    members.append(_FakeMember(99, bot=True, sees=True))  # the bot still sees it
    assert _count(members) == (0, 5)


def test_viewer_count_on_an_empty_member_list_is_unknown_not_broken():
    assert _count([]) == (0, 0)
    assert _count([_FakeMember(1, bot=True)]) == (0, 0)


# ── Categories play by different rules ────────────────────────────────


def _cat(**over) -> ChannelSnapshot:
    base = dict(
        key="bios_wizard_category_id",
        label="Wizard category",
        panel="Config → Bios",
        channel_id=1526281046478557184,
        exists=True,
        channel_name="Bio-writer",
        accepts_messages=False,
        human_viewers=0,
        total_humans=172,
        bot_can_view=True,
        bot_can_send=True,
        bot_can_embed=True,
        is_category=True,
        bot_can_manage_channels=True,
    )
    base.update(over)
    return ChannelSnapshot(**base)  # type: ignore[arg-type]


def test_healthy_category_has_no_issues():
    """Takes no messages and is invisible to members — both correct here, and
    both would be faults under the channel rules."""
    assert diagnose_category(_cat()) == []
    # Proof the distinction is real: the channel rules reject the same input.
    assert [i.code for i in diagnose_channel(_cat())] == [
        "wrong_type",
        "nobody_can_view",
    ]


def test_category_missing_is_reported():
    assert [i.code for i in diagnose_category(_cat(exists=False))] == ["missing"]


def test_plain_channel_saved_as_a_category_is_reported():
    issues = diagnose_category(_cat(is_category=False))
    assert [i.code for i in issues] == ["wrong_type"]
    assert "needs to be a category" in issues[0].message


def test_category_the_bot_cannot_create_channels_in_is_reported():
    issues = diagnose_category(_cat(bot_can_manage_channels=False))
    assert [i.code for i in issues] == ["bot_cannot_post"]
    assert "Manage Channels" in issues[0].message


# ── Grouping ──────────────────────────────────────────────────────────


def test_settings_sharing_a_broken_channel_group_into_one_row():
    """Live check on the real server raised the bios channel twice, once per
    setting pointing at it. One channel, one fault, one fix."""
    issues = diagnose_all(
        [
            _snap(key="bios_channel_id", label="Bios channel", human_viewers=0),
            _snap(key="bios_trigger_channel_id", label="Trigger channel", human_viewers=0),
        ]
    )
    assert len(issues) == 2

    groups = group_by_channel(issues)
    assert len(groups) == 1
    assert groups[0].code == "nobody_can_view"
    assert [s[0] for s in groups[0].settings] == [
        "bios_channel_id",
        "bios_trigger_channel_id",
    ]


def test_grouping_keeps_different_channels_apart():
    issues = diagnose_all(
        [
            _snap(key="a_channel_id", channel_id=111, human_viewers=0),
            _snap(key="b_channel_id", channel_id=222, human_viewers=0),
        ]
    )
    assert len(group_by_channel(issues)) == 2


def test_grouping_keeps_different_faults_on_one_channel_apart():
    """Two problems with the same channel are two things to fix, not one."""
    groups = group_by_channel(
        diagnose_channel(_snap(bot_can_send=False, human_viewers=0))
    )
    assert [g.code for g in groups] == ["bot_cannot_post", "nobody_can_view"]


def test_grouped_issue_serialises_settings_and_snowflake():
    groups = group_by_channel(
        diagnose_all(
            [
                _snap(key="bios_channel_id", label="Bios channel", human_viewers=0),
                _snap(key="bios_trigger_channel_id", label="Trigger", human_viewers=0),
            ]
        )
    )
    payload = grouped_issue_to_dict(groups[0])
    assert payload["channel_id"] == "1526280601051594772"
    assert isinstance(payload["channel_id"], str)
    assert [s["label"] for s in payload["settings"]] == ["Bios channel", "Trigger"]
