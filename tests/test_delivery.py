import time

import pytest

from webhookreplay.delivery import (MAX_ATTEMPTS, MAX_REQUEUES, DeliveryRefused, DeliveryService, MockSink,
                                    backoff_seconds)


def test_delivers_on_first_attempt(service, events):
    result = service.replay(events["evt_0001"], "key-00000001")
    assert (result["outcome"], result["attempts"], result["backoff_schedule"]) == ("delivered", 1, [])
    assert service.sink.delivered == ["evt_0001"]


def test_retries_then_succeeds_with_backoff(service, events):
    result = service.replay(events["evt_0013"], "key-00000001")   # sink fails once
    assert (result["outcome"], result["attempts"], result["backoff_schedule"]) == ("delivered", 2, [1])


def test_exhausted_retries_become_a_dead_letter(service, events):
    result = service.replay(events["evt_0011"], "key-00000001")   # sink fails 5 times
    assert result["outcome"] == "dead_lettered" and result["attempts"] == MAX_ATTEMPTS
    assert result["backoff_schedule"] == [1, 2]
    record = service.dead_letters[result["dead_letter_id"]]
    assert record["status"] == "open" and record["event_id"] == "evt_0011"
    assert service.sink.attempts["evt_0011"] == MAX_ATTEMPTS     # bounded: never more than 3 tries per round
    assert service.sink.delivered == []


def test_backoff_doubles_and_is_capped():
    assert [backoff_seconds(n) for n in range(1, 8)] == [1, 2, 4, 8, 16, 30, 30]


def test_same_idempotency_key_returns_the_first_result_without_delivering_again(service, events):
    first = service.replay(events["evt_0001"], "key-00000001")
    second = service.replay(events["evt_0001"], "key-00000001")
    assert first["idempotent_replay"] is False and second["idempotent_replay"] is True
    assert second["outcome"] == "delivered"
    assert service.sink.delivered == ["evt_0001"]
    assert service.sink.attempts["evt_0001"] == 1


def test_a_new_key_never_delivers_an_already_delivered_event_again(service, events):
    service.replay(events["evt_0001"], "key-00000001")
    again = service.replay(events["evt_0001"], "key-00000002")
    assert again["outcome"] == "already_delivered"
    assert service.sink.delivered == ["evt_0001"]


def test_replay_of_an_event_with_an_open_dead_letter_is_refused(service, events):
    service.replay(events["evt_0011"], "key-00000001")
    with pytest.raises(DeliveryRefused):
        service.replay(events["evt_0011"], "key-00000002")


def test_same_key_after_a_dead_letter_returns_the_stored_result(service, events):
    first = service.replay(events["evt_0011"], "key-00000001")
    assert service.replay(events["evt_0011"], "key-00000001")["dead_letter_id"] == first["dead_letter_id"]
    assert service.sink.attempts["evt_0011"] == MAX_ATTEMPTS


def test_requeue_resolves_once_the_sink_recovers(service, events):
    dead_letter_id = service.replay(events["evt_0011"], "key-00000001")["dead_letter_id"]
    result = service.requeue(dead_letter_id, events["evt_0011"])   # attempts 4 and 5 fail, 6 succeeds
    assert result["outcome"] == "delivered" and result["attempts_this_round"] == 3
    assert result["dead_letter_status"] == "resolved"
    assert service.replay(events["evt_0011"], "key-00000009")["outcome"] == "already_delivered"


def test_requeue_is_bounded_then_abandoned(service, events):
    stuck = dict(events["evt_0001"], id="evt_stuck", synthetic_sink_failures=100)
    dead_letter_id = service.replay(stuck, "key-00000001")["dead_letter_id"]
    statuses = [service.requeue(dead_letter_id, stuck)["dead_letter_status"] for _ in range(MAX_REQUEUES)]
    assert statuses == ["open"] * (MAX_REQUEUES - 1) + ["abandoned"]
    with pytest.raises(DeliveryRefused):
        service.requeue(dead_letter_id, stuck)
    assert service.sink.attempts["evt_stuck"] == MAX_ATTEMPTS * (MAX_REQUEUES + 1)


def test_requeue_refuses_unknown_and_resolved_dead_letters(service, events):
    with pytest.raises(DeliveryRefused):
        service.requeue("dl_9999", events["evt_0011"])
    dead_letter_id = service.replay(events["evt_0011"], "key-00000001")["dead_letter_id"]
    service.requeue(dead_letter_id, events["evt_0011"])
    with pytest.raises(DeliveryRefused):
        service.requeue(dead_letter_id, events["evt_0011"])


def test_nothing_sleeps_and_nothing_touches_the_network(monkeypatch, events):
    def boom(*_a, **_k):
        raise AssertionError("must not sleep or open sockets")
    monkeypatch.setattr(time, "sleep", boom)
    import socket
    monkeypatch.setattr(socket.socket, "connect", boom)
    service = DeliveryService(MockSink())
    dead_letter_id = service.replay(events["evt_0011"], "key-00000001")["dead_letter_id"]
    service.requeue(dead_letter_id, events["evt_0011"])
