import json
import re

import pytest

from webhookreplay import signing
from webhookreplay.store import body_of, generate_events

from conftest import NOW, SECRET

BODY = '{"a":1}'


def test_sign_is_deterministic_hex_sha256():
    sig = signing.sign(SECRET, 100, BODY)
    assert sig == signing.sign(SECRET, 100, BODY)
    assert re.fullmatch(r"[0-9a-f]{64}", sig)


def test_verify_accepts_a_good_signature():
    sig = signing.sign(SECRET, NOW - 10, BODY)
    assert signing.verify(SECRET, NOW - 10, BODY, sig, NOW) == {"ok": True, "reason": "ok", "age_seconds": 10}


@pytest.mark.parametrize("secret,timestamp,body", [("other-secret", NOW, BODY), (SECRET, NOW + 1, BODY), (SECRET, NOW, BODY + " ")])
def test_verify_rejects_wrong_secret_timestamp_or_body(secret, timestamp, body):
    sig = signing.sign(SECRET, NOW, BODY)
    assert signing.verify(secret, timestamp, body, sig, NOW)["reason"] == "bad_signature"


@pytest.mark.parametrize("bad", [None, 123, ["x"], "é" * 64, ""])
def test_verify_handles_odd_signature_values_without_raising(bad):
    assert signing.verify(SECRET, NOW, BODY, bad, NOW)["reason"] == "bad_signature"


def test_tolerance_boundaries_are_inclusive():
    sig_old = signing.sign(SECRET, NOW - 300, BODY)
    sig_older = signing.sign(SECRET, NOW - 301, BODY)
    sig_future = signing.sign(SECRET, NOW + 300, BODY)
    sig_further = signing.sign(SECRET, NOW + 301, BODY)
    assert signing.verify(SECRET, NOW - 300, BODY, sig_old, NOW, 300)["ok"]
    assert signing.verify(SECRET, NOW - 301, BODY, sig_older, NOW, 300)["reason"] == "stale"
    assert signing.verify(SECRET, NOW + 300, BODY, sig_future, NOW, 300)["ok"]
    assert signing.verify(SECRET, NOW + 301, BODY, sig_further, NOW, 300)["reason"] == "future"


def test_bad_signature_is_reported_even_when_also_stale():
    assert signing.verify(SECRET, NOW - 99999, BODY, "0" * 64, NOW)["reason"] == "bad_signature"


def test_signature_comparison_is_constant_time(monkeypatch):
    calls = []
    real = signing.hmac.compare_digest
    monkeypatch.setattr(signing.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    signing.verify(SECRET, NOW, BODY, signing.sign(SECRET, NOW, BODY), NOW)
    assert len(calls) == 1


# ---- synthetic events ----

def test_events_are_deterministic_and_secret_dependent():
    assert generate_events(SECRET, NOW, 40) == generate_events(SECRET, NOW, 40)
    other = generate_events("different-secret", NOW, 40)
    assert [e["signature"] for e in other] != [e["signature"] for e in generate_events(SECRET, NOW, 40)]


def test_event_ids_are_unique_and_counted(events):
    assert len(events) == 40
    assert list(events)[0] == "evt_0001" and list(events)[-1] == "evt_0040"


def test_events_contain_no_card_ssn_email_or_secret(events):
    text = json.dumps([e["payload"] for e in events.values()])
    assert not re.search(r"\b\d{13,19}\b", text)               # card-number-shaped digit runs
    assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", text)       # SSN shape
    assert "@" not in text
    assert SECRET not in json.dumps(list(events.values()))


def test_bad_signature_events_differ_from_what_was_signed(events):
    tampered = events["evt_0007"]
    assert signing.verify(SECRET, tampered["timestamp"], body_of(tampered), tampered["signature"], NOW)["reason"] == "bad_signature"
    good = events["evt_0001"]
    assert signing.verify(SECRET, good["timestamp"], body_of(good), good["signature"], NOW)["ok"]
