import json

import pytest

from webhookreplay.tools import ToolError

from conftest import SECRET


def call(server, name, **args):
    return server.tools.call(name, args)


def audit_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---- read tools ----

def test_list_events_defaults_and_pagination_walk(make):
    server = make()
    first = call(server, "list_events")
    assert len(first["events"]) == 10 and first["total"] == 40 and first["next_cursor"] == "10"
    seen, cursor = [], None
    while True:
        page = call(server, "list_events", limit=15, **({"cursor": cursor} if cursor else {}))
        seen += [e["id"] for e in page["events"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 40 and len(set(seen)) == 40


@pytest.mark.parametrize("status,count", [("valid", 28), ("bad_signature", 5), ("stale", 7), ("all", 40)])
def test_list_events_status_filter(make, status, count):
    assert call(make(), "list_events", status=status, limit=50)["total"] == count


def test_list_events_type_filter_and_cursor_edges(make):
    server = make()
    assert call(server, "list_events", type="invoice.paid")["total"] == 7
    assert call(server, "list_events", cursor="999") == {"events": [], "total": 40, "next_cursor": None}
    with pytest.raises(ToolError):
        call(server, "list_events", cursor="abc")


def test_get_event_and_verify_signature(make):
    server = make()
    event = call(server, "get_event", event_id="evt_0001")
    assert event["payload"]["currency"] == "USD" and event["signature_status"] == "ok" and event["delivered"] is False
    assert call(server, "verify_signature", event_id="evt_0001")["ok"] is True
    assert call(server, "verify_signature", event_id="evt_0007")["reason"] == "bad_signature"
    assert call(server, "verify_signature", event_id="evt_0005")["reason"] == "stale"
    assert call(server, "verify_signature", event_id="evt_0005", tolerance_seconds=86400)["ok"] is True
    for tool in ("get_event", "verify_signature"):
        with pytest.raises(ToolError):
            call(server, tool, event_id="evt_9999")


# ---- replay: dry run, gating, idempotency ----

def test_dry_run_is_the_default_and_changes_nothing(make):
    server = make()   # writes disabled: dry runs must still work
    plan = call(server, "replay_event", event_id="evt_0001")
    assert plan["dry_run"] is True and plan["would_deliver"] is True
    refused = call(server, "replay_event", event_id="evt_0007")
    assert refused["would_deliver"] is False and "bad_signature" in refused["reasons"][0]
    assert server.tools.service.sink.delivered == [] and server.tools.service.ledger == {}


def test_real_replay_needs_allow_writes_and_a_key(make):
    with pytest.raises(ToolError, match="writes are disabled"):
        call(make(), "replay_event", event_id="evt_0001", dry_run=False, idempotency_key="key-00000001")
    with pytest.raises(ToolError, match="idempotency_key is required"):
        call(make(allow_writes=True), "replay_event", event_id="evt_0001", dry_run=False)


@pytest.mark.parametrize("event_id,reason", [("evt_0007", "bad_signature"), ("evt_0005", "stale")])
def test_unverified_events_are_refused_unless_overridden(make, event_id, reason):
    server = make(allow_writes=True)
    with pytest.raises(ToolError, match=reason):
        call(server, "replay_event", event_id=event_id, dry_run=False, idempotency_key="key-00000001")
    assert server.tools.service.sink.delivered == []
    done = call(server, "replay_event", event_id=event_id, dry_run=False, idempotency_key="key-00000001",
                override_verification=True)
    assert done["outcome"] == "delivered" and done["override_used"] is True


def test_real_replay_is_idempotent_and_never_double_delivers(make):
    server = make(allow_writes=True)
    args = dict(event_id="evt_0001", dry_run=False, idempotency_key="key-00000001")
    first = call(server, "replay_event", **args)
    assert first["outcome"] == "delivered" and first["override_used"] is False
    assert call(server, "replay_event", **args)["idempotent_replay"] is True
    assert call(server, "replay_event", **dict(args, idempotency_key="key-00000002"))["outcome"] == "already_delivered"
    assert server.tools.service.sink.delivered == ["evt_0001"]
    assert call(server, "replay_event", event_id="evt_0001")["would_deliver"] is False   # dry run now says so
    assert call(server, "get_event", event_id="evt_0001")["delivered"] is True


def test_dead_letter_flow_through_the_tools(make):
    server = make(allow_writes=True)
    result = call(server, "replay_event", event_id="evt_0011", dry_run=False, idempotency_key="key-00000001")
    assert result["outcome"] == "dead_lettered"
    listed = call(server, "list_dead_letters")
    assert listed["total"] == 1 and listed["dead_letters"][0]["event_id"] == "evt_0011"
    plan = call(server, "requeue_dead_letter", dead_letter_id=result["dead_letter_id"])   # dry_run default
    assert plan["dry_run"] is True and plan["would_requeue"] is True and plan["requeues_remaining"] == 3
    assert call(server, "list_dead_letters")["total"] == 1
    done = call(server, "requeue_dead_letter", dead_letter_id=result["dead_letter_id"], dry_run=False)
    assert done["outcome"] == "delivered"
    assert call(server, "list_dead_letters")["total"] == 0
    assert call(server, "list_dead_letters", status="resolved")["total"] == 1


def test_requeue_of_an_unknown_dead_letter_is_an_error(make):
    server = make(allow_writes=True)
    for dry_run in (True, False):
        with pytest.raises(ToolError, match="unknown dead letter"):
            call(server, "requeue_dead_letter", dead_letter_id="dl_9999", dry_run=dry_run)


def test_real_requeue_without_allow_writes_is_refused(make):
    writer = make(allow_writes=True)
    dead_letter_id = call(writer, "replay_event", event_id="evt_0011", dry_run=False,
                          idempotency_key="key-00000001")["dead_letter_id"]
    reader = make()   # same data, writes off
    reader.tools.service = writer.tools.service
    with pytest.raises(ToolError, match="writes are disabled"):
        call(reader, "requeue_dead_letter", dead_letter_id=dead_letter_id, dry_run=False)


# ---- audit log ----

def test_every_call_is_logged_with_increasing_sequence_numbers(make, audit_path):
    server = make()
    call(server, "list_events")
    call(server, "get_event", event_id="evt_0001")
    with pytest.raises(ToolError):
        call(server, "get_event", event_id="evt_9999")
    lines = audit_lines(audit_path)
    assert [l["tool"] for l in lines] == ["list_events", "get_event", "get_event"]
    assert [l["seq"] for l in lines] == [1, 2, 3]
    assert [l["is_error"] for l in lines] == [False, False, True]
    assert lines[2]["detail"] == "unknown event: evt_9999"


def test_write_intent_is_logged_before_the_sink_is_touched(make, audit_path):
    server = make(allow_writes=True)
    seen_at_delivery = []
    real = server.tools.service.sink.deliver
    server.tools.service.sink.deliver = lambda e: (seen_at_delivery.append([l["event"] for l in audit_lines(audit_path)]), real(e))
    call(server, "replay_event", event_id="evt_0001", dry_run=False, idempotency_key="key-00000001")
    assert seen_at_delivery == [["write_intent"]]
    assert [l["event"] for l in audit_lines(audit_path)] == ["write_intent", "tool_call"]


def test_an_override_is_recorded_in_the_audit_log(make, audit_path):
    server = make(allow_writes=True)
    call(server, "replay_event", event_id="evt_0007", dry_run=False, idempotency_key="key-00000001",
         override_verification=True)
    intent = [l for l in audit_lines(audit_path) if l["event"] == "write_intent"][0]
    assert intent["override_used"] is True and intent["verification"] == "bad_signature"


def test_a_broken_audit_log_blocks_writes_but_not_reads(tmp_path, capsys):
    from webhookreplay.server import build_server
    server = build_server(allow_writes=True, audit_file=str(tmp_path), fixed_now=1_800_000_000, secret=SECRET)  # a directory
    with pytest.raises(ToolError, match="audit log unavailable"):
        call(server, "replay_event", event_id="evt_0001", dry_run=False, idempotency_key="key-00000001")
    assert server.tools.service.sink.delivered == [] and server.tools.service.ledger == {}
    assert call(server, "get_event", event_id="evt_0001")["id"] == "evt_0001"
    assert "warning" in capsys.readouterr().err


def test_the_signing_secret_never_reaches_the_audit_log(make, audit_path):
    server = make(allow_writes=True)
    call(server, "verify_signature", event_id="evt_0001")
    call(server, "replay_event", event_id="evt_0001", dry_run=False, idempotency_key="key-00000001")
    assert SECRET not in audit_path.read_text(encoding="utf-8")
