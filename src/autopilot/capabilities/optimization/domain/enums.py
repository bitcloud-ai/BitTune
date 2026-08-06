"""Stable optimization validation codes."""

from enum import StrEnum


class OptimizationValidationCode(StrEnum):
    OUTSIDE_SEARCH_SPACE = "OPTIMIZATION_OUTSIDE_SEARCH_SPACE"
    STATIC_REJECTED = "OPTIMIZATION_STATIC_REJECTED"
    INSUFFICIENT_FEASIBLE_TRIALS = "OPTIMIZATION_INSUFFICIENT_FEASIBLE_TRIALS"
    PLAN_BINDING = "OPTIMIZATION_PLAN_BINDING"
    TRIAL_BINDING = "OPTIMIZATION_TRIAL_BINDING"
    LEDGER_CONFLICT = "OPTIMIZATION_LEDGER_CONFLICT"


class TrialExecutionCode(StrEnum):
    """Classified orchestration failures that do not fit a Trial terminal status."""

    EVIDENCE_RECORDING_FAILED = "TRIAL_EVIDENCE_RECORDING_FAILED"
    CLEANUP_FAILED = "TRIAL_CLEANUP_FAILED"


class TrialExecutionStage(StrEnum):
    """External lifecycle stage that is still running."""

    DEPLOYMENT = "deployment"
    BENCHMARK = "benchmark"


class OptimizationProviderTrialState(StrEnum):
    """Normalized Optuna lifecycle states used by reconciliation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OptimizationProviderCode(StrEnum):
    """Stable failures emitted by the pinned Optuna adapter."""

    PROFILE_UNVERIFIED = "OPTIMIZATION_PROFILE_UNVERIFIED"
    STUDY_BINDING_CONFLICT = "OPTIMIZATION_STUDY_BINDING_CONFLICT"
    TRIAL_NOT_FOUND = "OPTIMIZATION_TRIAL_NOT_FOUND"
    TRIAL_OUTCOME_CONFLICT = "OPTIMIZATION_TRIAL_OUTCOME_CONFLICT"
    STORAGE_FAILURE = "OPTIMIZATION_STORAGE_FAILURE"


class OptimizationRunState(StrEnum):
    """One-step Controller state returned to the Job worker."""

    RUNNING = "running"
    PENDING = "pending"
    STOPPED = "stopped"


class OptimizationStopReason(StrEnum):
    """Deterministic convergence or budget reason."""

    TRIAL_BUDGET = "trial_budget"
    WALL_CLOCK_BUDGET = "wall_clock_budget"
    REQUEST_BUDGET = "request_budget"
    TOKEN_BUDGET = "token_budget"  # noqa: S105
    NO_IMPROVEMENT = "no_improvement"
    SEARCH_SPACE_EXHAUSTED = "search_space_exhausted"
    CANCELLED = "cancelled"


class VerificationRunState(StrEnum):
    """One-step Top-candidate verification lifecycle."""

    RUNNING = "running"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
