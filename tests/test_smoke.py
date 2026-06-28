import re


def test_version_importable():
    import anirss_lib
    # Don't pin the exact version (it changes every release); just assert it's
    # importable and shaped like a semver string.
    assert re.fullmatch(r"\d+\.\d+\.\d+", anirss_lib.__version__)
