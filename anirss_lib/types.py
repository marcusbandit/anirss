"""Shared dataclass-like types used across modules."""

from typing import NamedTuple


class Item(NamedTuple):
    title: str
    link: str
    seeders: int = 0
    leechers: int = 0
    downloads: int = 0
    size: str = ""
    category: str = ""


class Group(NamedTuple):
    label: str
    tokens: list[str]
    member_count: int
    has_poster: bool


class Pick(NamedTuple):
    """Result of pick_group: a kind and (for tokens/custom) the relevant terms."""
    kind: str  # tokens | done | exclude | show_all | best_fit | custom | back | endpoint
    tokens: list[str]


PICK_DONE = Pick("done", [])
PICK_EXCLUDE = Pick("exclude", [])
PICK_SHOW_ALL = Pick("show_all", [])
PICK_BEST_FIT = Pick("best_fit", [])
PICK_BACK = Pick("back", [])
PICK_ENDPOINT = Pick("endpoint", [])
