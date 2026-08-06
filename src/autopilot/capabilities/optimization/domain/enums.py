"""Stable optimization validation codes."""

from enum import StrEnum


class OptimizationValidationCode(StrEnum):
    OUTSIDE_SEARCH_SPACE = "OPTIMIZATION_OUTSIDE_SEARCH_SPACE"
    STATIC_REJECTED = "OPTIMIZATION_STATIC_REJECTED"
    INSUFFICIENT_FEASIBLE_TRIALS = "OPTIMIZATION_INSUFFICIENT_FEASIBLE_TRIALS"


class TrialExecutionCode(StrEnum):
    """Classified orchestration failures that do not fit a Trial terminal status."""

    EVIDENCE_RECORDING_FAILED = "TRIAL_EVIDENCE_RECORDING_FAILED"
    CLEANUP_FAILED = "TRIAL_CLEANUP_FAILED"


class TrialExecutionStage(StrEnum):
    """External lifecycle stage that is still running."""

    DEPLOYMENT = "deployment"
    BENCHMARK = "benchmark"
