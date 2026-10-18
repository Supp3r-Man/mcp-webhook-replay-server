"""MCP over stdio, implemented directly: newline-delimited JSON-RPC 2.0.

Handles initialize, notifications/initialized, ping, tools/list and tools/call. Stdout carries protocol
messages only (one JSON object per line); anything meant for a human goes to stderr.
"""
import json
import secrets
import time

from . import __version__, schema
from .audit import AuditLog
from .delivery import DeliveryService, MockSink
from .store import EventStore, generate_events
from .tools import TOOL_DEFS, ToolError, WebhookTools

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_NAME = "webhook-replay"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
NOT_INITIALIZED = -32002

INSTRUCTIONS = ("Inspect and replay synthetic webhook events. Everything is fake data and replays go to an in-process "
                "mock sink. replay_event and requeue_dead_letter default to dry_run; real writes need the server "
                "to be started with --allow-writes.")


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class McpServer:
    def __init__(self, tools: WebhookTools):
        self.tools = tools
        self.initialized = False
        self._schemas = {t["name"]: t["inputSchema"] for t in TOOL_DEFS}

    def handle(self, message):
        """Return the response object for one decoded message, or None when no response is due."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            request_id = message.get("id") if isinstance(message, dict) else None
            return _error(request_id, INVALID_REQUEST, "invalid request: expected a JSON-RPC 2.0 object with a method")
        if "id" not in message:
            return None  # a notification never gets a response, known or not
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            return _error(None, INVALID_REQUEST, "invalid request: id must be a string or integer")
        method = message["method"]
        params = message.get("params", {})

        if method == "ping":
            return _result(request_id, {})
        if method == "initialize":
            return self._initialize(request_id, params)
        if method in ("tools/list", "tools/call") and not self.initialized:
            return _error(request_id, NOT_INITIALIZED, "server not initialized: send initialize first")
        if method == "tools/list":
            return _result(request_id, {"tools": TOOL_DEFS})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, request_id, params):
        if (not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict)):
            return _error(request_id, INVALID_PARAMS, "initialize needs protocolVersion, capabilities and clientInfo")
        requested = params["protocolVersion"]
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        self.initialized = True
        return _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        })

    def _call_tool(self, request_id, params):
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, INVALID_PARAMS, "tools/call needs a tool name")
        name = params["name"]
        arguments = params.get("arguments", {})
        if name not in self._schemas:
            return _error(request_id, INVALID_PARAMS, f"unknown tool: {name}")
        problems = schema.validate(arguments, self._schemas[name])
        if problems:
            return _error(request_id, INVALID_PARAMS, "invalid arguments: " + "; ".join(problems))
        try:
            payload, is_error = self.tools.call(name, arguments), False
        except ToolError as exc:
            payload, is_error = {"error": str(exc)}, True
        return _result(request_id, {
            "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "structuredContent": payload,
            "isError": is_error,
        })


def build_server(allow_writes=False, audit_file="webhook_audit.jsonl", count=40, tolerance=300,
                 fixed_now=None, secret=None) -> McpServer:
    """Assemble a server. The secret is generated per process unless a test passes one in; it is never printed or stored."""
    now_fn = (lambda: fixed_now) if fixed_now is not None else (lambda: int(time.time()))
    secret = secret or secrets.token_hex(16)
    store = EventStore(generate_events(secret, now_fn(), count))
    service = DeliveryService(MockSink())
    tools = WebhookTools(secret, now_fn, store, service, AuditLog(audit_file, now_fn), allow_writes, tolerance)
    return McpServer(tools)


def serve(server: McpServer, stdin, stdout) -> None:
    """Read one JSON-RPC message per line until EOF. A bad line gets an error response and the loop carries on."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, PARSE_ERROR, "parse error: line is not valid JSON")
        else:
            response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
