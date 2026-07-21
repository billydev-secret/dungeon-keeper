import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from .logic import deserialize_user_ids, serialize_user_ids
from .models import PendingQuestionState, PostedQuestionState, PromptKind, RiskyRollState

log = logging.getLogger(__name__)

MAX_GAMES_PER_CHANNEL = 10
_POSTED_Q_MAX_AGE = 7 * 24 * 3600  # 7 days


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self._path = str(db_path)

    @property
    def db_path(self) -> str:
        """The SQLite path, exposed so the module can resolve embed accents."""
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # Guild settings — stored in DK's shared config KV table
    # ------------------------------------------------------------------

    def _load_ping_roles(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, value FROM config WHERE key = 'risky_ping_role_id'"
            ).fetchall()
        return {int(row["guild_id"]): int(row["value"]) for row in rows}

    async def load_ping_roles(self) -> dict[int, int]:
        return await asyncio.to_thread(self._load_ping_roles)

    def _set_ping_role(self, guild_id: int, role_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, 'risky_ping_role_id', ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, str(role_id)),
            )

    async def set_ping_role(self, guild_id: int, role_id: int) -> None:
        await asyncio.to_thread(self._set_ping_role, guild_id, role_id)

    def _load_min_game_times(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, value FROM config WHERE key = 'risky_min_game_seconds'"
            ).fetchall()
        return {int(row["guild_id"]): int(row["value"]) for row in rows}

    async def load_min_game_times(self) -> dict[int, int]:
        return await asyncio.to_thread(self._load_min_game_times)

    def _set_min_game_time(self, guild_id: int, seconds: int | None) -> None:
        with self._connect() as conn:
            if seconds is None:
                conn.execute(
                    "DELETE FROM config WHERE guild_id = ? AND key = 'risky_min_game_seconds'",
                    (guild_id,),
                )
            else:
                conn.execute(
                    "INSERT INTO config (guild_id, key, value) VALUES (?, 'risky_min_game_seconds', ?) "
                    "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                    (guild_id, str(seconds)),
                )

    async def set_min_game_time(self, guild_id: int, seconds: int | None) -> None:
        await asyncio.to_thread(self._set_min_game_time, guild_id, seconds)

    def _load_max_games_per_channel(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, value FROM config WHERE key = 'risky_max_games_per_channel'"
            ).fetchall()
        return {int(row["guild_id"]): int(row["value"]) for row in rows}

    async def load_max_games_per_channel(self) -> dict[int, int]:
        return await asyncio.to_thread(self._load_max_games_per_channel)

    def _set_max_games_per_channel(self, guild_id: int, cap: int | None) -> None:
        with self._connect() as conn:
            if cap is None:
                conn.execute(
                    "DELETE FROM config WHERE guild_id = ? AND key = 'risky_max_games_per_channel'",
                    (guild_id,),
                )
            else:
                conn.execute(
                    "INSERT INTO config (guild_id, key, value) VALUES (?, 'risky_max_games_per_channel', ?) "
                    "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                    (guild_id, str(cap)),
                )

    async def set_max_games_per_channel(self, guild_id: int, cap: int | None) -> None:
        await asyncio.to_thread(self._set_max_games_per_channel, guild_id, cap)

    # ------------------------------------------------------------------
    # Active rounds
    # ------------------------------------------------------------------

    def _save_round(self, state: RiskyRollState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risky_active_rounds (
                    game_id, channel_id, guild_id, opener_id, message_id, is_open,
                    highest_user, lowest_user, reroll_user_ids,
                    auto_close_players, auto_close_minutes, created_at,
                    skip_min_game_time, second_lowest_user, second_highest_user
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    opener_id = excluded.opener_id,
                    message_id = excluded.message_id,
                    is_open = excluded.is_open,
                    highest_user = excluded.highest_user,
                    lowest_user = excluded.lowest_user,
                    reroll_user_ids = excluded.reroll_user_ids,
                    auto_close_players = excluded.auto_close_players,
                    auto_close_minutes = excluded.auto_close_minutes,
                    created_at = excluded.created_at,
                    skip_min_game_time = excluded.skip_min_game_time,
                    second_lowest_user = excluded.second_lowest_user,
                    second_highest_user = excluded.second_highest_user
                """,
                (
                    state.game_id, state.channel_id, state.guild_id, state.opener_id,
                    state.message_id, int(state.is_open), state.highest_user,
                    state.lowest_user, serialize_user_ids(state.reroll_user_ids),
                    state.auto_close_players, state.auto_close_minutes, state.created_at,
                    int(state.skip_min_game_time), state.second_lowest_user,
                    state.second_highest_user,
                ),
            )
            for user_id, roll in state.rolls.items():
                conn.execute(
                    "INSERT INTO risky_round_rolls (game_id, user_id, roll) VALUES (?, ?, ?) "
                    "ON CONFLICT(game_id, user_id) DO UPDATE SET roll = excluded.roll",
                    (state.game_id, user_id, roll),
                )

    async def save_round(self, state: RiskyRollState) -> None:
        await asyncio.to_thread(self._save_round, state)

    def _save_single_roll(self, game_id: str, user_id: int, roll: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO risky_round_rolls (game_id, user_id, roll) VALUES (?, ?, ?) "
                "ON CONFLICT(game_id, user_id) DO UPDATE SET roll = excluded.roll",
                (game_id, user_id, roll),
            )

    async def save_single_roll(self, game_id: str, user_id: int, roll: int) -> None:
        await asyncio.to_thread(self._save_single_roll, game_id, user_id, roll)

    def _delete_round(self, game_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM risky_active_rounds WHERE game_id = ?", (game_id,))

    async def delete_round(self, game_id: str) -> None:
        await asyncio.to_thread(self._delete_round, game_id)

    def _load_active_rounds(self) -> list[RiskyRollState]:
        with self._connect() as conn:
            round_rows = conn.execute(
                """
                SELECT game_id, channel_id, guild_id, opener_id, message_id, is_open,
                       highest_user, lowest_user, reroll_user_ids,
                       auto_close_players, auto_close_minutes, created_at,
                       skip_min_game_time, second_lowest_user, second_highest_user
                FROM risky_active_rounds
                WHERE is_open = 1
                """
            ).fetchall()

            states = {
                str(row["game_id"]): RiskyRollState(
                    game_id=str(row["game_id"]),
                    channel_id=int(row["channel_id"]),
                    guild_id=int(row["guild_id"]),
                    opener_id=int(row["opener_id"]),
                    message_id=int(row["message_id"]) if row["message_id"] is not None else None,
                    is_open=bool(row["is_open"]),
                    highest_user=int(row["highest_user"]) if row["highest_user"] is not None else None,
                    lowest_user=int(row["lowest_user"]) if row["lowest_user"] is not None else None,
                    reroll_user_ids=deserialize_user_ids(row["reroll_user_ids"]),
                    auto_close_players=int(row["auto_close_players"]) if row["auto_close_players"] is not None else None,
                    auto_close_minutes=int(row["auto_close_minutes"]) if row["auto_close_minutes"] is not None else None,
                    created_at=float(row["created_at"]) if row["created_at"] is not None else time.time(),
                    skip_min_game_time=bool(row["skip_min_game_time"]),
                    second_lowest_user=int(row["second_lowest_user"]) if row["second_lowest_user"] is not None else None,
                    second_highest_user=int(row["second_highest_user"]) if row["second_highest_user"] is not None else None,
                )
                for row in round_rows
            }

            roll_rows = conn.execute(
                """
                SELECT game_id, user_id, roll FROM risky_round_rolls
                WHERE game_id IN (SELECT game_id FROM risky_active_rounds WHERE is_open = 1)
                ORDER BY roll DESC
                """
            ).fetchall()

        for row in roll_rows:
            game_id = str(row["game_id"])
            s = states.get(game_id)
            if s is not None:
                s.rolls[int(row["user_id"])] = int(row["roll"])

        return list(states.values())

    async def load_active_rounds(self) -> list[RiskyRollState]:
        return await asyncio.to_thread(self._load_active_rounds)

    # ------------------------------------------------------------------
    # Pending questions
    # ------------------------------------------------------------------

    def _save_pending_question(self, state: PendingQuestionState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risky_pending_questions (
                    game_id, channel_id, guild_id, winner_id, prompt_message_id,
                    participant_user_ids, lowest_tie_user_ids, prompt_kind,
                    extra_questioner_id, questioners_asked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    winner_id = excluded.winner_id,
                    prompt_message_id = excluded.prompt_message_id,
                    participant_user_ids = excluded.participant_user_ids,
                    lowest_tie_user_ids = excluded.lowest_tie_user_ids,
                    prompt_kind = excluded.prompt_kind,
                    extra_questioner_id = excluded.extra_questioner_id,
                    questioners_asked = excluded.questioners_asked
                """,
                (
                    state.game_id, state.channel_id, state.guild_id, state.winner_id,
                    state.prompt_message_id,
                    serialize_user_ids(state.participant_user_ids),
                    serialize_user_ids(state.lowest_tie_user_ids),
                    state.prompt_kind,
                    state.extra_questioner_id,
                    serialize_user_ids(state.questioners_asked),
                ),
            )

    async def save_pending_question(self, state: PendingQuestionState) -> None:
        await asyncio.to_thread(self._save_pending_question, state)

    def _delete_pending_question(self, game_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM risky_pending_questions WHERE game_id = ?", (game_id,))

    async def delete_pending_question(self, game_id: str) -> None:
        await asyncio.to_thread(self._delete_pending_question, game_id)

    def _load_pending_questions(self) -> list[PendingQuestionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT game_id, channel_id, guild_id, winner_id, prompt_message_id,
                       participant_user_ids, lowest_tie_user_ids, prompt_kind,
                       extra_questioner_id, questioners_asked
                FROM risky_pending_questions
                """
            ).fetchall()

        return [
            PendingQuestionState(
                game_id=str(row["game_id"]),
                channel_id=int(row["channel_id"]),
                guild_id=int(row["guild_id"]),
                winner_id=int(row["winner_id"]),
                participant_user_ids=deserialize_user_ids(row["participant_user_ids"]),
                prompt_message_id=int(row["prompt_message_id"]) if row["prompt_message_id"] is not None else None,
                lowest_tie_user_ids=deserialize_user_ids(row["lowest_tie_user_ids"]),
                prompt_kind=PromptKind(row["prompt_kind"] or PromptKind.ROOM.value),
                extra_questioner_id=int(row["extra_questioner_id"]) if row["extra_questioner_id"] is not None else None,
                questioners_asked=deserialize_user_ids(row["questioners_asked"]),
            )
            for row in rows
        ]

    async def load_pending_questions(self) -> list[PendingQuestionState]:
        return await asyncio.to_thread(self._load_pending_questions)

    # ------------------------------------------------------------------
    # Posted questions
    # ------------------------------------------------------------------

    def _save_posted_question(self, state: PostedQuestionState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risky_posted_questions (
                    message_id, channel_id, guild_id, asker_id,
                    allowed_replier_ids, question_text,
                    asker_rolled_100, target_rolled_1, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    asker_id = excluded.asker_id,
                    allowed_replier_ids = excluded.allowed_replier_ids,
                    question_text = excluded.question_text,
                    asker_rolled_100 = excluded.asker_rolled_100,
                    target_rolled_1 = excluded.target_rolled_1
                """,
                (
                    state.message_id, state.channel_id, state.guild_id, state.asker_id,
                    serialize_user_ids(state.allowed_replier_ids),
                    state.question_text,
                    int(state.asker_rolled_100), int(state.target_rolled_1),
                    int(state.created_at),
                ),
            )

    async def save_posted_question(self, state: PostedQuestionState) -> None:
        await asyncio.to_thread(self._save_posted_question, state)

    def _delete_posted_question(self, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM risky_posted_questions WHERE message_id = ?", (message_id,))

    async def delete_posted_question(self, message_id: int) -> None:
        await asyncio.to_thread(self._delete_posted_question, message_id)

    def _load_posted_questions(self) -> list[PostedQuestionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, channel_id, guild_id, asker_id,
                       allowed_replier_ids, question_text,
                       asker_rolled_100, target_rolled_1, created_at
                FROM risky_posted_questions
                """
            ).fetchall()

        return [
            PostedQuestionState(
                message_id=int(row["message_id"]),
                channel_id=int(row["channel_id"]),
                guild_id=int(row["guild_id"]),
                asker_id=int(row["asker_id"]),
                allowed_replier_ids=deserialize_user_ids(row["allowed_replier_ids"]),
                question_text=str(row["question_text"]),
                asker_rolled_100=bool(row["asker_rolled_100"]),
                target_rolled_1=bool(row["target_rolled_1"]),
                created_at=float(row["created_at"]) if row["created_at"] is not None else time.time(),
            )
            for row in rows
        ]

    async def load_posted_questions(self) -> list[PostedQuestionState]:
        return await asyncio.to_thread(self._load_posted_questions)

    def _sweep_old_posted_questions(self) -> int:
        cutoff = int(time.time()) - _POSTED_Q_MAX_AGE
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM risky_posted_questions WHERE created_at IS NOT NULL AND created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount or 0

    async def sweep_old_posted_questions(self) -> int:
        return await asyncio.to_thread(self._sweep_old_posted_questions)

    def _delete_guild_data(self, guild_id: int) -> list[str]:
        with self._connect() as conn:
            game_id_rows = conn.execute(
                "SELECT game_id FROM risky_active_rounds WHERE guild_id = ?", (guild_id,)
            ).fetchall()
            game_ids = [str(row["game_id"]) for row in game_id_rows]

            conn.execute("DELETE FROM risky_active_rounds WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM risky_pending_questions WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM risky_posted_questions WHERE guild_id = ?", (guild_id,))
            conn.execute(
                "DELETE FROM config WHERE guild_id = ? AND key IN "
                "('risky_ping_role_id', 'risky_min_game_seconds', 'risky_max_games_per_channel')",
                (guild_id,),
            )
        return game_ids

    async def delete_guild_data(self, guild_id: int) -> list[str]:
        return await asyncio.to_thread(self._delete_guild_data, guild_id)
