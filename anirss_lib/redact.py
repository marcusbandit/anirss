"""Masking secrets in URLs before they are shown or logged. No imports from
the rest of anirss, so the fetch layer can use it too."""

import urllib.parse

_SECRET_PARAMS = ("token", "apikey", "api_key", "key", "passkey", "auth", "secret",
                  "pass", "password", "pwd", "rsskey", "uid", "sid", "session")


def redact_url(url: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """`url` with secret-looking query values (token=, apikey=, …) masked,
    and every value in `secrets` (as it appears in the URL) masked wherever
    it sits, path included. For showing endpoint config and for logs."""
    for value in secrets:
        if value:
            url = url.replace(value, "***")
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"***@{host}" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    if not parts.query:
        return urllib.parse.urlunsplit(parts)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in _SECRET_PARAMS and v else v) for k, v in pairs]
    query = urllib.parse.urlencode(masked, safe="{}*")
    return urllib.parse.urlunsplit(parts._replace(query=query))
