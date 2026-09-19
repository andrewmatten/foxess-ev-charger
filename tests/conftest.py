"""Shared pytest fixtures for the FoxESS EV Charger test suite.

This repo ships with `content_in_root: true` in hacs.json (see README) - the
integration's own files (__init__.py, const.py, etc.) live directly at the
repo root rather than under a custom_components/<domain>/ folder, which is
what HA's test tooling (and the `custom_components.foxess_charger.*` import
path used throughout this test suite) expects to find. `custom_components/
foxess_charger` at the repo root is a symlink back to `..` (the repo root
itself) that satisfies that expectation for tests only - it changes nothing
about how the integration is packaged or deployed.
"""
from __future__ import annotations

import os
import sys

import pytest

# Put the repo root on sys.path so `import custom_components.foxess_charger`
# resolves via the symlink above, regardless of where pytest is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# `enable_custom_integrations` is provided by
# pytest-homeassistant-custom-component. Its docs show it wired up as a
# blanket `autouse` fixture, but that forces every test in the suite to pull
# in the (async) `hass` fixture, including this repo's plain pure-function
# tests (energy_guard, modbus_client) that have nothing to do with hass at
# all - doing that broke those tests outright (pytest errors on a sync test
# depending on an async fixture). So it's deliberately NOT autoused here:
# tests that actually exercise hass's component loader (e.g. setting up a
# config entry) should request `enable_custom_integrations` explicitly as a
# fixture argument, same as any other opt-in fixture.
