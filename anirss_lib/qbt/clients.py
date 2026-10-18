"""Several qBittorrents: the [[qbittorrent.client]] blocks, each resolved to the
QbtConfig a login takes, and picked by name."""

import copy
import re
from typing import cast

from anirss_lib.config import AnirssConfig, QbtConfig
from anirss_lib.errors import ConfigError, NotFoundError

# The name of the one client a config without [[qbittorrent.client]] blocks
# has. Its session and saved password keep the file names from before there
# were several, so an existing install stays logged in.
DEFAULT_CLIENT = "default"


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
        # The name ends up in the session and password file names.
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: invalid name "
                              f"(use only letters, digits, '.', '_', '-')")
        url = str(raw.get("url") or "").strip()
        if not url:
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: missing `url`")
        tags = raw.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise ConfigError(f"[[qbittorrent.client]] {name!r}: `tags` must be a list")
        merged = {**copy.deepcopy(shared), **copy.deepcopy(raw), "name": name, "url": url}
        out.append(cast(QbtConfig, merged))
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
