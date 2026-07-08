"""Integration tests for /api/games/* endpoints.

Migration 054 seeds a starter 'ffa' bank into ``games_question_bank`` (51
rows, 24 nsfw-tagged), so a freshly-migrated DB is never empty. Tests that
need an empty bank call :func:`_clear_bank` first; count assertions either
clear the seed or scope by game_type.
"""

from __future__ import annotations

import json

from bot_modules.core.db_utils import open_db

BASE = "/api/games"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _seed_question(db_path, game_type="wyr", category="sfw", text="Test?", tags=None):
    """Seed a bank row. ``category`` is translated to the reserved ``nsfw`` tag
    (legacy callers pass 'sfw'/'nsfw'); explicit ``tags`` override it."""
    if tags is None:
        tags = ["nsfw"] if category == "nsfw" else []
    with open_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO games_question_bank (game_type, tags, question_text)"
            " VALUES (?, ?, ?)",
            (game_type, json.dumps(tags), text),
        )
        return cur.lastrowid


def _clear_bank(db_path):
    """Delete the migration-seeded question bank so 'empty' endpoints can be
    exercised (migration 054 seeds a starter 'ffa' bank)."""
    with open_db(db_path) as conn:
        conn.execute("DELETE FROM games_question_bank")


def _seed_history(db_path, game_type="wyr", player_count=3, round_count=5):
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO games_game_history"
            " (game_id, game_type, channel_id, host_id, player_count, round_count, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (f"game-{game_type}", game_type, 111, 222, player_count, round_count),
        )


def _create_template(client, **overrides):
    body = {
        "title": "Test Template",
        "body": "I like to {1} with a {2}.",
        "tier": 1,
        "tags": '["fun"]',
        "status": "active",
        "player_min": 2,
        "player_max": 8,
        "blanks": '[{"id": "1", "pos": "verb"}, {"id": "2", "pos": "noun"}]',
        "notes": "Test notes",
        **overrides,
    }
    return client.post(f"{BASE}/legitlibs/templates", json=body)


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_unauthenticated_request_returns_401(fake_ctx):
    from fastapi.testclient import TestClient
    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"{BASE}/stats")
    assert resp.status_code == 401
    client.close()


# ── Stats ─────────────────────────────────────────────────────────────────────


def test_stats_empty_db(open_client, fake_ctx):
    _clear_bank(fake_ctx.db_path)
    resp = open_client.get(f"{BASE}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_questions"] == 0
    assert data["games_played"] == 0
    assert data["rounds_played"] == 0
    assert data["unique_players"] == 0
    assert data["bank_by_type"] == {}


def test_stats_counts_questions_and_history(open_client, fake_ctx):
    _clear_bank(fake_ctx.db_path)
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "Q1?")
    _seed_question(fake_ctx.db_path, "wyr", "nsfw", "Q2?")
    _seed_history(fake_ctx.db_path, "wyr", player_count=4, round_count=10)

    resp = open_client.get(f"{BASE}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_questions"] == 2
    assert data["games_played"] == 1
    assert data["rounds_played"] == 10
    assert "wyr" in data["bank_by_type"]
    assert data["bank_by_type"]["wyr"]["sfw"] == 1
    assert data["bank_by_type"]["wyr"]["nsfw"] == 1
    assert data["games_by_type"] == {"wyr": 1}


# ── Bank CRUD ─────────────────────────────────────────────────────────────────


def test_bank_create_returns_question_id(open_client):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "wyr", "tags": ["funny"], "question_text": "Fly or swim?"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "question_id" in data
    assert isinstance(data["question_id"], int)


def test_bank_create_tags_roundtrip(open_client, fake_ctx):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "wyr", "tags": ["spicy", "nsfw", "spicy"], "question_text": "Q?"},
    )
    assert resp.status_code == 200
    qid = resp.json()["question_id"]
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT tags FROM games_question_bank WHERE question_id = ?", (qid,)
        ).fetchone()
    assert json.loads(row["tags"]) == ["spicy", "nsfw"]  # deduped, order preserved


