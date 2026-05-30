"""input() prompts with per-history-key line editing."""

from pathlib import Path

from anirss_lib.logging import die, log


HISTORY_LIMIT = 1000


def _history_path(key: str) -> Path:
    return Path(f"~/.local/state/anirss/{key}.history").expanduser()


def setup_readline() -> None:
    """Enable line editing on input() prompts: arrows, Ctrl-w, and especially
    Alt-Backspace (Option+Delete on macOS) for backward-kill-word.

    History is loaded/saved per-prompt by `prompt()` itself (each prompt type
    has its own file), so editing a recalled Search query never leaks into
    Name or Exclude history and vice versa.
    """
    try:
        import readline
    except ImportError:
        return
    libedit = "libedit" in (readline.__doc__ or "")
    if libedit:
        # macOS default Python links libedit, which has a different config syntax.
        binds = [r'bind "\e\x7f" ed-delete-prev-word',
                 r'bind "\e\b"   ed-delete-prev-word']
    else:
        binds = [r'"\e\x7f": backward-kill-word',
                 r'"\e\b":   backward-kill-word']
    for b in binds:
        try:
            readline.parse_and_bind(b)
        except Exception:
            pass
    readline.set_history_length(HISTORY_LIMIT)


def prompt(label: str, *, history: str | None = None) -> str:
    """input() with line editing. If `history` is set, Up/Down recall entries
    from `~/.local/state/anirss/<history>.history` only — no cross-pollination
    between Search / Name / Exclude prompts.
    """
    try:
        import readline as rl
    except ImportError:
        rl = None  # type: ignore[assignment]

    history_file = _history_path(history) if history else None
    saved: list[str] = []
    if rl is not None and history_file is not None:
        # Save in-memory history so this prompt's entries don't leak to the next.
        n = rl.get_current_history_length()
        saved = [rl.get_history_item(i + 1) or "" for i in range(n)]
        rl.clear_history()
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            if history_file.exists():
                rl.read_history_file(str(history_file))
        except OSError as e:
            log("WARN", f"history read {history_file}: {e}")

    try:
        result = input(label).strip()
    except (EOFError, KeyboardInterrupt):
        die("cancelled")

    if rl is not None and history_file is not None:
        try:
            rl.write_history_file(str(history_file))
        except OSError as e:
            log("WARN", f"history write {history_file}: {e}")
        rl.clear_history()
        for entry in saved:
            if entry:
                rl.add_history(entry)

    return result


def get_name(default_name: str | None) -> str:
    label = f"Name [{default_name}]: " if default_name else "Name: "
    name = prompt(label, history="name")
    if not name and default_name:
        name = default_name
    if not name:
        die("name cannot be empty")
    if "/" in name or "\\" in name:
        die("name must not contain '/' or '\\'")
    return name
