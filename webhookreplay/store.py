"""Synthetic webhook events. Fictional companies and ids only: no card, bank, health or personal data."""
import json

from . import signing

TYPES = ["order.created", "order.shipped", "invoice.paid", "invoice.overdue",
         "customer.updated", "subscription.renewed"]
PREFIX = {"order": "ord", "invoice": "inv", "customer": "cus", "subscription": "sub"}
COMPANIES = ["Acme Widgets", "Globex Tools", "Initech Supplies", "Hooli Cloud", "Soylent Foods"]


def body_of(event: dict) -> str:
    """Canonical body the signature covers."""
    return json.dumps(event["payload"], sort_keys=True, separators=(",", ":"))


def generate_events(secret: str, now: int, count: int = 40) -> list:
    """Deterministic for a given (secret, now, count). Rules by 1-based position n:

    n % 7 == 0   payload changed after signing (bad signature)
    n % 5 == 0   signed correctly but an hour or more old (stale)
    n % 11 == 0  mock sink fails 5 times (exhausts retries, becomes a dead letter, succeeds on requeue)
    n % 13 == 0  mock sink fails once (succeeds on the second attempt)
    """
    events = []
    for i in range(count):
        n = i + 1
        etype = TYPES[i % len(TYPES)]
        payload = {
            "object_id": f"{PREFIX[etype.split('.')[0]]}_{1000 + n}",
            "amount_cents": 1000 + (n * 137) % 9000,
            "currency": "USD",
            "customer_id": f"cust_{(n * 7) % 25 + 1:03d}",
            "customer_name": COMPANIES[i % len(COMPANIES)],
        }
        stale = n % 5 == 0
        timestamp = now - (3600 + n * 60) if stale else now - (5 + (n * 17) % 110)
        signature = signing.sign(secret, timestamp, json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if n % 7 == 0:
            payload = dict(payload, amount_cents=payload["amount_cents"] + 1)
        failures = 5 if n % 11 == 0 else 1 if n % 13 == 0 else 0
        events.append({
            "id": f"evt_{n:04d}",
            "type": etype,
            "timestamp": timestamp,
            "payload": payload,
            "signature": signature,
            "synthetic_sink_failures": failures,
        })
    return events


class EventStore:
    def __init__(self, events: list):
        self.events = list(events)
        self._by_id = {e["id"]: e for e in self.events}

    def get(self, event_id: str):
        return self._by_id.get(event_id)
