"""Shared pytest fixtures for the FoxESS EV Charger test suite.

The integration lives under `custom_components/foxess_charger/` (standard HACS
layout with `content_in_root: false`), matching the `custom_components.foxess_charger.*`
import path expected by Home Assistant and the test suite.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

# Put the repo root on sys.path so `import custom_components.foxess_charger`
# resolves regardless of where pytest is invoked from.
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


@pytest.fixture(autouse=True)
def _real_sockets_for_modbus_loopback_tests(request):
    """Allow real localhost sockets in the two modbus loopback test modules only."""
    # The HA test plugin blocks sockets per test; these modules need real TCP.
    if request.module.__name__.rsplit(".", 1)[-1] not in (
        "test_modbus_slow_replies",
        "test_modbus_client_outcomes",
        "test_modbus_message_interval",
    ):
        yield
        return
    import socket

    import pytest_socket

    blocked = socket.socket
    pytest_socket.enable_socket()
    yield
    socket.socket = blocked


@pytest.fixture(autouse=True)
def _stop_unstopped_patches():
    """Stop any unittest.mock patches started with .start() during a test."""
    yield
    patch.stopall()
