"""Stable deployment capability enums."""

from enum import StrEnum


class DeploymentValidationCode(StrEnum):
    VERSION_MISMATCH = "DEPLOYMENT_VERSION_MISMATCH"
    IMAGE_MISMATCH = "DEPLOYMENT_IMAGE_MISMATCH"
    WORKLOAD_MISMATCH = "DEPLOYMENT_WORKLOAD_MISMATCH"
    CONTEXT_EXCEEDED = "DEPLOYMENT_CONTEXT_EXCEEDED"
    PARAMETER_UNSUPPORTED = "DEPLOYMENT_PARAMETER_UNSUPPORTED"
    BUDGET_EXCEEDED = "DEPLOYMENT_BUDGET_EXCEEDED"
    PROVIDER_UNAVAILABLE = "DEPLOYMENT_PROVIDER_UNAVAILABLE"
    MODEL_REF_UNSUPPORTED = "DEPLOYMENT_MODEL_REF_UNSUPPORTED"
    RUNNER_REJECTED = "DEPLOYMENT_RUNNER_REJECTED"
    PROFILE_UNVERIFIED = "DEPLOYMENT_PROFILE_UNVERIFIED"


class DeploymentProviderState(StrEnum):
    """Normalized vLLM lifecycle states."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    HEALTHY = "healthy"
    STOPPED = "stopped"
    FAILED = "failed"


class DeploymentHealthCheck(StrEnum):
    PROCESS = "process"
    HTTP = "http"
    MODEL_LIST = "model_list"
    MINIMAL_COMPLETION = "minimal_completion"
    NON_EMPTY_COMPLETION = "non_empty_completion"
    GPU_MEMORY = "gpu_memory"
    FATAL_LOG = "fatal_log"
