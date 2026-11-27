"""C6: make_client() socket patch does not leak into subsequent tests, and
test_persistence skips cleanly when git or commit 4490e4a is unavailable."""
from __future__ import annotations

import socket
import subprocess

import pytest

from custom_components.foxess_charger import modbus_client as mc

import test_persistence
from test_modbus_client import SLAVE_ID, build_frame, fc03_response_pdu, make_client

_REAL_CREATE_CONNECTION = socket.create_connection


def test_step1_make_client_patches_socket_during_test():
    frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=fc03_response_pdu([7]))
    client, _ = make_client([frame])
    assert mc.socket.create_connection is not _REAL_CREATE_CONNECTION
    assert client.read_registers(0x1000, 1) == [7]


def test_step2_create_connection_is_restored_for_following_test():
    assert mc.socket.create_connection is _REAL_CREATE_CONNECTION


def test_persistence_243_helper_skips_when_repo_has_no_git_history(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(test_persistence, "REPO", str(tmp_path))
    with pytest.raises(pytest.skip.Exception):
        test_persistence._import_release_243(tmp_path)


def test_persistence_243_helper_skips_when_git_binary_missing(
    tmp_path, monkeypatch
):
    def _no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    with pytest.raises(pytest.skip.Exception):
        test_persistence._import_release_243(tmp_path)
