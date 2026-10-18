"""Several qBittorrents: the [[qbittorrent.client]] blocks, each resolved to the
QbtConfig a login takes, and picked by name."""

import copy
import os
import re
import urllib.parse
from typing import cast

from anirss_lib.config import AnirssConfig, QbtConfig
from anirss_lib.errors import ConfigError, NotFoundError

# The name of the one client a config without [[qbittorrent.client]] blocks
# has. Its session and saved password keep the file names from before there
# were several, so an existing install stays logged in.
DEFAULT_CLIENT = "default"

# The name ends up in the session and password file names.
_NAME = re.compile(r"[A-Za-z0-9._-]+")

# The download roots a block may set for itself. They are paths on the
# machine that client runs on, which for a seedbox is not this one.
PATH_KEYS = ("save_base", "movie_path", "hidden_base")


def is_local_url(base_url: str) -> bool:
    """True when base_url points at this machine."""
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def local_files(client: QbtConfig) -> bool:
    """Whether the client's downloads are on this machine's disk, so anirss
    may size and delete them itself. `local_files` in the block says so
    (a NAS mounted at the same path, say); otherwise a client at a local
    address has them here and any other does not."""
    value = client.get("local_files")
    if isinstance(value, bool):
        return value
    return is_local_url(client["url"])


def download_root(cfg: AnirssConfig, client: QbtConfig, key: str) -> str:
    """The client's own save_base / movie_path / hidden_base, else the
    [downloads] one."""
    return str(client.get(key) or cfg["downloads"][key])


def _settle_paths(client: QbtConfig, label: str) -> None:
    """Check a client's local_files and own download roots. A local client's
    `~` is expanded here; another's is refused, since nothing on that
    machine would expand it."""
    value = client.get("local_files")
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{label}: `local_files` must be true or false")
    here = local_files(client)
    for key in PATH_KEYS:
        if key not in client:
            continue
        path = client[key]
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"{label}: `{key}` must be a path")
        path = path.strip()
        if path.startswith("~"):
            if not here:
                raise ConfigError(f"{label}: `{key}` is a path on that client's own "
                                  f"machine; give it in full, `~` is not expanded there")
            path = os.path.expanduser(path)
        client[key] = path


def qbt_clients(cfg: AnirssConfig) -> list[QbtConfig]:
    """Every configured qBittorrent in priority order (first = default): the
    [qbittorrent] keys with each block's own laid over them. Raises
    ConfigError for an invalid block."""
    shared = {k: v for k, v in cfg["qbittorrent"].items() if k != "client"}
    blocks = cfg["qbittorrent"].get("client") or []
    if not isinstance(blocks, list):
        raise ConfigError("[qbittorrent] `client` must be [[qbittorrent.client]] blocks")
    if not blocks:
        # A client qbt_client() already resolved carries its own name.
        name = str(shared.get("name") or DEFAULT_CLIENT)
        if not _NAME.fullmatch(name):
            raise ConfigError(f"[qbittorrent] name {name!r}: invalid name "
                              f"(use only letters, digits, '.', '_', '-')")
        return [cast(QbtConfig, {**copy.deepcopy(shared), "name": name})]
    out: list[QbtConfig] = []
    seen: set[str] = set()
    for i, raw in enumerate(blocks, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"[[qbittorrent.client]] #{i}: not a table")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ConfigError(f"[[qbittorrent.client]] #{i}: missing `name`")
        if name in seen:
            raise ConfigError(f"[[qbittorrent.client]] #{i}: duplicate name {name!r}")
        seen.add(name)
        if not _NAME.fullmatch(name):
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: invalid name "
                              f"(use only letters, digits, '.', '_', '-')")
        if name.startswith("env-"):
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: names starting "
                              f"with 'env-' are kept for ANIRSS_QBT_URL")
        url = str(raw.get("url") or "").strip()
        if not url:
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: missing `url`")
        tags = raw.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: `tags` must be a list")
        merged = cast(QbtConfig, {**copy.deepcopy(shared), **copy.deepcopy(raw),
                                  "name": name, "url": url})
        _settle_paths(merged, f"[[qbittorrent.client]] {name!r}")
        out.append(merged)
    return out


def qbt_client(cfg: AnirssConfig, name: str | None = None) -> QbtConfig:
    """The client called `name`, or the default one when `name` is empty.
    Raises NotFoundError for a name no block has."""
    clients = qbt_clients(cfg)
    if not name:
        return clients[0]
    for client in clients:
        if client["name"] == name:
            return client
    names = ", ".join(c["name"] for c in clients)
    raise NotFoundError(f"unknown qBittorrent client {name!r} (configured: {names})")
