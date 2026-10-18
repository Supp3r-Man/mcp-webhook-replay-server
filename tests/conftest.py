import pytest

from webhookreplay.delivery import DeliveryService, MockSink
from webhookreplay.server import build_server
from webhookreplay.store import generate_events

SECRET = "unit-test-secret-not-a-real-key"
NOW = 1_800_000_000


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def make(audit_path):
    """make(allow_writes=False) -> a fully wired McpServer with a pinned clock and known secret."""
    def _make(allow_writes=False, **kwargs):
        return build_server(allow_writes=allow_writes, audit_file=str(audit_path), fixed_now=NOW, secret=SECRET, **kwargs)
    return _make


@pytest.fixture
def events():
    """The 40 generated events by id. Known facts at count=40:
    bad signature: n in 7,14,21,28,35 (5);  stale only: 5,10,15,20,25,30,40 (7);  valid: the other 28.
    sink fails 5x: 11, 22, 33;  fails once: 13, 26, 39."""
    return {e["id"]: e for e in generate_events(SECRET, NOW, 40)}


@pytest.fixture
def service():
    return DeliveryService(MockSink())


def rpc(server, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def handshake(server):
    response = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "0"}})
    assert "result" in response
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    return server
