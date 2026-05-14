import logging

log = logging.getLogger(__name__)


class RoundManager:
    """Tracks round state for games with multiple rounds."""

    def __init__(self, game_id: str, total_rounds: int, current_round: int = 0):
        self.game_id = game_id
        self.current_round = current_round
        self.total_rounds = total_rounds

    async def advance(self, db) -> bool:
        """Advance to next round. Returns False if game is over."""
        self.current_round += 1
        if self.current_round > self.total_rounds:
            return False
        await self._persist(db)
        return True

    async def add_rounds(self, count: int, db):
        """Add additional rounds mid-game."""
        self.total_rounds += count
        await self._persist(db)

    async def _persist(self, db):
        from bot_modules.games.utils.game_manager import get_game_payload, update_game_payload
        payload = await get_game_payload(db, self.game_id)
        payload["round_manager"] = self.to_dict()
        await update_game_payload(db, self.game_id, payload)

    def to_dict(self) -> dict:
        return {
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
        }

    @classmethod
    def from_payload(cls, game_id: str, payload: dict) -> "RoundManager":
        rm = payload.get("round_manager", {})
        return cls(
            game_id=game_id,
            total_rounds=rm.get("total_rounds", 1),
            current_round=rm.get("current_round", 0),
        )

    @property
    def is_finished(self) -> bool:
        return self.current_round > self.total_rounds

    @property
    def progress_str(self) -> str:
        return f"Round {self.current_round}/{self.total_rounds}"
