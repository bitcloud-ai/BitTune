"""Typed Champion policy failures."""

from autopilot.capabilities.evidence.domain.enums import ChampionPolicyCode


class ChampionPolicyError(ValueError):
    """A deterministic verification or selection failure."""

    def __init__(self, code: ChampionPolicyCode, message: str) -> None:
        self.code = code
        super().__init__(message)
