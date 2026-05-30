from anirss_lib.ansi import C_OFF, C_RED, ansi_strip, right_anchor, truncate_ansi


def test_ansi_strip_removes_color():
    assert ansi_strip(f"{C_RED}hi{C_OFF}") == "hi"


def test_right_anchor_pads_visible_width():
    out = right_anchor("a", "b", 10)
    assert ansi_strip(out) == "a" + " " * 8 + "b"


def test_truncate_ansi_keeps_visible_chars():
    out = truncate_ansi("hello world", 5)
    # 4 visible chars + dim ellipsis (1 visible char in the ANSI form)
    assert ansi_strip(out) == "hell…"


def test_truncate_ansi_noop_when_fits():
    assert truncate_ansi("hi", 10) == "hi"