def test_bank_create_no_tags_defaults_empty(open_client, fake_ctx):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "wyr", "question_text": "Q?"},
    )
    assert resp.status_code == 200
    qid = resp.json()["question_id"]
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT tags FROM games_question_bank WHERE question_id = ?", (qid,)
        ).fetchone()
    assert json.loads(row["tags"]) == []


def test_bank_create_invalid_game_type(open_client):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "bogus", "tags": [], "question_text": "Q?"},
    )
    assert resp.status_code == 400


# ── Traditional Truth-or-Dare: one-of-four category tag enforcement ───────────


def test_bank_create_traditional_requires_a_category(open_client):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "traditional", "tags": [], "question_text": "Q?"},
    )
    assert resp.status_code == 400


def test_bank_create_traditional_rejects_unknown_category(open_client):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "traditional", "tags": ["spicy"], "question_text": "Q?"},
    )
    assert resp.status_code == 400


def test_bank_create_traditional_rejects_extra_tags(open_client):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "traditional", "tags": ["sfw_truth", "extra"], "question_text": "Q?"},
    )
    assert resp.status_code == 400


def test_bank_create_traditional_accepts_single_category(open_client, fake_ctx):
    resp = open_client.post(
        f"{BASE}/bank",
        json={"game_type": "traditional", "tags": ["nsfw_dare"], "question_text": "Q?"},
    )
    assert resp.status_code == 200
    qid = resp.json()["question_id"]
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT tags FROM games_question_bank WHERE question_id = ?", (qid,)
        ).fetchone()
    assert json.loads(row["tags"]) == ["nsfw_dare"]


def test_bank_bulk_traditional_requires_a_category(open_client):
    resp = open_client.post(
        f"{BASE}/bank/bulk",
        json={"game_type": "traditional", "tags": [], "lines": ["a", "b"]},
    )
    assert resp.status_code == 400


def test_bank_update_traditional_rejects_bad_category(open_client, fake_ctx):
    qid = _seed_question(fake_ctx.db_path, "traditional", tags=["sfw_truth"], text="Q?")
    resp = open_client.put(f"{BASE}/bank/{qid}", json={"tags": ["nope"]})
    assert resp.status_code == 400


def test_bank_list_empty(open_client, fake_ctx):
    _clear_bank(fake_ctx.db_path)
    resp = open_client.get(f"{BASE}/bank")
    assert resp.status_code == 200
    data = resp.json()
    assert data["questions"] == []
    assert data["total"] == 0
    assert data["total_pages"] == 1


def test_bank_list_with_questions(open_client, fake_ctx):
    _clear_bank(fake_ctx.db_path)
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "Q1?")
    _seed_question(fake_ctx.db_path, "nhie", "nsfw", "Q2?")

    resp = open_client.get(f"{BASE}/bank")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["questions"]) == 2


def test_bank_list_filter_by_game_type(open_client, fake_ctx):
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "WYR?")
    _seed_question(fake_ctx.db_path, "nhie", "sfw", "NHIE?")

    resp = open_client.get(f"{BASE}/bank?game_type=wyr")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["questions"][0]["game_type"] == "wyr"


def test_bank_list_filter_by_tag(open_client, fake_ctx):
    # Seeded 'ffa' rows include nsfw-tagged ones — clear so the tag filter
    # result is exactly the row seeded below.
    _clear_bank(fake_ctx.db_path)
    _seed_question(fake_ctx.db_path, "wyr", text="Safe?", tags=["calm"])
    _seed_question(fake_ctx.db_path, "wyr", text="Spicy?", tags=["nsfw"])

    resp = open_client.get(f"{BASE}/bank?tag=nsfw")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["questions"][0]["tags"] == ["nsfw"]
    assert data["questions"][0]["question_text"] == "Spicy?"


def test_bank_tags_endpoint_returns_distinct_tags(open_client, fake_ctx):
    _seed_question(fake_ctx.db_path, "wyr", text="A?", tags=["a", "b"])
    _seed_question(fake_ctx.db_path, "wyr", text="B?", tags=["b", "c"])
    _seed_question(fake_ctx.db_path, "nhie", text="C?", tags=["other"])

    resp = open_client.get(f"{BASE}/bank/tags?game_type=wyr")
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["a", "b", "c"]  # sorted, distinct, only wyr


