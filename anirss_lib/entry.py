"""Console entry point. Keeps `anirss_lib.main` (and every module it pulls
in) out of the import that runs before a Ctrl-C handler exists, so Ctrl-C
right after launch never prints an import traceback."""


def main() -> None:
    try:
        from anirss_lib.main import main as real_main
    except KeyboardInterrupt:
        die_by_sigint()
        return
    real_main()


def die_by_sigint() -> None:
    """No traceback. Die by SIGINT itself rather than exit(130), so a calling
    shell loop sees the signal and stops too."""
    import os
    import signal
    import sys
    print(file=sys.stderr)
    sys.stderr.flush()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)
    sys.exit(130)  # only reached where SIGINT cannot kill (Windows)
