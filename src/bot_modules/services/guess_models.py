"""Guess cog data models."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass
class Detection:
    label: str
    score: float
    box: BoundingBox


@dataclass
class GuessConfig:
    guild_id: int
    guess_role_id: int = 0
    guess_channel_id: int = 0
    guess_cooldown_seconds: int = 60
    crop_difficulty: str = "medium"
    min_image_dimension_px: int = 400
    max_image_size_mb: int = 10
    prompt_message_id: int = 0


@dataclass
class GuessRound:
    id: int
    guild_id: int
    submitter_id: int
    answer_id: int
    channel_id: int
    message_id: int
    crop_path: str
    crop_url: str
    original_path: str
    difficulty: str
    candidate_count: int
    reroll_count: int
    allow_reuse: bool
    is_reuse: bool
    original_round_id: int | None
    reuse_blocked: bool
    created_at: float
    solved_at: float | None
    solver_id: int | None
    guesses_to_solve: int | None
    unique_guessers_to_solve: int | None
    answer_optout: bool
    deleted_at: float | None
    crop_box_x1: float | None = None
    crop_box_y1: float | None = None
    crop_box_x2: float | None = None
    crop_box_y2: float | None = None
    round_type: str = "photo"
    confession_text: str = ""
    confession_prompt_text: str = ""


@dataclass
class GuessGuess:
    id: int
    round_id: int
    guesser_id: int
    guessed_user_id: int
    correct: bool
    created_at: float


@dataclass
class GuessOptin:
    user_id: int
    guild_id: int
    opted_in_at: float


@dataclass
class PipelineResult:
    candidates: list[Detection]
    crops: list[bytes] = field(default_factory=list)


@dataclass
class GuessAuditEvent:
    id: int
    guild_id: int
    ts: float
    actor_id: int
    action: str
    round_id: int | None
    details: str  # JSON-encoded; callers parse on demand
