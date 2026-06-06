import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto

from .logic import run_tie_rolloff

log = logging.getLogger(__name__)


class RoundResult(Enum):
    NOT_ENOUGH = auto()
    WAITING_FOR_REROLLS = auto()
    TIE = auto()
    SIXTYNINE = auto()
    SIXTYNINE_TIE = auto()
    OK = auto()


class PromptKind(str, Enum):
    ROOM = "room"
    DIRECT = "direct"
    TWO_QUESTIONERS = "two_questioners"


@dataclass
class ResolutionResult:
    result_type: RoundResult
    rolloff_user_ids: list[int] = field(default_factory=list)
    rolloff_rounds: list[dict[int, int]] | None = None
    lowest_rolloff_user_ids: list[int] = field(default_factory=list)
    lowest_rolloff_rounds: list[dict[int, int]] | None = None


@dataclass
class RiskyRollState:
    channel_id: int
    guild_id: int
    opener_id: int
    game_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message_id: int | None = None
    rolls: dict[int, int] = field(default_factory=dict)
    is_open: bool = True
    highest_user: int | None = None
    lowest_user: int | None = None
    lowest_tie_user_ids: set[int] = field(default_factory=set)
    highest_tie_user_ids: set[int] = field(default_factory=set)
    reroll_user_ids: set[int] = field(default_factory=set)
    auto_close_players: int | None = None
    auto_close_minutes: int | None = None
    created_at: float = field(default_factory=time.time)
    skip_min_game_time: bool = False
    second_lowest_user: int | None = None
    second_highest_user: int | None = None
    second_lowest_tie_user_ids: set[int] = field(default_factory=set)
    second_highest_tie_user_ids: set[int] = field(default_factory=set)

    def add_roll(self, user_id: int, value: int) -> None:
        self.rolls[user_id] = value
        if self.reroll_user_ids:
            completed = {uid for uid in self.reroll_user_ids if uid in self.rolls}
            if completed == self.reroll_user_ids:
                self.reroll_user_ids.clear()

    def can_roll(self, user_id: int) -> bool:
        if self.reroll_user_ids:
            return user_id in self.reroll_user_ids and user_id not in self.rolls
        return user_id not in self.rolls

    def prepare_reroll(self, user_ids: list[int]) -> None:
        self.reroll_user_ids = set(user_ids)
        for uid in self.reroll_user_ids:
            self.rolls.pop(uid, None)
        self.highest_user = None
        self.lowest_user = None
        self.lowest_tie_user_ids.clear()
        self.highest_tie_user_ids.clear()
        self.second_lowest_user = None
        self.second_highest_user = None
        self.second_lowest_tie_user_ids.clear()
        self.second_highest_tie_user_ids.clear()

    def reroll_mentions(self) -> str:
        return ", ".join(f"<@{uid}>" for uid in sorted(self.reroll_user_ids))

    def pending_reroll_mentions(self) -> str:
        pending = [uid for uid in self.reroll_user_ids if uid not in self.rolls]
        return ", ".join(f"<@{uid}>" for uid in pending)

    def _find_second_extreme(self, *, pick_lowest: bool) -> tuple[int | None, set[int]]:
        if self.highest_user is None or self.lowest_user is None:
            return None, set()
        candidates = [
            (uid, r) for uid, r in self.rolls.items()
            if uid != self.highest_user and uid != self.lowest_user
        ]
        if not candidates:
            return None, set()
        target_val = min(r for _, r in candidates) if pick_lowest else max(r for _, r in candidates)
        tied = [uid for uid, r in candidates if r == target_val]
        if len(tied) == 1:
            return tied[0], set()
        winner_id, _ = run_tie_rolloff(tied, pick_lowest=pick_lowest)
        return winner_id, set(tied)

    def _apply_special_roll_rules(self) -> None:
        if self.highest_user is not None and self.rolls.get(self.highest_user) == 100:
            self.second_lowest_user, self.second_lowest_tie_user_ids = self._find_second_extreme(pick_lowest=True)
        if self.lowest_user is not None and self.rolls.get(self.lowest_user) == 1:
            self.second_highest_user, self.second_highest_tie_user_ids = self._find_second_extreme(pick_lowest=False)

    def resolve(self) -> ResolutionResult:
        self.lowest_tie_user_ids.clear()
        self.highest_tie_user_ids.clear()
        self.second_lowest_tie_user_ids.clear()
        self.second_highest_tie_user_ids.clear()

        if self.reroll_user_ids and any(uid not in self.rolls for uid in self.reroll_user_ids):
            return ResolutionResult(result_type=RoundResult.WAITING_FOR_REROLLS)

        if len(self.rolls) < 2:
            return ResolutionResult(result_type=RoundResult.NOT_ENOUGH)

        max_value = max(self.rolls.values())
        min_value = min(self.rolls.values())

        sixtyniners = [uid for uid, roll in self.rolls.items() if roll == 69]
        if sixtyniners:
            if len(sixtyniners) > 1:
                winner_id, rolloff_rounds = run_tie_rolloff(sixtyniners)
                self.highest_user = winner_id
                self.lowest_user = None
                self.highest_tie_user_ids = set(sixtyniners)
                self.is_open = False
                log.info("Game %s: 69 tie resolved. Winner: %s", self.game_id, winner_id)
                return ResolutionResult(
                    result_type=RoundResult.SIXTYNINE_TIE,
                    rolloff_user_ids=sixtyniners,
                    rolloff_rounds=rolloff_rounds,
                )
            self.highest_user = sixtyniners[0]
            self.lowest_user = None
            self.is_open = False
            log.info("Game %s: 69 rolled by %s", self.game_id, sixtyniners[0])
            return ResolutionResult(result_type=RoundResult.SIXTYNINE)

        highest_users = [uid for uid, roll in self.rolls.items() if roll == max_value]
        if len(highest_users) > 1:
            winner_id, rolloff_rounds = run_tie_rolloff(highest_users)
            self.highest_tie_user_ids = set(highest_users)
            remaining = [uid for uid in self.rolls if uid != winner_id]
            lowest_rolloff_user_ids: list[int] = []
            lowest_rolloff_rounds: list[dict[int, int]] | None = None
            if remaining:
                min_roll = min(self.rolls[uid] for uid in remaining)
                lowest_tied = [u for u in remaining if self.rolls[u] == min_roll]
                if len(lowest_tied) > 1:
                    lowest_id, lowest_rolloff_rounds = run_tie_rolloff(lowest_tied, pick_lowest=True)
                    self.lowest_tie_user_ids = set(lowest_tied)
                    lowest_rolloff_user_ids = lowest_tied
                else:
                    lowest_id = lowest_tied[0]
            else:
                lowest_id = winner_id

            self.highest_user = winner_id
            self.lowest_user = lowest_id
            self.is_open = False
            self.reroll_user_ids.clear()
            self._apply_special_roll_rules()
            log.info("Game %s: highest tie resolved. Winner: %s, Lowest: %s", self.game_id, winner_id, lowest_id)
            return ResolutionResult(
                result_type=RoundResult.TIE,
                rolloff_user_ids=highest_users,
                rolloff_rounds=rolloff_rounds,
                lowest_rolloff_user_ids=lowest_rolloff_user_ids,
                lowest_rolloff_rounds=lowest_rolloff_rounds,
            )

        lowest_users = [uid for uid, roll in self.rolls.items() if roll == min_value]
        lowest_rolloff_rounds = None
        if len(lowest_users) > 1:
            lowest_id, lowest_rolloff_rounds = run_tie_rolloff(lowest_users, pick_lowest=True)
            self.lowest_tie_user_ids = set(lowest_users)
            log.info("Game %s: lowest tie resolved. Selected: %s", self.game_id, lowest_id)
        else:
            lowest_id = lowest_users[0]

        self.highest_user = highest_users[0]
        self.lowest_user = lowest_id
        self.is_open = False
        self._apply_special_roll_rules()
        log.info("Game %s: resolved. Winner: %s, Lowest: %s", self.game_id, highest_users[0], lowest_id)
        return ResolutionResult(
            result_type=RoundResult.OK,
            lowest_rolloff_user_ids=lowest_users if lowest_rolloff_rounds else [],
            lowest_rolloff_rounds=lowest_rolloff_rounds,
        )


@dataclass
class PendingQuestionState:
    channel_id: int
    guild_id: int
    winner_id: int
    participant_user_ids: set[int]
    game_id: str
    lowest_tie_user_ids: set[int] = field(default_factory=set)
    prompt_message_id: int | None = None
    prompt_kind: PromptKind = PromptKind.ROOM
    extra_questioner_id: int | None = None
    questioners_asked: set[int] = field(default_factory=set)

    @property
    def questions_remaining(self) -> int:
        total = 1 + (1 if self.extra_questioner_id is not None else 0)
        return total - len(self.questioners_asked)

    def allowed_questioners(self) -> set[int]:
        ids = {self.winner_id}
        if self.extra_questioner_id is not None:
            ids.add(self.extra_questioner_id)
        return ids


@dataclass
class PostedQuestionState:
    message_id: int
    channel_id: int
    guild_id: int
    asker_id: int
    allowed_replier_ids: set[int]
    question_text: str
    asker_rolled_100: bool = False
    target_rolled_1: bool = False
    created_at: float = field(default_factory=time.time)
