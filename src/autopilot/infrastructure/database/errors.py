"""Typed persistence boundary failures without database implementation details."""


class PersistenceError(RuntimeError):
    """Base class for deterministic database adapter failures."""


class JobNotFoundError(PersistenceError):
    """The requested Job does not exist."""


class IdempotencyConflictError(PersistenceError):
    """An idempotency key was reused for different immutable input."""


class LeaseConflictError(PersistenceError):
    """A Job lease is missing, expired, or owned by another Worker."""


class PlanBindingError(PersistenceError):
    """A Job references a missing, cross-Experiment, or incompatible Plan."""


class ArtifactBindingError(PersistenceError):
    """A Job result does not match persisted Artifact metadata in its Experiment."""


class JobStateConflictError(PersistenceError):
    """A requested persistence operation is incompatible with the current Job state."""


class ApprovalNotFoundError(PersistenceError):
    """The requested Approval does not exist."""


class ApprovalBindingError(PersistenceError):
    """An Approval does not match the immutable L2 Plan material."""


class ApprovalStateConflictError(PersistenceError):
    """An Approval or its Plan is not in the required lifecycle state."""


class ApprovalActorConflictError(PersistenceError):
    """An Approval decision violates human actor separation."""


class AuthorizationBindingError(PersistenceError):
    """Persisted Tool Set or Job authorization evidence has conflicting material."""
