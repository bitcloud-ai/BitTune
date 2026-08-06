"""Typed runner failures that never expose provider stack traces or secrets."""


class RunnerServiceError(RuntimeError):
    """Base class for a classified runner failure."""

    code = "RUNNER_FAILURE"
    retryable = False


class RunnerValidationError(RunnerServiceError):
    code = "RUNNER_VALIDATION_ERROR"


class PathBoundaryError(RunnerValidationError):
    code = "PATH_BOUNDARY_VIOLATION"


class ImageNotAllowedError(RunnerValidationError):
    code = "IMAGE_NOT_ALLOWED"


class IdempotencyConflictError(RunnerValidationError):
    code = "IDEMPOTENCY_CONFLICT"


class RunnerRequestInProgressError(RunnerServiceError):
    code = "RUNNER_REQUEST_IN_PROGRESS"
    retryable = True


class GpuLeaseBusyError(RunnerServiceError):
    code = "GPU_LEASE_BUSY"
    retryable = True


class GpuLeaseNotFoundError(RunnerServiceError):
    code = "GPU_LEASE_NOT_FOUND"


class GpuLeaseExpiredError(RunnerServiceError):
    code = "GPU_LEASE_EXPIRED"
    retryable = True


class StaleFencingTokenError(RunnerServiceError):
    code = "STALE_FENCING_TOKEN"


class ResourceNotFoundError(RunnerServiceError):
    code = "RESOURCE_NOT_FOUND"


class DockerOperationError(RunnerServiceError):
    code = "DOCKER_OPERATION_FAILED"
    retryable = True


class VllmHealthProbeUnavailableError(RunnerServiceError):
    """The fixed production health probe has not been configured or verified."""

    code = "VLLM_HEALTH_PROBE_UNAVAILABLE"


class VllmHealthCheckError(RunnerServiceError):
    """A typed vLLM startup-health invariant failed."""

    code = "VLLM_HEALTH_CHECK_FAILED"


class CleanupError(RunnerServiceError):
    code = "CLEANUP_FAILED"
    retryable = True


class RunnerProtocolError(RunnerServiceError):
    code = "RUNNER_PROTOCOL_ERROR"


class RunnerConfigurationError(RunnerServiceError):
    code = "RUNNER_CONFIGURATION_ERROR"
