"""Shared fixtures. Every test runs against an isolated temp ARTEMIS_HOME so the
operator's real config is never touched and tests never collide."""

from __future__ import annotations

import os

import pytest


@pytest.fixture()
def artemis_home(tmp_path, monkeypatch):
    home = tmp_path / "artemis-home"
    monkeypatch.setenv("ARTEMIS_HOME", str(home))
    monkeypatch.delenv("ARTEMIS_PROFILE", raising=False)
    return home


@pytest.fixture()
def profile(artemis_home):
    from artemis.profiles import ProfileManager
    pm = ProfileManager()
    return pm.create("default", "Artemis")
