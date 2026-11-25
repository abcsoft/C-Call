"""Distributed TTS concurrency semaphore via Redis.

Mechanism: Redis INCR/DECR on key ``tts:concurrent:{tenant_id}``.
- INCR is atomic in Redis (single-threaded).
- If the resulting count exceeds the plan limit, DECR immediately and reject.
- Key TTL = TTS_CONCURRENT_TTL_SECONDS (default 120s) to auto-release on worker crash.
- Preview and order-call TTS share the same pool per tenant.

Failure policy (Redis unavailable):
- Fail open: allow TTS to proceed to avoid blocking legitimate calls.
- Logged at WARNING level.

0 meaning for max_concurrent_tts: not a valid value (min=1 enforced at plan creation).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger("ccall")

TTS_CONCURRENT_TTL_SECONDS = 120  # auto-release after 2 minutes (max TTS generation time)


def _get_redis():
    try:
        from app.extensions import redis_ext
        return redis_ext._client
    except Exception:
        return None


def acquire_tts_slot(tenant_id: int, max_concurrent: int) -> bool:
    """Attempt to acquire a TTS concurrency slot for tenant.

    Returns True if slot acquired, False if at limit (caller should return 429).
    On Redis failure: returns True (fail open) and logs warning.
    """
    redis = _get_redis()
    if redis is None:
        logger.warning("tts_concurrent: Redis unavailable, failing open for tenant=%s", tenant_id)
        return True

    key = f"tts:concurrent:{tenant_id}"
    try:
        count = redis.incr(key)
        redis.expire(key, TTS_CONCURRENT_TTL_SECONDS)
        if count > max_concurrent:
            redis.decr(key)
            logger.info(
                "tts_concurrent: slot denied tenant=%s count=%s limit=%s",
                tenant_id, count, max_concurrent,
            )
            return False
        logger.debug(
            "tts_concurrent: slot acquired tenant=%s count=%s limit=%s",
            tenant_id, count, max_concurrent,
        )
        return True
    except Exception as exc:
        logger.warning("tts_concurrent: Redis error, failing open tenant=%s: %s", tenant_id, exc)
        return True


def release_tts_slot(tenant_id: int) -> None:
    """Release a TTS concurrency slot. Clamps at 0 to prevent negative counters.

    Uses DECR then conditional SET(0) — two commands, but the worst-case race is
    an extra DECR that briefly makes counter -1 before SET fixes it; another
    in-flight acquire would see -1 + 1 = 0 ≤ limit and proceed correctly.
    """
    redis = _get_redis()
    if redis is None:
        return

    key = f"tts:concurrent:{tenant_id}"
    try:
        result = redis.decr(key)
        if result < 0:
            redis.set(key, 0)
            result = 0
        logger.debug("tts_concurrent: slot released tenant=%s remaining=%s", tenant_id, result)
    except Exception as exc:
        logger.warning("tts_concurrent: release error tenant=%s: %s", tenant_id, exc)


@contextmanager
def tts_concurrency_slot(tenant_id: int, max_concurrent: int) -> Generator[bool, None, None]:
    """Context manager: acquire slot on enter, release on exit (success, exception, or timeout).

    Yields True if slot acquired, False if at limit.
    Caller must check the yielded value and return 429 if False.

    Example::

        with tts_concurrency_slot(tenant_id, max_concurrent) as acquired:
            if not acquired:
                return error_429_response
            result = generate_tts(...)
    """
    acquired = acquire_tts_slot(tenant_id, max_concurrent)
    try:
        yield acquired
    finally:
        if acquired:
            release_tts_slot(tenant_id)


def get_active_slot_count(tenant_id: int) -> int:
    """Return current active TTS job count for tenant (for monitoring/display)."""
    redis = _get_redis()
    if redis is None:
        return 0
    key = f"tts:concurrent:{tenant_id}"
    try:
        val = redis.get(key)
        return max(0, int(val or 0))
    except Exception:
        return 0
