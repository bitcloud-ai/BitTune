"""Stable string enums persisted by the Autopilot domain."""

from enum import StrEnum


class MeasurementSource(StrEnum):
    ESTIMATED = "estimated"
    MEASURED = "measured"
    DERIVED = "derived"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


class PlanKind(StrEnum):
    ENVIRONMENT = "environment"
    CAPACITY = "capacity"
    DEPLOYMENT = "deployment"
    BENCHMARK = "benchmark"
    OPTIMIZATION = "optimization"
    VERIFICATION = "verification"
    CHAMPION = "champion"
    EVIDENCE = "evidence"


class JobStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class JobKind(StrEnum):
    ENVIRONMENT = "environment"
    DEPLOYMENT = "deployment"
    BENCHMARK = "benchmark"
    OPTIMIZATION = "optimization"
    VERIFICATION = "verification"
    EVIDENCE = "evidence"


class TrialStatus(StrEnum):
    SUGGESTED = "suggested"
    REJECTED_STATIC = "rejected_static"
    DEPLOYMENT_FAILED = "deployment_failed"
    BENCHMARK_FAILED = "benchmark_failed"
    OOM = "oom"
    CONSTRAINT_FAILED = "constraint_failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ErrorCategory(StrEnum):
    VALIDATION_ERROR = "validation_error"
    POLICY_DENIED = "policy_denied"
    RESOURCE_BUSY = "resource_busy"
    DEPLOYMENT_ERROR = "deployment_error"
    MODEL_INCOMPATIBLE = "model_incompatible"
    BENCHMARK_ERROR = "benchmark_error"
    OOM = "oom"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    UNKNOWN_ERROR = "unknown_error"


class SuggestedAction(StrEnum):
    REVISE_PLAN = "revise_plan"
    CHECK_ENVIRONMENT = "check_environment"
    WAIT_FOR_RESOURCE = "wait_for_resource"
    REQUEST_APPROVAL = "request_approval"
    CONTACT_OPERATOR = "contact_operator"


class ExperimentPhase(StrEnum):
    REQUIREMENTS = "requirements"
    ENVIRONMENT = "environment"
    PLANNING = "planning"
    APPROVAL = "approval"
    DEPLOYMENT = "deployment"
    BENCHMARK = "benchmark"
    OPTIMIZATION = "optimization"
    VERIFICATION = "verification"
    REPORT = "report"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentStatus(StrEnum):
    ACTIVE = "active"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class UserRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class TrafficMode(StrEnum):
    BASELINE = "baseline"
    CLOSED_LOOP_SWEEP = "closed_loop_sweep"
    OPEN_LOOP_SWEEP = "open_loop_sweep"
    SLA_SEARCH = "sla_search"


class ObjectiveDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class NumericMetric(StrEnum):
    E2E_P50_MS = "e2e_p50_ms"
    E2E_P95_MS = "e2e_p95_ms"
    E2E_P99_MS = "e2e_p99_ms"
    TTFT_P50_MS = "ttft_p50_ms"
    TTFT_P95_MS = "ttft_p95_ms"
    TTFT_P99_MS = "ttft_p99_ms"
    TPOT_P50_MS = "tpot_p50_ms"
    TPOT_P95_MS = "tpot_p95_ms"
    TPOT_P99_MS = "tpot_p99_ms"
    ITL_P50_MS = "itl_p50_ms"
    ITL_P95_MS = "itl_p95_ms"
    ITL_P99_MS = "itl_p99_ms"
    REQUESTS_PER_SECOND = "requests_per_second"
    SUCCESSFUL_REQUESTS_PER_MINUTE = "successful_requests_per_minute"
    INPUT_TOKENS_PER_SECOND = "input_tokens_per_second"
    SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND = "successful_output_tokens_per_second"
    TOTAL_TOKENS_PER_MINUTE = "total_tokens_per_minute"
    SUCCESS_RATE = "success_rate"
    WINDOW_COMPLETION_RATIO = "window_completion_ratio"


class BooleanMetric(StrEnum):
    OOM = "oom"


class NumericOperator(StrEnum):
    LESS_THAN_OR_EQUAL = "<="
    GREATER_THAN_OR_EQUAL = ">="


class BooleanOperator(StrEnum):
    EQUAL = "=="
