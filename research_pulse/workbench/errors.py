"""Stable cross-layer domain errors used for structured execution outcomes."""


class RevisionConflictError(ValueError):
    """A write was planned against a stale canonical revision and was not applied."""
