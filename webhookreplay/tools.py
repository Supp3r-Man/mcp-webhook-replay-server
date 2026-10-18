"""The six tools. Reads are always available; real deliveries need allow_writes and an audit log that works."""
import sys

from . import signing
from .audit import AuditError
from .delivery import MAX_ATTEMPTS, MAX_REQUEUES, DeliveryRefused, DeliveryService
from .store import body_of


class ToolError(Exception):
    """A tool-level failure: reported to the client as a result with isError true."""


def _object(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


EVENT_ID = {"type": "string", "minLength": 1, "maxLength": 64, "description": "Event id, for example evt_0001."}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 50, "description": "Page size (default 10)."}

TOOL_DEFS = [
    {
        "name": "list_events",
        "description": "List synthetic webhook events with their signature status. Filter by type or status; page with the cursor.",
        "inputSchema": _object({
            "type": {"type": "string", "maxLength": 64, "description": "Only this event type, for example invoice.paid."},
            "status": {"type": "string", "enum": ["all", "valid", "bad_signature", "stale"], "description": "Signature status filter (default all)."},
            "limit": LIMIT,
            "cursor": {"type": "string", "maxLength": 12, "description": "next_cursor from the previous page."},
        }),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_event",
        "description": "Get one event in full: type, timestamp, payload, signature and its current verification result.",
        "inputSchema": _object({"event_id": EVENT_ID}, ["event_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "verify_signature",
        "description": "Verify an event's HMAC-SHA256 signature and timestamp window. Reason is ok, bad_signature, stale or future.",
        "inputSchema": _object({
            "event_id": EVENT_ID,
            "tolerance_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "description": "Allowed clock difference (default 300)."},
        }, ["event_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "replay_event",
        "description": ("Replay an event to the in-process mock sink (never the network). dry_run defaults to true and only reports "
                        "what would happen. A real replay needs the server started with --allow-writes and an idempotency_key; "
                        "events with a bad or stale signature are refused unless override_verification is true (and that is logged)."),
        "inputSchema": _object({
            "event_id": EVENT_ID,
            "dry_run": {"type": "boolean", "description": "Default true: report only."},
            "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 64, "description": "Required for a real replay; the same key returns the first result."},
            "override_verification": {"type": "boolean", "description": "Replay despite a failed signature check. Default false."},
        }, ["event_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "list_dead_letters",
        "description": "List deliveries that failed after bounded retries.",
        "inputSchema": _object({
            "status": {"type": "string", "enum": ["open", "resolved", "abandoned", "all"], "description": "Default open."},
            "limit": LIMIT,
        }),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "requeue_dead_letter",
        "description": ("Try a dead letter again (up to 3 attempts per requeue, at most 3 requeues, then it is abandoned). "
                        "dry_run defaults to true; a real requeue needs --allow-writes."),
        "inputSchema": _object({
            "dead_letter_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "dry_run": {"type": "boolean", "description": "Default true: report only."},
        }, ["dead_letter_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
]


class WebhookTools:
    def __init__(self, secret, now_fn, store, service: DeliveryService, audit, allow_writes=False, tolerance=300):
        self._secret = secret
        self._now = now_fn
        self.store = store
        self.service = service
        self.audit = audit
        self.allow_writes = allow_writes
        self.tolerance = tolerance

    # ---- dispatch -------------------------------------------------------------------------

    def call(self, name: str, arguments: dict) -> dict:
        handler = getattr(self, "_tool_" + name, None)
        if handler is None:
            raise ToolError(f"unknown tool: {name}")
        try:
            result = handler(arguments)
        except ToolError as exc:
            self._log_call(name, arguments, True, str(exc))
            raise
        self._log_call(name, arguments, False, None)
        return result

    def _log_call(self, name, arguments, is_error, detail) -> None:
        try:
            self.audit.append({"event": "tool_call", "tool": name, "arguments": arguments,
                               "is_error": is_error, "detail": detail})
        except AuditError as exc:
            # A failed log must not break reads; real writes already refused before acting (see _write_intent).
            print(f"warning: {exc}", file=sys.stderr)

    def _write_intent(self, name, arguments, extra: dict) -> None:
        """Record the intent before acting. If the log cannot be written, the write does not happen."""
        try:
            self.audit.append(dict({"event": "write_intent", "tool": name, "arguments": arguments}, **extra))
        except AuditError as exc:
            raise ToolError(f"audit log unavailable, refusing to write: {exc}") from exc

    # ---- helpers --------------------------------------------------------------------------

    def _event(self, event_id: str) -> dict:
        event = self.store.get(event_id)
        if event is None:
            raise ToolError(f"unknown event: {event_id}")
        return event

    def _check(self, event: dict, tolerance=None) -> dict:
        return signing.verify(self._secret, event["timestamp"], body_of(event), event["signature"],
                              self._now(), self.tolerance if tolerance is None else tolerance)

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise ToolError("writes are disabled: start the server with --allow-writes (dry_run=true still works)")

    # ---- read tools -----------------------------------------------------------------------

    def _tool_list_events(self, args: dict) -> dict:
        wanted_type = args.get("type")
        wanted_status = args.get("status", "all")
        limit = args.get("limit", 10)
        cursor = args.get("cursor", "0")
        if not cursor.isdigit():
            raise ToolError(f"invalid cursor: {cursor!r}")
        offset = int(cursor)

        rows = []
        for event in self.store.events:
            if wanted_type is not None and event["type"] != wanted_type:
                continue
            status = self._check(event)["reason"]
            if wanted_status != "all" and status != ("ok" if wanted_status == "valid" else wanted_status):
                continue
            rows.append({"id": event["id"], "type": event["type"], "timestamp": event["timestamp"],
                         "signature_status": status})
        page = rows[offset:offset + limit]
        more = offset + limit < len(rows)
        return {"events": page, "total": len(rows), "next_cursor": str(offset + limit) if more else None}

    def _tool_get_event(self, args: dict) -> dict:
        event = self._event(args["event_id"])
        return dict(event, signature_status=self._check(event)["reason"],
                    delivered=event["id"] in self.service.delivered)

    def _tool_verify_signature(self, args: dict) -> dict:
        event = self._event(args["event_id"])
        result = self._check(event, args.get("tolerance_seconds"))
        return dict(result, event_id=event["id"],
                    tolerance_seconds=args.get("tolerance_seconds", self.tolerance))

    def _tool_list_dead_letters(self, args: dict) -> dict:
        wanted = args.get("status", "open")
        limit = args.get("limit", 10)
        rows = [r for r in self.service.dead_letters.values() if wanted == "all" or r["status"] == wanted]
        return {"dead_letters": rows[:limit], "total": len(rows)}

    # ---- write tools ----------------------------------------------------------------------

    def _tool_replay_event(self, args: dict) -> dict:
        event = self._event(args["event_id"])
        dry_run = args.get("dry_run", True)
        override = args.get("override_verification", False)
        check = self._check(event)

        if dry_run:
            reasons = []
            if not check["ok"] and not override:
                reasons.append(f"signature check failed: {check['reason']}")
            if event["id"] in self.service.delivered:
                reasons.append("already delivered")
            if self.service.open_dead_letter_for(event["id"]) is not None:
                reasons.append("open dead letter: use requeue_dead_letter")
            return {"dry_run": True, "event_id": event["id"], "verification": check["reason"],
                    "override_verification": override, "would_deliver": not reasons, "reasons": reasons}

        self._require_writes()
        key = args.get("idempotency_key")
        if key is None:
            raise ToolError("idempotency_key is required when dry_run is false")
        if not check["ok"] and not override:
            raise ToolError(f"refused: signature check failed ({check['reason']}); "
                            "pass override_verification=true to replay anyway")
        override_used = (not check["ok"]) and override
        self._write_intent("replay_event", args, {"verification": check["reason"], "override_used": override_used})
        try:
            result = self.service.replay(event, key)
        except DeliveryRefused as exc:
            raise ToolError(f"refused: {exc}") from exc
        return dict(result, dry_run=False, event_id=event["id"], override_used=override_used)

    def _tool_requeue_dead_letter(self, args: dict) -> dict:
        record = self.service.dead_letters.get(args["dead_letter_id"])
        if record is None:
            raise ToolError(f"unknown dead letter: {args['dead_letter_id']}")
        dry_run = args.get("dry_run", True)

        if dry_run:
            remaining = MAX_REQUEUES - record["requeue_count"]
            return {"dry_run": True, "dead_letter_id": record["id"], "status": record["status"],
                    "would_requeue": record["status"] == "open" and remaining > 0,
                    "requeues_remaining": remaining, "attempts_per_requeue": MAX_ATTEMPTS}

        self._require_writes()
        self._write_intent("requeue_dead_letter", args, {"event_id": record["event_id"]})
        try:
            result = self.service.requeue(record["id"], self._event(record["event_id"]))
        except DeliveryRefused as exc:
            raise ToolError(f"refused: {exc}") from exc
        return dict(result, dry_run=False, dead_letter_id=record["id"])
