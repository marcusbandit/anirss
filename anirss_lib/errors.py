"""Exception hierarchy for library and server callers.

Library modules raise these instead of exiting; the CLI turns any that
reach main() into the familiar red `error:` line + exit 1 (logging.die)."""


class AnirssError(Exception):
    """Base for every error anirss raises on purpose."""


class ConfigError(AnirssError):
    """The config (or an [[endpoint]] block) is invalid."""


class NoEndpointsError(ConfigError):
    """No [[endpoint]] is configured, so there is nothing to search. anirss
    ships with none; the user adds their own."""


class ValidationError(AnirssError):
    """A caller-supplied value was rejected (bad name, bad URL, ...)."""


class NotFoundError(AnirssError):
    """A named thing (endpoint, subscription, file) doesn't exist."""


class FetchError(AnirssError):
    """A search endpoint couldn't be fetched or parsed."""


class QbtError(AnirssError):
    """qBittorrent was unreachable, refused the login, or rejected a call."""


class QbtAuthError(QbtError):
    """No usable qBittorrent credentials (or they were rejected).

    `rejected` is True only when qBittorrent actually answered a login
    attempt with a refusal, the case where retrying risks its IP ban."""

    def __init__(self, msg: str = "", *, rejected: bool = False) -> None:
        super().__init__(msg)
        self.rejected = rejected


class RepoError(AnirssError):
    """An endpoint repo's file can't be trusted or read. `code` says why:
    "format" (not a repo, or malformed), "version" (a newer format than
    this anirss reads), "signature" (does not match its contents),
    "key_changed" (signed by another key than the one pinned) or "stale"
    (older than the index already accepted)."""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(msg)
        self.code = code
