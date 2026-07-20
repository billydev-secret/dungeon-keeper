"""Announcements loop (_process_due) branch behavior, over a real schema."""

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.services import announcements_service as svc

NOW = 1_000_000.0
GUILD = 9001
CHAN = 4242


class _Msg:
    id = 777


class _Chan:
    def __init__(self, cid):
        self.id = cid
        self.sends = []
        self.raise_on_send = None

    async def send(self, **kwargs):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sends.append(kwargs)
        return _Msg()


class _Bot:
    def __init__(self, channels):
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        if cid in self._channels:
            return self._channels[cid]
        raise RuntimeError("not found")

    def get_guild(self, gid):
        return None  # accent falls back to DEFAULT_ACCENT; no branding I/O


def _make_bot(*, has_channel=True):
    channels = {CHAN: _Chan(CHAN)} if has_channel else {}
    return _Bot(channels)


def _forbidden():
    class _Resp:
        status = 403
        reason = "Forbidden"

    return discord.Forbidden(_Resp(), "Missing Permissions")


def _insert(db_path, **over):
    cols = dict(
        guild_id=GUILD, channel_id=CHAN, title="Big news", body="Details",
        image_url=None, accent_hex=None, plain_text=None, mention_kind="none",
        mention_role_id=None, post_date="2030-01-01", post_time_min=1080,
        post_at=NOW, status="scheduled", created_by=2001, created_at=NOW - 60,
        updated_at=NOW - 60,
    )
    cols.update(over)
    with open_db(db_path) as conn:
        ann_id = svc.create_announcement(conn, **cols)
        return svc.get_announcement(conn, ann_id, GUILD)


def _row(db_path, ann_id):
    with open_db(db_path) as conn:
        return svc.get_announcement(conn, ann_id, GUILD)


# ── happy path ────────────────────────────────────────────────────────────────

async def test_due_row_sends_and_marks_sent(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path, mention_kind="role", mention_role_id=555,
                  plain_text="Heads up")

    await svc._process_due(bot, sync_db_path, row, NOW)

    sends = bot._channels[CHAN].sends
    assert len(sends) == 1
    assert sends[0]["content"] == "<@&555> Heads up"
    assert sends[0]["embed"].title == "Big news"
    assert [r.id for r in sends[0]["allowed_mentions"].roles] == [555]
    after = _row(sync_db_path, row["id"])
    assert after["status"] == "sent"
    assert after["sent_message_id"] == 777
    assert after["sent_channel_id"] == CHAN
    assert after["sent_at"] == NOW


async def test_post_now_armed_row_fires(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path, post_date=None, post_time_min=None, post_at=NOW)

    await svc._process_due(bot, sync_db_path, row, NOW)

    assert len(bot._channels[CHAN].sends) == 1
    assert _row(sync_db_path, row["id"])["status"] == "sent"


async def test_slightly_late_row_still_fires(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path, post_at=NOW - 1800)  # 30 min late

    await svc._process_due(bot, sync_db_path, row, NOW)

    assert len(bot._channels[CHAN].sends) == 1
    assert _row(sync_db_path, row["id"])["status"] == "sent"


# ── late / error branches ────────────────────────────────────────────────────

async def test_past_window_marks_missed_without_sending(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path, post_at=NOW - svc.MAX_LATE_SECONDS - 1)

    await svc._process_due(bot, sync_db_path, row, NOW)

    assert bot._channels[CHAN].sends == []
    after = _row(sync_db_path, row["id"])
    assert after["status"] == "error"
    assert after["error"] == svc.MISSED_ERROR
    assert after["sent_at"] is None


async def test_unreachable_channel_errors(sync_db_path):
    bot = _make_bot(has_channel=False)
    row = _insert(sync_db_path)

    await svc._process_due(bot, sync_db_path, row, NOW)

    after = _row(sync_db_path, row["id"])
    assert after["status"] == "error"
    assert "unreachable" in after["error"].lower()


async def test_send_forbidden_errors_and_never_retries(sync_db_path):
    bot = _make_bot()
    bot._channels[CHAN].raise_on_send = _forbidden()
    row = _insert(sync_db_path)

    await svc._process_due(bot, sync_db_path, row, NOW)

    after = _row(sync_db_path, row["id"])
    assert after["status"] == "error"
    assert "send failed" in after["error"].lower()

    # A second pass must not send: the row is no longer claimable.
    bot._channels[CHAN].raise_on_send = None
    await svc._process_due(bot, sync_db_path, after, NOW)
    assert bot._channels[CHAN].sends == []
    assert _row(sync_db_path, row["id"])["status"] == "error"


# ── role buttons ─────────────────────────────────────────────────────────────

