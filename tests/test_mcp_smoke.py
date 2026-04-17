"""Smoke-test the MCP server: spawn it as a subprocess, do a real handshake.

Talks straight JSON-RPC over stdio so we exercise the same wire format
that any MCP client (Claude Desktop, Cursor, Cline, Continue, ...) uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_line_with_timeout(stream, timeout: float) -> bytes:
    """``readline`` with a wall-clock timeout. Returns ``b""`` on timeout."""
    holder: dict[str, bytes] = {}

    def reader() -> None:
        try:
            holder["line"] = stream.readline()
        except Exception:  # noqa: BLE001
            holder["line"] = b""

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return holder.get("line", b"")


def _rpc(proc: subprocess.Popen, method: str, params: dict | None = None, *, msg_id: int | None = 1) -> dict:
    """Send one JSON-RPC message and read the response (or notification result)."""
    body: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    proc.stdin.write((json.dumps(body) + "\n").encode())
    proc.stdin.flush()
    if msg_id is None:  # notification, no response
        return {}
    raw = _read_line_with_timeout(proc.stdout, timeout=10.0)
    assert raw, f"No response to {method}"
    return json.loads(raw)


@pytest.mark.timeout(30)
def test_mcp_server_handshake_and_lists_tools() -> None:
    """Boot the server over stdio, initialize, and list tools."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "organizer.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(_PROJECT_ROOT),
    )
    try:
        init_response = _rpc(
            proc,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
            msg_id=1,
        )
        assert init_response.get("result", {}).get("serverInfo", {}).get("name") == "image-organizer"

        # MCP requires the client to send "initialized" before issuing requests.
        _rpc(proc, "notifications/initialized", {}, msg_id=None)

        tools_response = _rpc(proc, "tools/list", {}, msg_id=2)
        names = {t["name"] for t in tools_response["result"]["tools"]}

        for expected in (
            "organize_folder", "get_status", "stop_job",
            "analyze_image", "list_categories", "read_manifest",
        ):
            assert expected in names, f"missing tool {expected}; got {names}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
