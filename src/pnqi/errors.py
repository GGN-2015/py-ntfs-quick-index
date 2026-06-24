from __future__ import annotations


class PnqiError(Exception):
    """Base class for user-facing pnqi failures."""


class PlatformNotSupportedError(PnqiError):
    """Raised when the current OS or CPU architecture is unsupported."""


class NotAdminError(PnqiError):
    """Raised when administrator privileges are required but unavailable."""


class NotNtfsError(PnqiError):
    """Raised when the selected volume is not NTFS."""


class IndexNotFoundError(PnqiError):
    """Raised when an operation needs an index that does not exist."""


class IndexInvalidError(PnqiError):
    """Raised when an index file is missing required metadata or schema."""


class OperationCancelled(PnqiError):
    """Raised when the user cancels a long-running operation."""

