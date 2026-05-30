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
    kind: str  # "tokens" | "done" | "exclude" | "show_all" | "custom" | "back"
    tokens: list[str]


PICK_DONE = Pick("done", [])
PICK_EXCLUDE = Pick("exclude", [])
PICK_SHOW_ALL = Pick("show_all", [])
PICK_BACK = Pick("back", [])
