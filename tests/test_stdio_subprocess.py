"""End-to-end over real pipes: the server runs as a subprocess and mock_client.py speaks JSON-RPC to it."""
import json
import re

import pytest

from mock_client import MockClient

PINNED = ["--fixed-now", "1800000000"]


@pytest.fixture
def start(tmp_path):
    clients = []

    def _start(*extra):
        client = MockClient("--audit-file", str(tmp_path / "audit.jsonl"), *PINNED, *extra)
        clients.append(client)
        return client

    yield _start
    for client in clients:
        if client.proc.poll() is None:
            client.proc.kill()


def structured(result):
    return result["structuredContent"]


def test_handshake_and_tool_list_over_stdio(start):
    client = start()
    init = client.initialize()
    assert init["result"]["serverInfo"]["name"] == "webhook-replay"
    assert len(client.request("tools/list")["result"]["tools"]) == 6
    assert client.close()[0] == 0


def test_paginating_every_event_through_the_client(start):
    client = start()
    client.initialize()
    seen, cursor = [], None
    while True:
        page = structured(client.call("list_events", {"limit": 7, **({"cursor": cursor} if cursor else {})}))
        seen += [e["id"] for e in page["events"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 40 and len(set(seen)) == 40
    client.close()


def test_read_only_mode_allows_dry_runs_and_blocks_real_replays(start):
    client = start()
    client.initialize()
    assert structured(client.call("replay_event", {"event_id": "evt_0001"}))["would_deliver"] is True
    blocked = client.call("replay_event", {"event_id": "evt_0001", "dry_run": False, "idempotency_key": "key-00000001"})
    assert blocked["isError"] is True and "writes are disabled" in blocked["structuredContent"]["error"]
    client.close()


def test_with_allow_writes_a_replay_delivers_once_and_is_audited(start, tmp_path):
    client = start("--allow-writes")
    client.initialize()
    args = {"event_id": "evt_0001", "dry_run": False, "idempotency_key": "key-00000001"}
    assert structured(client.call("replay_event", args))["outcome"] == "delivered"
    assert structured(client.call("replay_event", args))["idempotent_replay"] is True
    code, _ = client.close()
    assert code == 0
    events = [json.loads(l)["event"] for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events.count("write_intent") == 2 and events.count("tool_call") == 2


def test_stdout_is_protocol_only_and_a_bad_line_does_not_kill_the_server(start):
    client = start()
    client.initialize()
    client.send_raw("this is not json")
    assert client.recv()["error"]["code"] == -32700
    assert client.request("ping")["result"] == {}
    code, stderr = client.close()
    assert code == 0
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in client.stdout_lines)
    assert "ready" in stderr and not re.search(r"[0-9a-f]{32}", stderr)   # status on stderr, no secret


def test_notifications_over_the_pipe_produce_no_output(start):
    client = start()
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    client.send({"jsonrpc": "2.0", "method": "notifications/unknown"})
    response = client.request("ping")      # the first line back must be the ping's, not a reply to a notification
    assert response["result"] == {}
    client.close()