def test_bank_list_search(open_client, fake_ctx):
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "Would you rather fly?")
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "Would you rather swim?")

    resp = open_client.get(f"{BASE}/bank?search=fly")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert "fly" in data["questions"][0]["question_text"]


def test_bank_update_question_text(open_client, fake_ctx):
    qid = _seed_question(fake_ctx.db_path, "wyr", "sfw", "Old text?")
    resp = open_client.put(
        f"{BASE}/bank/{qid}",
        json={"question_text": "New text?"},
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT question_text FROM games_question_bank WHERE question_id = ?", (qid,)
        ).fetchone()
    assert row["question_text"] == "New text?"


def test_bank_update_not_found(open_client):
    resp = open_client.put(f"{BASE}/bank/99999", json={"question_text": "X?"})
    assert resp.status_code == 404


def test_bank_update_tags(open_client, fake_ctx):
    qid = _seed_question(fake_ctx.db_path, tags=["old"])
    resp = open_client.put(f"{BASE}/bank/{qid}", json={"tags": ["new", "nsfw"]})
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT tags FROM games_question_bank WHERE question_id = ?", (qid,)
        ).fetchone()
    assert json.loads(row["tags"]) == ["new", "nsfw"]


def test_bank_delete_question(open_client, fake_ctx):
    qid = _seed_question(fake_ctx.db_path)
    resp = open_client.delete(f"{BASE}/bank/{qid}")
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM games_question_bank WHERE question_id = ?", (qid,)
        ).fetchone()
    assert row is None


def test_bank_delete_not_found(open_client):
    resp = open_client.delete(f"{BASE}/bank/99999")
    assert resp.status_code == 404


# ── Bulk add ──────────────────────────────────────────────────────────────────


