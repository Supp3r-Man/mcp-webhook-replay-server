"""MCP / JSON-RPC conformance of the server, driven in-process (see test_stdio_subprocess.py for the real pipes)."""
import io
import json

import pytest

from webhookreplay.server import SUPPORTED_VERSIONS, serve
from webhookreplay.tools import TOOL_DEFS

from conftest import handshake, rpc

INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}


def test_initialize_returns_capabilities_server_info_and_echoes_a_supported_version(make):
    server = make()
    result = rpc(server, "initialize", dict(INIT, protocolVersion="2024-11-05"))["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "webhook-replay" and "version" in result["serverInfo"]


def test_an_unsupported_protocol_version_is_answered_with_the_latest_supported(make):
    assert rpc(make(), "initialize", dict(INIT, protocolVersion="1999-01-01"))["result"]["protocolVersion"] == SUPPORTED_VERSIONS[0]


@pytest.mark.parametrize("params", [None, {}, {"protocolVersion": "2025-06-18"}, {"protocolVersion": 5, "capabilities": {}, "clientInfo": {}}, "x"])
def test_initialize_with_bad_params_is_invalid_params(make, params):
    assert rpc(make(), "initialize", params)["error"]["code"] == -32602


def test_tools_need_initialize_first_but_ping_does_not(make):
    server = make()
    assert rpc(server, "tools/list")["error"]["code"] == -32002
    assert rpc(server, "tools/call", {"name": "list_events"})["error"]["code"] == -32002
    assert rpc(server, "ping")["result"] == {}


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
    {"jsonrpc": "2.0", "method": "no/such/notification"},
    {"jsonrpc": "2.0", "method": "tools/list"},
])
def test_notifications_get_no_response_even_when_unknown(make, message):
    assert make().handle(message) is None


def test_unknown_method_is_method_not_found_and_echoes_the_id(make):
    server = handshake(make())
    response = rpc(server, "resources/list", request_id="abc-1")
    assert response["error"]["code"] == -32601 and response["id"] == "abc-1" and response["jsonrpc"] == "2.0"


@pytest.mark.parametrize("request_id", [0, 7, "s-1"])
def test_ids_are_echoed_exactly(make, request_id):
    assert rpc(make(), "ping", request_id=request_id)["id"] == request_id


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "id": [1], "method": "ping"}, {"jsonrpc": "2.0", "id": True, "method": "ping"},
    {"jsonrpc": "1.0", "id": 1, "method": "ping"}, {"id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1},
    {"jsonrpc": "2.0", "id": 1, "method": 5}, [], 42, "ping", None,
])
def test_malformed_requests_are_invalid_request(make, message):
    assert make().handle(message)["error"]["code"] == -32600


def run_lines(server, lines):
    out = io.StringIO()
    serve(server, io.StringIO("\n".join(lines) + "\n"), out)
    return [json.loads(l) for l in out.getvalue().splitlines()]


def test_malformed_json_is_a_parse_error_and_the_loop_carries_on(make):
    responses = run_lines(make(), ['{"jsonrpc": "2.0", "id": 1, "method": "ping"', "", "not json at all",
                                   '{"jsonrpc":"2.0","id":2,"method":"ping"}'])
    assert [(r["id"], r.get("error", {}).get("code")) for r in responses] == [(None, -32700), (None, -32700), (2, None)]


def test_a_batch_array_is_rejected_as_invalid_request(make):
    responses = run_lines(make(), ['[{"jsonrpc":"2.0","id":1,"method":"ping"}]'])
    assert responses[0]["error"]["code"] == -32600


def test_one_response_per_request_and_none_for_notifications(make):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/whatever"})]
    assert [r["id"] for r in run_lines(make(), lines)] == [1, 2]


# ---- tools/list ----

ALLOWED_TYPES = {"string", "integer", "boolean", "object"}


def valid_object_schema(schema):
    """A strict structural check of what this server promises: a closed object schema with typed properties."""
    assert schema["type"] == "object" and isinstance(schema["properties"], dict)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])
    for name, spec in schema["properties"].items():
        assert spec["type"] in ALLOWED_TYPES, name
        if "enum" in spec:
            assert isinstance(spec["enum"], list) and spec["enum"]
        if "minimum" in spec and "maximum" in spec:
            assert spec["minimum"] <= spec["maximum"]
        if "minLength" in spec and "maxLength" in spec:
            assert spec["minLength"] <= spec["maxLength"]


def test_tools_list_has_the_six_tools_with_valid_schemas(make):
    tools = rpc(handshake(make()), "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_events", "get_event", "verify_signature", "replay_event",
                                          "list_dead_letters", "requeue_dead_letter"]
    for tool in tools:
        assert tool["description"] and isinstance(tool["annotations"]["readOnlyHint"], bool)
        valid_object_schema(tool["inputSchema"])
    read_only = {t["name"] for t in tools if t["annotations"]["readOnlyHint"]}
    assert read_only == {"list_events", "get_event", "verify_signature", "list_dead_letters"}
    json.dumps(TOOL_DEFS)   # serialisable


# ---- tools/call ----

def call(server, name, arguments=None, **extra):
    params = {"name": name, **extra}
    if arguments is not None:
        params["arguments"] = arguments
    return rpc(server, "tools/call", params)


def test_tool_result_shape(make):
    result = call(handshake(make()), "get_event", {"event_id": "evt_0001"})["result"]
    assert result["isError"] is False and result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["id"] == "evt_0001"


def test_a_failing_tool_is_a_result_with_is_error_not_a_protocol_error(make):
    response = call(handshake(make()), "get_event", {"event_id": "evt_9999"})
    assert "error" not in response and response["result"]["isError"] is True
    assert "unknown event" in response["result"]["structuredContent"]["error"]


def test_calls_without_arguments_work_when_none_are_required(make):
    assert call(handshake(make()), "list_events")["result"]["structuredContent"]["total"] == 40


@pytest.mark.parametrize("name,arguments", [
    ("get_event", {}),                                          # missing required
    ("get_event", {"event_id": 5}),                             # wrong type
    ("get_event", {"event_id": "evt_0001", "extra": 1}),        # additional property
    ("list_events", {"status": "nonsense"}),                    # enum
    ("list_events", {"limit": 0}), ("list_events", {"limit": 51}),   # range
    ("list_events", {"limit": True}),                           # bool is not an integer
    ("list_events", {"cursor": "x" * 13}),                      # too long
    ("replay_event", {"event_id": "evt_0001", "idempotency_key": "short"}),
    ("replay_event", {"event_id": "evt_0001", "dry_run": "yes"}),
    ("verify_signature", {"event_id": "evt_0001", "tolerance_seconds": 0}),
    ("list_events", "not an object"),
])
def test_invalid_arguments_are_invalid_params(make, name, arguments):
    assert call(handshake(make()), name, arguments)["error"]["code"] == -32602


@pytest.mark.parametrize("params", [{}, {"name": 5}, {"name": "no_such_tool"}])
def test_bad_tool_names_are_invalid_params(make, params):
    assert rpc(handshake(make()), "tools/call", params)["error"]["code"] == -32602
