"""Shared test setup: no test ever reads or writes the real endpoint-repo
state or the onboarding marker in the user's home."""

import pytest

from anirss_lib import repos


@pytest.fixture(autouse=True)
def _private_repo_state(tmp_path, monkeypatch):
    monkeypatch.setattr(repos, "STATE_PATH", tmp_path / "repos-state.json")
    from anirss_lib.cli import setup
    monkeypatch.setattr(setup, "ONBOARDED", tmp_path / "onboarded")


@pytest.fixture(autouse=True)
def _private_played_state(tmp_path, monkeypatch):
    from anirss_lib import library
    monkeypatch.setattr(library, "PLAYED_PATH", tmp_path / "played.json")
