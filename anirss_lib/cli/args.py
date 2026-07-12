"""Argument parsing: pacman-style op flags + non-interactive action flags."""

from dataclasses import dataclass, field

from anirss_lib.logging import die


@dataclass
class ParsedArgs:
    """Structured result of parse_cli_args. Meta flags (--help, --version,
    --config, …) are handled separately in main._handle_meta_flags before
    this runs.
    """
    positional: list[str] = field(default_factory=list)
    noconfirm: bool = False
    name: str | None = None
    endpoint: str | None = None
    password_stdin: bool = False

    # action flags (mutually exclusive)
    subscribe: bool = False
    download_all: bool = False
    download_n: int | None = None
    movie: bool = False

    # Set by _handle_op_flag when `-T <path>` is parsed. main() then drops
    # the -T and its path from `positional` so the rest of the flow treats
    # the upload as the chosen action.
    local_torrent_path: str | None = None

    # Set by _handle_op_flag when the `h` modifier is bundled (e.g. -Sh, -Th).
    # Causes save_base to be replaced with [downloads].hidden_base.
    hidden: bool = False

    @property
    def action_flag_count(self) -> int:
        return sum([
            self.subscribe, self.download_all,
            self.download_n is not None, self.movie,
        ])

    @property
    def non_interactive(self) -> bool:
        return self.action_flag_count > 0


def parse_op_flag(arg: str) -> tuple[str, set[str]] | None:
    """Recognize pacman-style bundled op flags: -Q, -Qj, -S, -Sy, -R, -Rs,
    -Rn, -Rns, -T.

    Returns (op_letter, set_of_modifier_letters) or None if `arg` is not an op flag.
    Validates that exactly one of {Q, S, R, T} is the op and the rest are valid
    modifiers for that op. Raises die() on a malformed bundle (e.g. -Rz, -QR, -Tx).
    """
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
        return None
    body = arg[1:]
    if not body or not body[0].isalpha():
        return None
    op = body[0]
    if op not in ("Q", "S", "R", "T"):
        return None
    mods = set(body[1:])
    # 'h' = route to [downloads].hidden_base instead of save_base. Valid on
    # any flag that picks where new content lands: -S (subscribe), -T (upload),
    # -R (remove from hidden tree). Not on -Q (listing) which doesn't write.
    allowed = {
        "Q": {"j"},
        "S": {"y", "h"},
        "R": {"n", "s", "h"},
        "T": {"h"},
    }
    bad = mods - allowed[op]
    if bad:
        die(f"unknown modifier(s) for -{op}: {''.join(sorted(bad))!r} "
            f"(valid: {''.join(sorted(allowed[op])) or '<none>'})")
    return op, mods


def parse_cli_args(argv: list[str]) -> ParsedArgs:
    """Pull out long-form flags (--subscribe, --download-all, --download N,
    --movie, --name X, -e/--endpoint NAME, --password-stdin, --noconfirm).
    Leaves op flags (-Q/-S/-R*), URLs, and free-form queries in `positional`.

    Errors (via die) on bad N for --download or two competing action flags.
    """
    out = ParsedArgs()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--noconfirm":
            out.noconfirm = True
        elif a == "--subscribe":
            out.subscribe = True
        elif a == "--download-all":
            out.download_all = True
        elif a == "--movie":
            out.movie = True
        elif a == "--password-stdin":
            out.password_stdin = True
        elif a == "--name":
            i += 1
            if i >= len(argv):
                die("--name requires a value")
            out.name = argv[i]
        elif a.startswith("--name="):
            out.name = a.split("=", 1)[1]
        elif a == "--download":
            i += 1
            if i >= len(argv):
                die("--download requires N")
            try:
                out.download_n = int(argv[i])
            except ValueError:
                die(f"--download expects an integer, got {argv[i]!r}")
            if out.download_n < 1:
                die(f"--download N must be >= 1, got {out.download_n}")
        elif a.startswith("--download="):
            try:
                out.download_n = int(a.split("=", 1)[1])
            except ValueError:
                die(f"--download expects an integer, got {a.split('=', 1)[1]!r}")
            if out.download_n is not None and out.download_n < 1:
                die(f"--download N must be >= 1, got {out.download_n}")
        elif a in ("-e", "--endpoint"):
            i += 1
            if i >= len(argv):
                die("--endpoint requires a name")
            out.endpoint = argv[i]
        elif a.startswith("--endpoint="):
            out.endpoint = a.split("=", 1)[1]
        elif a.startswith("--"):
            # An unrecognized `--flag` would silently become part of the search
            # query, which is almost never what the user meant — and risks
            # downloading random hits matching the flag name. Fail loud.
            die(f"unknown flag: {a}")
        else:
            out.positional.append(a)
        i += 1

    if out.action_flag_count > 1:
        die("at most one of --subscribe, --download-all, --download, --movie")
    return out
