"""Shared test setup: no test ever reads or writes the real endpoint-repo
state in ~/.local/state/anirss."""

import pytest

from anirss_lib import repos


@pytest.fixture(autouse=True)
def _private_repo_state(tmp_path, monkeypatch):
    monkeypatch.setattr(repos, "STATE_PATH", tmp_path / "repos-state.json")
