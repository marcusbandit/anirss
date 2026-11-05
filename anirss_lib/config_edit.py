"""Setting a few string keys in config.toml without touching the rest of the
file: comments, ordering and every other key stay as the user left them.
Each edit is checked by parsing the new text; a mismatch refuses the write."""

import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from anirss_lib.errors import ConfigError

_ANY_HEADER = re.compile(r"^\s*\[")
_CONTENT = re.compile(r"^\s*[^\s#]")


def _header(section: str) -> re.Pattern:
    return re.compile(r"^\s*\[\s*" + re.escape(section) + r"\s*\]\s*(#.*)?$")


def set_values(path: Path, section: str, values: dict[str, str]) -> None:
    """Sets `values` (string keys to string values) in the `[section]` table
    of the file at `path`: a key already there is rewritten on its own line,
    a new one goes after the table's last key, and a missing table is
    appended. The previous file is kept as config.toml.bak."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    head = _header(section)
    start = next((i for i, line in enumerate(lines) if head.match(line)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"[{section}]\n")
        start = len(lines) - 1
    end = start + 1
    last = start
    while end < len(lines) and not _ANY_HEADER.match(lines[end]):
        if _CONTENT.match(lines[end]):
            last = end
        end += 1
    for key, value in values.items():
        line = f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
        key_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
        at = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
        if at is not None:
            lines[at] = line
        else:
            lines.insert(last + 1, line)
            last += 1
            end += 1
    new_text = "".join(lines)
    try:
        got = tomllib.loads(new_text).get(section, {})
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"refusing to write config.toml: the edit does not parse ({e})") from e
    if any(got.get(k) != v for k, v in values.items()):
        raise ConfigError("refusing to write config.toml: the edit did not produce "
                          "the intended values")
    _atomic_write(path, new_text)


def _atomic_write(path: Path, text: str) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    # A config.toml that is a symlink (dotfiles) keeps being one.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
