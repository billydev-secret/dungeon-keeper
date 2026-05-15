# tests/unit/test_veil_repo.py
"""Tier 1 unit tests: guess repo layer (sync sqlite3)."""
from __future__ import annotations

from pathlib import Path


from bot_modules.core.db_utils import open_db
from bot_modules.services.guess_repo import (
    count_guesses_for_round,
    count_unique_guessers_for_round,
    delete_optin,
    get_active_rounds_for_guild,
    get_all_active_round_ids,
    get_all_optins_for_guild,
    get_guesses_for_round,
    get_last_guess_by_user_for_round,
    get_optin,
    get_reusable_rounds,
    get_round,
    get_guess_config,
    insert_guess,
    insert_round,
    is_opted_in,
    mark_round_solved,
    set_round_answer_optout,
    set_round_reroll_count,
    set_guess_config_value,
    soft_delete_round,
    update_round_message,
    upsert_optin,
)

GUILD = 9001
USER_A = 1001
USER_B = 1002


def test_get_veil_config_defaults(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        cfg = get_guess_config(conn, GUILD)
    assert cfg.guild_id == GUILD
    assert cfg.crop_difficulty == "medium"
    assert cfg.guess_cooldown_seconds == 60


def test_set_and_get_veil_config_value(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_guess_config_value(conn, GUILD, "guess_crop_difficulty", "hard")
        cfg = get_guess_config(conn, GUILD)
    assert cfg.crop_difficulty == "hard"


def test_insert_and_get_round(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B, difficulty="hard", candidate_count=3)
        r = get_round(conn, rid)
    assert r is not None
    assert r.id == rid
    assert r.difficulty == "hard"
    assert r.candidate_count == 3
    assert r.allow_reuse is False
    assert r.solved_at is None
    assert r.deleted_at is None


def test_get_round_nonexistent_returns_none(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        assert get_round(conn, 9999) is None


def test_soft_delete_sets_deleted_at(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        soft_delete_round(conn, rid)
        r = get_round(conn, rid)
    assert r is not None
    assert r.deleted_at is not None


def test_mark_round_solved(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        mark_round_solved(conn, rid, solver_id=USER_B, guesses_to_solve=3, unique_guessers_to_solve=2)
        r = get_round(conn, rid)
    assert r is not None
    assert r.solver_id == USER_B
    assert r.guesses_to_solve == 3
    assert r.solved_at is not None


def test_set_answer_optout(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        set_round_answer_optout(conn, rid)
        r = get_round(conn, rid)
    assert r is not None
    assert r.answer_optout is True


def test_get_active_rounds_excludes_deleted(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        r1 = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        r2 = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        soft_delete_round(conn, r1)
        active_ids = {r.id for r in get_active_rounds_for_guild(conn, GUILD)}
    assert r2 in active_ids
    assert r1 not in active_ids


def test_get_reusable_rounds_includes_eligible(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B, allow_reuse=True)
        mark_round_solved(conn, rid, solver_id=USER_B, guesses_to_solve=1, unique_guessers_to_solve=1)
        rounds = get_reusable_rounds(conn, GUILD, min_age_seconds=0)
    assert any(r.id == rid for r in rounds)


def test_get_reusable_rounds_excludes_no_allow_reuse(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B, allow_reuse=False)
        mark_round_solved(conn, rid, solver_id=USER_B, guesses_to_solve=1, unique_guessers_to_solve=1)
        rounds = get_reusable_rounds(conn, GUILD, min_age_seconds=0)
    assert not any(r.id == rid for r in rounds)


def test_insert_and_get_guesses(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        gid = insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_A, correct=False)
        guesses = get_guesses_for_round(conn, rid)
    assert len(guesses) == 1
    assert guesses[0].id == gid
    assert guesses[0].correct is False


def test_count_guesses_for_round(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_A, correct=False)
        insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_B, correct=True)
        assert count_guesses_for_round(conn, rid) == 2


def test_count_unique_guessers(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_A, correct=False)
        insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_B, correct=True)
        insert_guess(conn, round_id=rid, guesser_id=2002, guessed_user_id=USER_B, correct=True)
        assert count_unique_guessers_for_round(conn, rid) == 2


def test_get_last_guess_by_user(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_A, correct=False)
        g2 = insert_guess(conn, round_id=rid, guesser_id=USER_B, guessed_user_id=USER_B, correct=True)
        last = get_last_guess_by_user_for_round(conn, rid, USER_B)
    assert last is not None
    assert last.id == g2


def test_upsert_and_get_optin(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        upsert_optin(conn, USER_A, GUILD)
        optin = get_optin(conn, USER_A, GUILD)
    assert optin is not None
    assert optin.user_id == USER_A


def test_is_opted_in_true_and_false(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        upsert_optin(conn, USER_A, GUILD)
        assert is_opted_in(conn, USER_A, GUILD) is True
        assert is_opted_in(conn, USER_B, GUILD) is False


def test_delete_optin(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        upsert_optin(conn, USER_A, GUILD)
        assert delete_optin(conn, USER_A, GUILD) is True
        assert is_opted_in(conn, USER_A, GUILD) is False


def test_delete_optin_nonexistent_returns_false(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        assert delete_optin(conn, 9999, GUILD) is False


def test_get_all_optins_for_guild(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        upsert_optin(conn, USER_A, GUILD)
        upsert_optin(conn, USER_B, GUILD)
        optins = get_all_optins_for_guild(conn, GUILD)
    assert {o.user_id for o in optins} == {USER_A, USER_B}


def test_update_round_message(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        update_round_message(conn, rid, message_id=42, crop_url="https://example.com/crop.jpg", crop_path="/tmp/crop.jpg")
        r = get_round(conn, rid)
    assert r is not None
    assert r.message_id == 42
    assert r.crop_url == "https://example.com/crop.jpg"
    assert r.crop_path == "/tmp/crop.jpg"


def test_set_round_reroll_count(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        set_round_reroll_count(conn, rid, 3)
        r = get_round(conn, rid)
    assert r is not None
    assert r.reroll_count == 3


def test_get_all_active_round_ids_returns_empty_when_no_rounds(sync_db_path):
    with open_db(sync_db_path) as conn:
        result = get_all_active_round_ids(conn)
    assert result == []


def test_get_all_active_round_ids_returns_unsolved_and_solved(sync_db_path):
    with open_db(sync_db_path) as conn:
        rid1 = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        rid2 = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        mark_round_solved(conn, rid2, solver_id=USER_B,
                          guesses_to_solve=1, unique_guessers_to_solve=1)
        result = get_all_active_round_ids(conn)
    ids_solved = {(rid, solved) for rid, solved in result}
    assert (rid1, False) in ids_solved
    assert (rid2, True) in ids_solved


def test_get_all_active_round_ids_excludes_deleted(sync_db_path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        soft_delete_round(conn, rid)
        result = get_all_active_round_ids(conn)
    assert result == []
