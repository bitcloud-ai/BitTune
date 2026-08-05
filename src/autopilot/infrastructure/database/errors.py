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
