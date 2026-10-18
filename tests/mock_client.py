"""A small MCP client for tests: starts the server as a real subprocess and talks newline-delimited JSON-RPC over its pipes.

This is the only client the server has been exercised with. It is not a real MCP client such as Claude Desktop.
"""
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class MockClient:
    def __init__(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "webhookreplay", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(ROOT), env=env)
        self._lines = queue.Queue()
        self.stdout_lines = []
        self._next_id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            self._lines.put(line)
        self._lines.put(None)

    def send_raw(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def send(self, message: dict) -> None:
        self.send_raw(json.dumps(message))

    def recv(self, timeout=15):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("no response from server") from exc
        return None if line is None else json.loads(line)

    def request(self, method, params=None):
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.recv()
        assert response["id"] == self._next_id
        return response

    def initialize(self):
        response = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "mock-client", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call(self, name, arguments=None):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return response["result"] if "result" in response else response

    def close(self):
        """Close stdin (EOF) and wait for a clean exit. Returns (exit_code, stderr_text)."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        code = self.proc.wait(timeout=15)
        return code, self.proc.stderr.read()
