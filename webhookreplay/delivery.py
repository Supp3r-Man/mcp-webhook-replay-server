"""Delivery to an in-process mock sink: bounded retries, deterministic backoff, idempotency, dead letters.

Nothing here touches the network or sleeps. The backoff schedule is computed and reported, because a
real deployment would wait that long and a test should not.
"""
MAX_ATTEMPTS = 3
MAX_REQUEUES = 3
BACKOFF_CAP_SECONDS = 30


def backoff_seconds(failed_attempt: int) -> int:
    """Delay after failed attempt number n (1-based): 1, 2, 4, ... capped."""
    return min(BACKOFF_CAP_SECONDS, 2 ** (failed_attempt - 1))


class SinkError(Exception):
    pass


class MockSink:
    """Fails the first `synthetic_sink_failures` attempts for an event, counted across replays and requeues."""

    def __init__(self):
        self.attempts = {}
        self.delivered = []

    def deliver(self, event: dict) -> None:
        count = self.attempts.get(event["id"], 0) + 1
        self.attempts[event["id"]] = count
        if count <= event.get("synthetic_sink_failures", 0):
            raise SinkError(f"mock sink answered 503 (attempt {count})")
        self.delivered.append(event["id"])


class DeliveryRefused(Exception):
    pass


class DeliveryService:
    def __init__(self, sink: MockSink):
        self.sink = sink
        self.ledger = {}          # (event_id, idempotency_key) -> stored result
        self.delivered = set()    # event ids already delivered successfully
        self.dead_letters = {}    # id -> record
        self._dead_letter_seq = 0

    def open_dead_letter_for(self, event_id: str):
        for record in self.dead_letters.values():
            if record["event_id"] == event_id and record["status"] == "open":
                return record
        return None

    def _attempt_round(self, event: dict) -> dict:
        schedule = []
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.sink.deliver(event)
                return {"ok": True, "attempts": attempt, "backoff_schedule": schedule, "last_error": None}
            except SinkError as exc:
                last_error = str(exc)
                if attempt < MAX_ATTEMPTS:
                    schedule.append(backoff_seconds(attempt))
        return {"ok": False, "attempts": MAX_ATTEMPTS, "backoff_schedule": schedule, "last_error": last_error}

    def replay(self, event: dict, idempotency_key: str) -> dict:
        key = (event["id"], idempotency_key)
        if key in self.ledger:
            return dict(self.ledger[key], idempotent_replay=True)

        if event["id"] in self.delivered:
            result = {"outcome": "already_delivered", "attempts": 0, "backoff_schedule": []}
        elif self.open_dead_letter_for(event["id"]) is not None:
            raise DeliveryRefused("event has an open dead letter; use requeue_dead_letter")
        else:
            round_ = self._attempt_round(event)
            if round_["ok"]:
                self.delivered.add(event["id"])
                result = {"outcome": "delivered", "attempts": round_["attempts"],
                          "backoff_schedule": round_["backoff_schedule"]}
            else:
                self._dead_letter_seq += 1
                dead_letter_id = f"dl_{self._dead_letter_seq:04d}"
                self.dead_letters[dead_letter_id] = {
                    "id": dead_letter_id, "event_id": event["id"], "status": "open",
                    "attempts": round_["attempts"], "requeue_count": 0,
                    "last_error": round_["last_error"],
                }
                result = {"outcome": "dead_lettered", "dead_letter_id": dead_letter_id,
                          "attempts": round_["attempts"], "backoff_schedule": round_["backoff_schedule"],
                          "last_error": round_["last_error"]}
        self.ledger[key] = result
        return dict(result, idempotent_replay=False)

    def requeue(self, dead_letter_id: str, event: dict) -> dict:
        record = self.dead_letters.get(dead_letter_id)
        if record is None:
            raise DeliveryRefused(f"unknown dead letter: {dead_letter_id}")
        if record["status"] != "open":
            raise DeliveryRefused(f"dead letter is {record['status']}, not open")
        record["requeue_count"] += 1
        round_ = self._attempt_round(event)
        if round_["ok"]:
            record["status"] = "resolved"
            self.delivered.add(event["id"])
        else:
            record["attempts"] += round_["attempts"]
            record["last_error"] = round_["last_error"]
            if record["requeue_count"] >= MAX_REQUEUES:
                record["status"] = "abandoned"
        return {"outcome": "delivered" if round_["ok"] else "failed_again",
                "dead_letter_status": record["status"], "requeue_count": record["requeue_count"],
                "attempts_this_round": round_["attempts"], "backoff_schedule": round_["backoff_schedule"],
                "last_error": round_["last_error"]}