def _add_buttons(db_path, ann_id, buttons):
    with open_db(db_path) as conn:
        svc.replace_buttons(conn, ann_id, buttons)


async def test_row_without_buttons_sends_no_view(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path)

    await svc._process_due(bot, sync_db_path, row, NOW)

    assert bot._channels[CHAN].sends[0]["view"] is None


async def test_buttons_ride_along_on_the_post(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path)
    _add_buttons(sync_db_path, row["id"], [
        {"role_id": 555, "label": "Movie Night", "emoji": "🎬", "style": "primary"},
        {"role_id": 556, "label": "Book Club", "emoji": "", "style": "success"},
    ])

    await svc._process_due(bot, sync_db_path, _row(sync_db_path, row["id"]), NOW)

    view = bot._channels[CHAN].sends[0]["view"]
    assert [c.custom_id for c in view.children] == ["ann_role:555", "ann_role:556"]
    assert [c.item.label for c in view.children] == ["Movie Night", "Book Club"]


async def test_buttons_keep_their_configured_order(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path)
    _add_buttons(sync_db_path, row["id"], [
        {"role_id": 900 + i, "label": f"R{i}"} for i in range(svc.MAX_BUTTONS)
    ])

    await svc._process_due(bot, sync_db_path, _row(sync_db_path, row["id"]), NOW)

    view = bot._channels[CHAN].sends[0]["view"]
    assert [c.item.label for c in view.children] == [f"R{i}" for i in range(svc.MAX_BUTTONS)]


async def test_replace_buttons_swaps_the_whole_set(sync_db_path):
    row = _insert(sync_db_path)
    _add_buttons(sync_db_path, row["id"], [{"role_id": 555, "label": "Old"}])
    _add_buttons(sync_db_path, row["id"], [{"role_id": 556, "label": "New"}])

    with open_db(sync_db_path) as conn:
        rows = svc.list_buttons(conn, row["id"])
    assert [(r["role_id"], r["label"]) for r in rows] == [(556, "New")]


async def test_deleting_an_announcement_drops_its_buttons(sync_db_path):
    row = _insert(sync_db_path)
    _add_buttons(sync_db_path, row["id"], [{"role_id": 555, "label": "Movie Night"}])

    with open_db(sync_db_path) as conn:
        svc.delete_announcement(conn, row["id"], GUILD)
        assert svc.list_buttons(conn, row["id"]) == []


async def test_delete_from_another_guild_leaves_buttons_alone(sync_db_path):
    # The guild guard is on the parent row; the child delete must not run when
    # the parent delete matched nothing.
    row = _insert(sync_db_path)
    _add_buttons(sync_db_path, row["id"], [{"role_id": 555, "label": "Movie Night"}])

    with open_db(sync_db_path) as conn:
        svc.delete_announcement(conn, row["id"], GUILD + 1)
        assert len(svc.list_buttons(conn, row["id"])) == 1


async def test_clone_copies_the_buttons(sync_db_path):
    row = _insert(sync_db_path)
    _add_buttons(sync_db_path, row["id"], [
        {"role_id": 555, "label": "Movie Night", "emoji": "🎬", "style": "success"},
    ])

    with open_db(sync_db_path) as conn:
        new_id = svc.clone_announcement(conn, row["id"], GUILD, 2001, NOW)
        copied = svc.list_buttons(conn, new_id)

    assert [(r["role_id"], r["label"], r["emoji"], r["style"]) for r in copied] == [
        (555, "Movie Night", "🎬", "success")
    ]


# ── claim semantics ──────────────────────────────────────────────────────────

async def test_claim_is_atomic(sync_db_path):
    row = _insert(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert svc.claim(conn, row["id"], NOW) is True
        assert svc.claim(conn, row["id"], NOW) is False  # already sent


async def test_already_sent_row_is_skipped(sync_db_path):
    bot = _make_bot()
    row = _insert(sync_db_path, status="sent")

    await svc._process_due(bot, sync_db_path, row, NOW)

    assert bot._channels[CHAN].sends == []


# ── fetch_due filtering ──────────────────────────────────────────────────────

async def test_fetch_due_only_returns_due_scheduled_rows(sync_db_path):
    _insert(sync_db_path, title="draft", status="draft", post_at=None,
            post_date=None, post_time_min=None)
    _insert(sync_db_path, title="errored", status="error")
    _insert(sync_db_path, title="future", post_at=NOW + 3600)
    due = _insert(sync_db_path, title="due")

    with open_db(sync_db_path) as conn:
        rows = svc.fetch_due(conn, NOW)

    assert [r["id"] for r in rows] == [due["id"]]
