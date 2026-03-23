"""Domain errors for storage and lifecycle operations."""

from __future__ import annotations


class ArchivistError(Exception):
    """Base application error."""


class NotFoundError(ArchivistError):
    """Raised when a record does not exist."""


class ConflictError(ArchivistError):
    """Raised when optimistic concurrency checks fail."""


class ConflictWithContextError(ConflictError):
    """Conflict error carrying deterministic contender/base context."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class InvalidLifecycleTransitionError(ArchivistError):
    """Raised when state transitions violate lifecycle rules."""


class ConstraintError(ArchivistError):
    """Raised when a DB integrity/constraint check fails."""
