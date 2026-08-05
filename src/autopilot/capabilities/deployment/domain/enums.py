"""Stable deployment capability enums."""

from enum import StrEnum


class DeploymentValidationCode(StrEnum):
    VERSION_MISMATCH = "DEPLOYMENT_VERSION_MISMATCH"
    IMAGE_MISMATCH = "DEPLOYMENT_IMAGE_MISMATCH"
    WORKLOAD_MISMATCH = "DEPLOYMENT_WORKLOAD_MISMATCH"
    CONTEXT_EXCEEDED = "DEPLOYMENT_CONTEXT_EXCEEDED"
    PARAMETER_UNSUPPORTED = "DEPLOYMENT_PARAMETER_UNSUPPORTED"
    BUDGET_EXCEEDED = "DEPLOYMENT_BUDGET_EXCEEDED"


class DeploymentHealthCheck(StrEnum):
    PROCESS = "process"
    HTTP = "http"
    MODEL_LIST = "model_list"
    MINIMAL_COMPLETION = "minimal_completion"
    NON_EMPTY_COMPLETION = "non_empty_completion"
    GPU_MEMORY = "gpu_memory"
    FATAL_LOG = "fatal_log"
