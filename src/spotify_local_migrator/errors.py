"""User-facing errors deliberately omit HTTP bodies, URLs and tokens."""


class MigratorError(Exception):
    """An actionable failure safe to display in normal CLI output."""


class ConfigurationError(MigratorError):
    pass


class AuthenticationError(MigratorError):
    pass


class SpotifyAPIError(MigratorError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(SpotifyAPIError):
    def __init__(
        self, message: str, retry_after: float | None = None, *, reason: str | None = None
    ):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after
        self.reason = reason


class PlaylistChangedError(MigratorError):
    pass


class StateError(MigratorError):
    pass
