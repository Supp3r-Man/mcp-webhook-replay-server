"""HMAC-SHA256 webhook signatures with a timestamp tolerance window.

The signed message is "<timestamp>.<body>", the same shape most webhook providers use, so a captured
body cannot be replayed with a fresh timestamp without the secret.
"""
import hashlib
import hmac

DEFAULT_TOLERANCE_SECONDS = 300


def sign(secret: str, timestamp: int, body: str) -> str:
    message = f"{timestamp}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: int, body: str, signature, now: int,
           tolerance: int = DEFAULT_TOLERANCE_SECONDS) -> dict:
    """Check a signature. reason is one of: ok, bad_signature, stale, future.

    The signature is compared first and in constant time, so a wrong signature is reported as such
    whatever the timestamp, and timing does not reveal how many leading characters matched.
    """
    expected = sign(secret, timestamp, body)
    age = now - timestamp
    if not isinstance(signature, str):
        return {"ok": False, "reason": "bad_signature", "age_seconds": age}
    if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
        return {"ok": False, "reason": "bad_signature", "age_seconds": age}
    if age > tolerance:
        return {"ok": False, "reason": "stale", "age_seconds": age}
    if age < -tolerance:
        return {"ok": False, "reason": "future", "age_seconds": age}
    return {"ok": True, "reason": "ok", "age_seconds": age}
