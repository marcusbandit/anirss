import pytest

from anirss_lib.cli import args as a


def test_parse_subscribe_with_query():
    p = a.parse_cli_args(["--subscribe", "Frieren"])
    assert p.subscribe
    assert p.positional == ["Frieren"]
    assert p.non_interactive


def test_parse_download_n():
    p = a.parse_cli_args(["--download", "5", "Frieren"])
    assert p.download_n == 5
    assert p.positional == ["Frieren"]


def test_parse_download_equals_form():
    p = a.parse_cli_args(["--download=3", "Frieren"])
    assert p.download_n == 3


def test_parse_name():
    p = a.parse_cli_args(["--subscribe", "--name", "Frieren-Anime", "Frieren"])
    assert p.name == "Frieren-Anime"
    assert p.positional == ["Frieren"]


def test_parse_name_equals_form():
    p = a.parse_cli_args(["--name=Frieren", "--subscribe"])
    assert p.name == "Frieren"


def test_parse_password_stdin():
    p = a.parse_cli_args(["--password-stdin", "--subscribe", "Frieren"])
    assert p.password_stdin


def test_parse_rejects_two_action_flags(monkeypatch):
    captured = {}
    def fake_die(msg):
        captured["msg"] = msg
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_cli_args(["--subscribe", "--movie", "Frieren"])
    assert "at most one" in captured["msg"]


def test_parse_rejects_non_integer_n(monkeypatch):
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_cli_args(["--download", "x", "Frieren"])


def test_parse_rejects_unknown_long_flag(monkeypatch):
    """Unknown --flag args must error, not silently become search terms.
    Real motivation: `anirss --download 2 --help` was eaten as a search query
    `--help` and tried to download whatever nyaa returned. Now it errors.
    """
    captured = {}
    def fake_die(msg):
        captured["msg"] = msg
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_cli_args(["--download", "2", "--help"])
    assert "--help" in captured["msg"]


def test_parse_allows_nyaa_exclude_term_with_single_dash():
    """Search syntax `-term` (single dash) excludes a token — must stay
    in positional, not be confused with a flag."""
    p = a.parse_cli_args(["Frieren", "-dub"])
    assert p.positional == ["Frieren", "-dub"]


def test_parse_no_action_flags_means_interactive():
    p = a.parse_cli_args(["Frieren"])
    assert not p.non_interactive
    assert p.positional == ["Frieren"]
    assert p.action_flag_count == 0


def test_parse_op_flag_bare():
    assert a.parse_op_flag("-Q") == ("Q", set())
    assert a.parse_op_flag("-S") == ("S", set())
    assert a.parse_op_flag("-R") == ("R", set())
    assert a.parse_op_flag("-T") == ("T", set())


def test_parse_op_flag_with_modifiers():
    assert a.parse_op_flag("-Qj") == ("Q", {"j"})
    assert a.parse_op_flag("-Sy") == ("S", {"y"})
    assert a.parse_op_flag("-Rns") == ("R", {"n", "s"})


def test_parse_op_flag_accepts_hidden_modifier():
    """`h` is valid on -S, -T, and -R (anything that touches the save tree)."""
    assert a.parse_op_flag("-Sh") == ("S", {"h"})
    assert a.parse_op_flag("-Th") == ("T", {"h"})
    assert a.parse_op_flag("-Rh") == ("R", {"h"})
    assert a.parse_op_flag("-Rnsh") == ("R", {"n", "s", "h"})


def test_parse_op_flag_rejects_h_on_query(monkeypatch):
    """-Q only lists feeds — h has no meaning, so -Qh must error."""
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_op_flag("-Qh")


def test_parse_op_flag_rejects_modifiers_on_T(monkeypatch):
    """-T takes no modifiers — `-Tx` should error, not silently accept."""
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_op_flag("-Tx")


def test_parse_op_flag_ignores_long_and_unknown():
    assert a.parse_op_flag("--version") is None
    assert a.parse_op_flag("-h") is None
    assert a.parse_op_flag("frieren") is None
    assert a.parse_op_flag("") is None
    assert a.parse_op_flag("-") is None


def test_endpoint_flag_short():
    out = a.parse_cli_args(["-e", "anirena", "some", "query"])
    assert out.endpoint == "anirena"
    assert out.positional == ["some", "query"]


def test_endpoint_flag_long_and_equals():
    assert a.parse_cli_args(["--endpoint", "nyaa"]).endpoint == "nyaa"
    assert a.parse_cli_args(["--endpoint=nyaa"]).endpoint == "nyaa"


def test_endpoint_flag_missing_value_dies(monkeypatch):
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(a, "die", fake_die)
    with pytest.raises(SystemExit):
        a.parse_cli_args(["-e"])