def test_bank_bulk_add_questions(open_client, fake_ctx):
    resp = open_client.post(
        f"{BASE}/bank/bulk",
        json={
            "game_type": "nhie",
            "tags": ["batch"],
            "lines": ["Q1?", "Q2?", "  Q3?  ", "Q4?"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 4

    with open_db(fake_ctx.db_path) as conn:
        rows = conn.execute(
            "SELECT tags FROM games_question_bank WHERE game_type = 'nhie'"
        ).fetchall()
    assert len(rows) == 4
    assert all(json.loads(r["tags"]) == ["batch"] for r in rows)


def test_bank_bulk_blank_lines_stripped(open_client):
    resp = open_client.post(
        f"{BASE}/bank/bulk",
        json={
            "game_type": "wyr",
            "tags": [],
            "lines": ["Real Q?", "   ", ""],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 1


def test_bank_bulk_all_empty_lines_rejected(open_client):
    resp = open_client.post(
        f"{BASE}/bank/bulk",
        json={"game_type": "wyr", "tags": [], "lines": ["  ", ""]},
    )
    assert resp.status_code == 400


def test_bank_bulk_invalid_game_type(open_client):
    resp = open_client.post(
        f"{BASE}/bank/bulk",
        json={"game_type": "unknown", "tags": [], "lines": ["Q?"]},
    )
    assert resp.status_code == 400


# ── Export / Import ───────────────────────────────────────────────────────────


def test_bank_export_all(open_client, fake_ctx):
    _clear_bank(fake_ctx.db_path)
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "Q1?")
    _seed_question(fake_ctx.db_path, "nhie", "nsfw", "Q2?")

    resp = open_client.get(f"{BASE}/bank/export")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert all("game_type" in i and "tags" in i and "question_text" in i for i in items)
    assert all(isinstance(i["tags"], list) for i in items)


def test_bank_export_filtered_by_game_type(open_client, fake_ctx):
    _seed_question(fake_ctx.db_path, "wyr", "sfw", "WYR?")
    _seed_question(fake_ctx.db_path, "nhie", "sfw", "NHIE?")

    resp = open_client.get(f"{BASE}/bank/export?game_type=wyr")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["game_type"] == "wyr"


def test_bank_export_empty_returns_list(open_client, fake_ctx):
    _clear_bank(fake_ctx.db_path)
    resp = open_client.get(f"{BASE}/bank/export")
    assert resp.status_code == 200
    assert resp.json() == []


def test_bank_import_valid_array(open_client, fake_ctx):
    payload = [
        {"game_type": "wyr", "tags": ["air"], "question_text": "Fly or swim?"},
        # No "tags" key → defaults to []. Legacy "category":"nsfw" maps to the nsfw tag.
        {"game_type": "nhie", "category": "nsfw", "question_text": "Never have I?"},
        {"game_type": "wyr", "question_text": "Bare question?"},
    ]
    resp = open_client.post(f"{BASE}/bank/import", json=payload)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 3

    with open_db(fake_ctx.db_path) as conn:
        rows = {
            r["question_text"]: json.loads(r["tags"])
            for r in conn.execute(
                "SELECT question_text, tags FROM games_question_bank"
            ).fetchall()
        }
    assert rows["Fly or swim?"] == ["air"]
    assert rows["Never have I?"] == ["nsfw"]  # legacy category backfilled to tag
    assert rows["Bare question?"] == []  # missing tags defaults to empty


def test_bank_import_not_an_array(open_client):
    resp = open_client.post(f"{BASE}/bank/import", json={"game_type": "wyr"})
    assert resp.status_code == 400


def test_bank_import_invalid_game_type(open_client):
    payload = [{"game_type": "bogus", "tags": [], "question_text": "Q?"}]
    resp = open_client.post(f"{BASE}/bank/import", json=payload)
    assert resp.status_code == 400


def test_bank_import_empty_texts_skipped(open_client):
    payload = [
        {"game_type": "wyr", "tags": [], "question_text": "   "},
        {"game_type": "wyr", "tags": [], "question_text": ""},
    ]
    resp = open_client.post(f"{BASE}/bank/import", json=payload)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 0


# ── Prompts ───────────────────────────────────────────────────────────────────


def test_prompts_get_returns_default_structure(open_client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "web_server.routes.games._PROMPT_CONFIG_PATH",
        tmp_path / "prompt_config.json",
    )
    resp = open_client.get(f"{BASE}/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert "audience" in data
    assert "sfw_tone" in data
    assert "nsfw_tone" in data
    assert "games" in data


def test_prompts_update_global(open_client, monkeypatch, tmp_path):
    cfg_path = tmp_path / "prompt_config.json"
    monkeypatch.setattr("web_server.routes.games._PROMPT_CONFIG_PATH", cfg_path)

    resp = open_client.put(
        f"{BASE}/prompts/global",
        json={"audience": "adults", "sfw_tone": "playful", "nsfw_tone": "bold"},
    )
    assert resp.status_code == 200

    saved = json.loads(cfg_path.read_text())
    assert saved["audience"] == "adults"
    assert saved["sfw_tone"] == "playful"
    assert saved["nsfw_tone"] == "bold"


def test_prompts_update_game_entry(open_client, monkeypatch, tmp_path):
    cfg_path = tmp_path / "prompt_config.json"
    monkeypatch.setattr("web_server.routes.games._PROMPT_CONFIG_PATH", cfg_path)

    resp = open_client.put(
        f"{BASE}/prompts/game/wyr",
        json={"descriptor": "Would You Rather", "max_tokens": 150},
    )
    assert resp.status_code == 200

    saved = json.loads(cfg_path.read_text())
    assert saved["games"]["wyr"]["descriptor"] == "Would You Rather"
    assert saved["games"]["wyr"]["max_tokens"] == 150


def test_prompts_update_invalid_game_type(open_client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "web_server.routes.games._PROMPT_CONFIG_PATH",
        tmp_path / "prompt_config.json",
    )
    resp = open_client.put(
        f"{BASE}/prompts/game/bogus",
        json={"descriptor": "Bad"},
    )
    assert resp.status_code == 400


# ── History ───────────────────────────────────────────────────────────────────


def test_history_empty(open_client):
    resp = open_client.get(f"{BASE}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == []
    assert data["total"] == 0
    assert data["total_pages"] == 1


def test_history_with_data(open_client, fake_ctx):
    _seed_history(fake_ctx.db_path, "wyr", player_count=5, round_count=8)

    resp = open_client.get(f"{BASE}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    row = data["rows"][0]
    assert row["game_type"] == "wyr"
    assert row["player_count"] == 5
    assert row["round_count"] == 8


def test_history_filter_by_game_type(open_client, fake_ctx):
    _seed_history(fake_ctx.db_path, "wyr")
    _seed_history(fake_ctx.db_path, "nhie")

    resp = open_client.get(f"{BASE}/history?game_type=nhie")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["rows"][0]["game_type"] == "nhie"


def test_history_pagination(open_client, fake_ctx):
    for i in range(3):
        with open_db(fake_ctx.db_path) as conn:
            conn.execute(
                "INSERT INTO games_game_history"
                " (game_id, game_type, channel_id, host_id, started_at)"
                " VALUES (?, 'wyr', 111, 222, datetime('now'))",
                (f"game-{i}",),
            )

    resp = open_client.get(f"{BASE}/history?per_page=2&page=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rows"]) == 2
    assert data["total"] == 3
    assert data["total_pages"] == 2


# ── LegitLibs templates ───────────────────────────────────────────────────────


def test_ll_templates_list_empty(open_client):
    resp = open_client.get(f"{BASE}/legitlibs/templates")
    assert resp.status_code == 200
    assert resp.json()["templates"] == []


def test_ll_templates_create(open_client):
    resp = _create_template(open_client)
    assert resp.status_code == 200
    data = resp.json()
    assert "template_id" in data
    assert isinstance(data["template_id"], int)


def test_ll_templates_list_after_create(open_client):
    _create_template(open_client)
    _create_template(open_client, title="Second")

    resp = open_client.get(f"{BASE}/legitlibs/templates")
    assert resp.status_code == 200
    assert len(resp.json()["templates"]) == 2


def test_ll_templates_get_by_id(open_client):
    create_resp = _create_template(open_client, title="My Template", tier=2)
    tid = create_resp.json()["template_id"]

    resp = open_client.get(f"{BASE}/legitlibs/templates/{tid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "My Template"
    assert data["tier"] == 2


def test_ll_templates_get_not_found(open_client):
    resp = open_client.get(f"{BASE}/legitlibs/templates/99999")
    assert resp.status_code == 404


def test_ll_templates_update(open_client):
    create_resp = _create_template(open_client)
    tid = create_resp.json()["template_id"]

    resp = open_client.put(
        f"{BASE}/legitlibs/templates/{tid}",
        json={"title": "Updated Title", "tier": 3},
    )
    assert resp.status_code == 200

    get_resp = open_client.get(f"{BASE}/legitlibs/templates/{tid}")
    assert get_resp.json()["title"] == "Updated Title"
    assert get_resp.json()["tier"] == 3


def test_ll_templates_update_not_found(open_client):
    resp = open_client.put(
        f"{BASE}/legitlibs/templates/99999",
        json={"title": "X"},
    )
    assert resp.status_code == 404


def test_ll_templates_delete(open_client, fake_ctx):
    create_resp = _create_template(open_client)
    tid = create_resp.json()["template_id"]

    resp = open_client.delete(f"{BASE}/legitlibs/templates/{tid}")
    assert resp.status_code == 200

    get_resp = open_client.get(f"{BASE}/legitlibs/templates/{tid}")
    assert get_resp.status_code == 404


def test_ll_templates_delete_not_found(open_client):
    resp = open_client.delete(f"{BASE}/legitlibs/templates/99999")
    assert resp.status_code == 404


def test_ll_templates_filter_by_tier(open_client):
    _create_template(open_client, tier=1)
    _create_template(open_client, tier=2)
    _create_template(open_client, tier=2, title="Another tier 2")

    resp = open_client.get(f"{BASE}/legitlibs/templates?tier=2")
    assert resp.status_code == 200
    templates = resp.json()["templates"]
    assert len(templates) == 2
    assert all(t["tier"] == 2 for t in templates)


def test_ll_templates_filter_by_status(open_client):
    _create_template(open_client, status="active")
    _create_template(open_client, status="draft", title="Draft one")

    resp = open_client.get(f"{BASE}/legitlibs/templates?status=active")
    assert resp.status_code == 200
    templates = resp.json()["templates"]
    assert len(templates) == 1
    assert templates[0]["status"] == "active"


def test_ll_templates_blanks_count_reflects_blanks_json(open_client):
    _create_template(
        open_client,
        blanks='[{"id":"1","pos":"noun"},{"id":"2","pos":"verb"},{"id":"3","pos":"adj"}]',
    )
    resp = open_client.get(f"{BASE}/legitlibs/templates")
    assert resp.status_code == 200
    t = resp.json()["templates"][0]
    assert t["blanks_count"] == 3


# ── LegitLibs axes ────────────────────────────────────────────────────────────


def test_ll_axes_structure(open_client):
    resp = open_client.get(f"{BASE}/legitlibs/axes")
    assert resp.status_code == 200
    data = resp.json()
    assert "pos_values" in data
    assert "domains_by_pos" in data
    assert "forms_by_pos" in data


def test_ll_axes_has_seeded_pos_values(open_client):
    resp = open_client.get(f"{BASE}/legitlibs/axes")
    pos = {item["value"] for item in resp.json()["pos_values"]}
    assert pos == {"noun", "verb", "adjective", "adverb", "exclamation", "number", "wildcard"}


def test_ll_axes_noun_has_domains(open_client):
    resp = open_client.get(f"{BASE}/legitlibs/axes")
    noun_domains = {item["value"] for item in resp.json()["domains_by_pos"].get("noun", [])}
    assert "place" in noun_domains
    assert "person" in noun_domains


def test_ll_axes_verb_has_forms(open_client):
    resp = open_client.get(f"{BASE}/legitlibs/axes")
    verb_forms = {item["value"] for item in resp.json()["forms_by_pos"].get("verb", [])}
    assert "ing" in verb_forms
    assert "past" in verb_forms
    assert "infinitive" in verb_forms


# ── Config channels ───────────────────────────────────────────────────────────


def test_channels_list_empty(open_client):
    resp = open_client.get(f"{BASE}/config/channels")
    assert resp.status_code == 200
    assert resp.json()["channels"] == []


def test_channels_add_and_appear_in_list(open_client, fake_ctx):
    resp = open_client.post(
        f"{BASE}/config/channels", json={"channel_id": "123456789"}
    )
    assert resp.status_code == 200

    resp = open_client.get(f"{BASE}/config/channels")
    assert resp.status_code == 200
    channels = resp.json()["channels"]
    assert len(channels) == 1
    assert channels[0]["channel_id"] == 123456789


def test_channels_remove(open_client, fake_ctx):
    open_client.post(f"{BASE}/config/channels", json={"channel_id": "555"})

    resp = open_client.delete(f"{BASE}/config/channels/555")
    assert resp.status_code == 200

    channels = open_client.get(f"{BASE}/config/channels").json()["channels"]
    assert channels == []


def test_channels_add_idempotent(open_client, fake_ctx):
    open_client.post(f"{BASE}/config/channels", json={"channel_id": "777"})
    open_client.post(f"{BASE}/config/channels", json={"channel_id": "777"})

    channels = open_client.get(f"{BASE}/config/channels").json()["channels"]
    assert len(channels) == 1


def test_channels_delete_nonexistent_is_ok(open_client):
    resp = open_client.delete(f"{BASE}/config/channels/99999")
    assert resp.status_code == 200


# ── Config audit ──────────────────────────────────────────────────────────────


def test_audit_get_when_empty_returns_null(open_client):
    resp = open_client.get(f"{BASE}/config/audit")
    assert resp.status_code == 200
    assert resp.json() is None


def test_audit_set_and_get(open_client, fake_ctx):
    resp = open_client.put(
        f"{BASE}/config/audit", json={"channel_id": "999999"}
    )
    assert resp.status_code == 200

    resp = open_client.get(f"{BASE}/config/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data is not None
    assert data["guild_id"] == fake_ctx.guild_id
    assert data["channel_id"] == 999999


def test_audit_update_replaces_channel(open_client, fake_ctx):
    open_client.put(f"{BASE}/config/audit", json={"channel_id": "111"})
    open_client.put(f"{BASE}/config/audit", json={"channel_id": "222"})

    data = open_client.get(f"{BASE}/config/audit").json()
    assert data["channel_id"] == 222
