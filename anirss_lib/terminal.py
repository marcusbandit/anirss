"""Terminal size + alternate-screen lifecycle.

The hidden ``--_search-rss`` reload subprocess that backs the live nyaa
picker inherits piped stdout from fzf, so ``shutil.get_terminal_size()``
falls back to its default. Reading ``/dev/tty`` (the controlling terminal)
sidesteps the problem.

The alt-screen helpers wrap the interactive UI so the original scrollback
is left untouched on exit — no stacked title lists, no leftover fzf
overlay frames.
"""

import contextlib
import os
import shutil
import sys

FALLBACK = os.terminal_size((120, 24))

# DEC private mode 1049: switch to the alternate screen buffer (saving
# the cursor) and back. Supported by xterm, kitty, alacritty, foot,
# wezterm, gnome-terminal, konsole, iTerm2, Apple Terminal, tmux, and
# screen — i.e. anything modern enough to run anirss.
ALT_SCREEN_ENTER = "\x1b[?1049h"
ALT_SCREEN_LEAVE = "\x1b[?1049l"
CLEAR_AND_HOME = "\x1b[2J\x1b[H"


def get_size() -> os.terminal_size:
    try:
        with open("/dev/tty") as tty:
            return os.get_terminal_size(tty.fileno())
    except OSError:
        return shutil.get_terminal_size(FALLBACK)


def is_tty() -> bool:
    """True iff stdout looks like a real terminal we can drive with escapes."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def clear_screen() -> None:
    """Clear the screen and park the cursor at home (no-op if stdout isn't a tty)."""
    if not is_tty():
        return
    sys.stdout.write(CLEAR_AND_HOME)
    sys.stdout.flush()


@contextlib.contextmanager
def alt_screen():
    """Enter the alternate screen buffer for the duration of the block.

    On exit (clean return, exception, KeyboardInterrupt, or sys.exit), the
    original screen is restored before propagation — your shell prompt
    comes back exactly as it was. No-op if stdout isn't a tty, so piped
    output (`anirss -Qj | jq`) is unaffected.
    """
    if not is_tty():
        yield
        return
    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()
    try:
        yield
    finally:
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()
