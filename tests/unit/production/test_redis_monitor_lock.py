"""
Phase 7: Redis owner-token monitor lock tests.

Verifies:
- Owner acquires lock
- Second owner denied
- Wrong owner cannot release (Lua compare-and-delete)
- Owner renews with Lua compare-and-expire
- Expired lock is reclaimable
- Redis restart simulation (lock TTL expiry)
"""
from __future__ import annotations

import os
import time
import pytest

_LOCK_KEY = "ccall:test_monitor_lock"
_LOCK_TTL = 2  # short TTL for testing

_LOCK_RENEW_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
else
    return 0
end
"""

_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


@pytest.fixture()
def redis_client():
    redis = pytest.importorskip("redis")
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.from_url(url, decode_responses=False)
        client.ping()
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
    client.delete(_LOCK_KEY)
    yield client
    client.delete(_LOCK_KEY)


# ── 1. Owner acquires ─────────────────────────────────────────────────────────
def test_owner_acquires_lock(redis_client):
    result = redis_client.set(_LOCK_KEY, "worker-A", ex=_LOCK_TTL, nx=True)
    assert result is True, "First acquire must succeed"
    val = redis_client.get(_LOCK_KEY)
    assert val == b"worker-A"


# ── 2. Second owner denied ────────────────────────────────────────────────────
def test_second_owner_denied(redis_client):
    redis_client.set(_LOCK_KEY, "worker-A", ex=_LOCK_TTL, nx=True)
    result = redis_client.set(_LOCK_KEY, "worker-B", ex=_LOCK_TTL, nx=True)
    assert result is None or result is False, "Second NX acquire must fail"
    val = redis_client.get(_LOCK_KEY)
    assert val == b"worker-A", "Lock value must not change"


# ── 3. Wrong owner cannot release ────────────────────────────────────────────
def test_wrong_owner_cannot_release(redis_client):
    redis_client.set(_LOCK_KEY, "worker-A", ex=_LOCK_TTL, nx=True)
    result = redis_client.eval(_LOCK_RELEASE_LUA, 1, _LOCK_KEY, "worker-B")
    assert result == 0, "Wrong owner must not be able to release lock"
    val = redis_client.get(_LOCK_KEY)
    assert val == b"worker-A", "Lock must still be held by worker-A"


# ── 4. Owner renews via Lua ───────────────────────────────────────────────────
def test_owner_renews_lock(redis_client):
    redis_client.set(_LOCK_KEY, "worker-A", ex=_LOCK_TTL, nx=True)
    result = redis_client.eval(
        _LOCK_RENEW_LUA, 1, _LOCK_KEY, "worker-A", str(_LOCK_TTL + 5)
    )
    assert result == 1, "Owner must be able to renew lock"
    ttl = redis_client.ttl(_LOCK_KEY)
    assert ttl > _LOCK_TTL, f"TTL must be extended; got {ttl}"


# ── 5. Non-owner cannot renew ────────────────────────────────────────────────
def test_non_owner_cannot_renew(redis_client):
    redis_client.set(_LOCK_KEY, "worker-A", ex=_LOCK_TTL, nx=True)
    result = redis_client.eval(
        _LOCK_RENEW_LUA, 1, _LOCK_KEY, "worker-B", str(_LOCK_TTL + 5)
    )
    assert result == 0, "Non-owner must not be able to renew lock"


# ── 6. Expired lock reclaimable ───────────────────────────────────────────────
def test_expired_lock_reclaimable(redis_client):
    redis_client.set(_LOCK_KEY, "worker-A", ex=1, nx=True)  # 1s TTL
    time.sleep(1.5)
    result = redis_client.set(_LOCK_KEY, "worker-B", ex=_LOCK_TTL, nx=True)
    assert result is True, "Expired lock must be reclaimable"
    val = redis_client.get(_LOCK_KEY)
    assert val == b"worker-B"


# ── 7. Owner releases, next can acquire ──────────────────────────────────────
def test_owner_releases_lock(redis_client):
    redis_client.set(_LOCK_KEY, "worker-A", ex=_LOCK_TTL, nx=True)
    result = redis_client.eval(_LOCK_RELEASE_LUA, 1, _LOCK_KEY, "worker-A")
    assert result == 1, "Owner must be able to release lock"
    # Next worker can acquire immediately
    result2 = redis_client.set(_LOCK_KEY, "worker-B", ex=_LOCK_TTL, nx=True)
    assert result2 is True, "Lock must be acquirable after release"


# ── 8. TTL expiry simulates Redis restart (no persistent lock) ───────────────
def test_lock_expires_simulates_restart(redis_client):
    """Short-TTL lock expires — simulates worker crash / Redis restart."""
    redis_client.set(_LOCK_KEY, "crashed-worker", ex=1, nx=True)
    time.sleep(1.5)
    val = redis_client.get(_LOCK_KEY)
    assert val is None, "Lock must expire automatically (simulating crashed worker)"
    # New worker can take over
    result = redis_client.set(_LOCK_KEY, "new-worker", ex=_LOCK_TTL, nx=True)
    assert result is True
