"""Writing [[endpoint]] blocks into config.toml without touching the rest of
the file: comments, ordering and every other section stay as the user left
them. Silent core logic; the facade and the HTTP API call it.

A block runs from its `[[endpoint]]` header to its last key line before the
next table header, so the comments and blank lines between blocks are never
part of one and survive a removal. Every edit is checked by parsing the new
text and comparing the endpoint list with the one intended; a mismatch
refuses the write and leaves the file alone."""

import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from anirss_lib.endpoints import load_endpoints
from anirss_lib.errors import ConfigError, NotFoundError, ValidationError

# Only the keys a caller over the API may set. `start`, `stop` and `ready`
# are shell commands and a probe URL: a token holder must not be able to
# make anirss run a command, so on-demand endpoints stay hand-written.
WRITABLE_KEYS = ("name", "kind", "url", "category", "filter")

_ENDPOINT_HEADER = re.compile(r"^\s*\[\[\s*endpoint\s*\]\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_CONTENT = re.compile(r"^\s*[^\s#]")


def check_spec(spec: dict) -> dict:
    """The endpoint as it will be written: only WRITABLE_KEYS, strings
    trimmed, checked by the same rules a config file gets. Raises
    ValidationError."""
    if not isinstance(spec, dict):
        raise ValidationError("endpoint must be an object")
    extra = sorted(set(spec) - set(WRITABLE_KEYS))
    if extra:
        raise ValidationError(f"endpoint keys not allowed here: {', '.join(extra)}")
    clean: dict = {}
    for key in WRITABLE_KEYS:
        value = spec.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValidationError(f"endpoint `{key}` must be a string")
        # Control characters and lone surrogates cannot go into a TOML
        # string as written below, nor into a URL.
        if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
               for c in value):
            raise ValidationError(f"endpoint `{key}` holds a control character")
        value = value.strip()
        if value:
            clean[key] = value
    url = clean.get("url", "")
    if url and not url.startswith(("http://", "https://")):
        raise ValidationError("endpoint url must start with http:// or https://")
    if clean.get("kind") == "rss":
        clean.pop("category", None)
        clean.pop("filter", None)
    try:
        load_endpoints({"endpoint": [clean]})
    except ConfigError as e:
        raise ValidationError(str(e)) from e
    return clean


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line index of every [[endpoint]] block, last inclusive."""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if not _ENDPOINT_HEADER.match(lines[i]):
            i += 1
            continue
        start = end = i
        j = i + 1
        while j < len(lines) and not _ANY_HEADER.match(lines[j]):
            if _CONTENT.match(lines[j]):
                end = j
            j += 1
        out.append((start, end))
        i = j
    return out


def _block(lines: list[str], span: tuple[int, int]) -> dict:
    text = "".join(lines[span[0]:span[1] + 1])
    try:
        return dict(tomllib.loads(text)["endpoint"][0])
    except (tomllib.TOMLDecodeError, KeyError, IndexError):
        return {}


def _render(spec: dict) -> list[str]:
    # JSON string escapes are all valid TOML basic-string escapes.
    out = ["[[endpoint]]\n"]
    for key in WRITABLE_KEYS:
        if key in spec:
            out.append(f"{key} = {json.dumps(spec[key], ensure_ascii=False)}\n")
    return out


def _endpoints_of(text: str) -> list[dict]:
    try:
        return list(tomllib.loads(text).get("endpoint") or [])
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml does not parse: {e}") from e


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write(path: Path, new_text: str, expected: list[dict]) -> None:
    got = _endpoints_of(new_text)
    if [{k: e.get(k) for k in WRITABLE_KEYS} for e in got] != \
            [{k: e.get(k) for k in WRITABLE_KEYS} for e in expected]:
        raise ConfigError("refusing to write config.toml: the edit did not "
                          "produce the intended endpoint list")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    # A config.toml that is a symlink (dotfiles) keeps being one: the write
    # lands on the file it points at.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_NYAA_URL = re.compile(r"^\s*nyaa_url\s*=")


def _retire_legacy_url(lines: list[str]) -> None:
    """Comments out [search] nyaa_url once the endpoint it stood for is
    written as a block, so removing that block later really removes it."""
    in_search = False
    for i, line in enumerate(lines):
        if _ANY_HEADER.match(line):
            in_search = line.strip().split("#")[0].strip() == "[search]"
        elif in_search and _NYAA_URL.match(line):
            lines[i] = "# moved to [[endpoint]]: " + line


def _load(path: Path, implicit: list[dict] | None
          ) -> tuple[list[str], list[dict], list[tuple[int, int]]]:
    """The file's lines, its endpoints and their blocks. A file with no
    [[endpoint]] block whose config still yields endpoints (`implicit`: the
    one a legacy [search] section stands for) gets them written out as
    blocks first, so the edit starts from the list anirss is really using."""
    lines = _read(path).splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    current = _endpoints_of("".join(lines))
    if not current and implicit:
        _retire_legacy_url(lines)
        for spec in implicit:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend(_render(spec))
        current = _endpoints_of("".join(lines))
    blocks = _blocks(lines)
    if len(blocks) != len(current):
        raise ConfigError("config.toml lists endpoints in a form anirss cannot "
                          "edit safely; change it by hand")
    return lines, current, blocks


def add_endpoint(path: Path, spec: dict, implicit: list[dict] | None = None) -> bool:
    """Writes `spec` (already through check_spec) into the file at `path`.
    An endpoint with the same name is replaced in place, comments inside its
    block included; a new one goes after the last [[endpoint]] block, or at
    the end of the file when there is none. One with keys the API cannot
    write (an on-demand endpoint's commands, its own paths) is refused rather
    than stripped. True when it replaced one."""
    lines, current, blocks = _load(path, implicit)
    found = [_block(lines, b) for b in blocks]
    names = [b.get("name") for b in found]
    block = _render(spec)
    if spec["name"] in names:
        i = names.index(spec["name"])
        extra = sorted(set(found[i]) - set(WRITABLE_KEYS))
        if extra:
            raise ValidationError(
                f"endpoint {spec['name']!r} sets {', '.join(extra)}, which only "
                f"config.toml can hold; edit it there")
        start, end = blocks[i]
        lines[start:end + 1] = block
        expected = [spec if e.get("name") == spec["name"] else e for e in current]
        replaced = True
    else:
        if blocks:
            at = blocks[-1][1] + 1
            lines[at:at] = ["\n", *block]
        else:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend(block)
        expected = [*current, spec]
        replaced = False
    _write(path, "".join(lines), expected)
    return replaced


def remove_endpoint(path: Path, name: str, implicit: list[dict] | None = None) -> None:
    """Takes the named [[endpoint]] block out of the file at `path`.
    Raises NotFoundError when no endpoint has that name."""
    lines, current, blocks = _load(path, implicit)
    names = [_block(lines, b).get("name") for b in blocks]
    if name not in names:
        raise NotFoundError(f"unknown endpoint {name!r}")
    start, end = blocks[names.index(name)]
    del lines[start:end + 1]
    # The blank line that separated it from what came before goes too.
    if start > 0 and not lines[start - 1].strip() and \
            (start == len(lines) or not lines[start].strip()):
        del lines[start - 1]
    expected = [e for e in current if e.get("name") != name]
    _write(path, "".join(lines), expected)
