from __future__ import annotations

import hashlib
import secrets
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, cast


class CoordinationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class LockLease(ABC):
    @abstractmethod
    def release(self) -> None:
        raise NotImplementedError


class CoordinationBackend(ABC):
    name: str

    @abstractmethod
    def health(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def rate_limit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        raise NotImplementedError

    @abstractmethod
    def get_idempotency(self, key: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def put_idempotency(self, key: str, value: str, *, ttl_seconds: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def acquire_lock(self, key: str, *, ttl_seconds: int) -> LockLease | None:
        raise NotImplementedError

    @abstractmethod
    def circuit_is_open(self, key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def circuit_success(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def circuit_failure(self, key: str, *, threshold: int, open_seconds: int) -> None:
        raise NotImplementedError


@dataclass
class _ExpiringValue:
    value: str
    expires_at: float


class _LocalLease(LockLease):
    def __init__(self, backend: LocalCoordination, key: str, token: str) -> None:
        self.backend = backend
        self.key = key
        self.token = token

    def release(self) -> None:
        self.backend._release_lock(self.key, self.token)


class LocalCoordination(CoordinationBackend):
    """Thread-safe single-process implementation used by default and as rate fallback."""

    name = "local"

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._rate: dict[str, tuple[int, float]] = {}
        self._idempotency: dict[str, _ExpiringValue] = {}
        self._locks: dict[str, _ExpiringValue] = {}
        self._circuits: dict[str, tuple[int, float]] = {}

    def health(self) -> bool:
        return True

    def rate_limit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.monotonic()
        with self._mutex:
            count, expires_at = self._rate.get(key, (0, now + window_seconds))
            if expires_at <= now:
                count, expires_at = 0, now + window_seconds
            if count >= limit:
                return RateLimitDecision(False, max(1, int(expires_at - now + 0.999)))
            self._rate[key] = (count + 1, expires_at)
        return RateLimitDecision(True)

    def get_idempotency(self, key: str) -> str | None:
        now = time.monotonic()
        with self._mutex:
            item = self._idempotency.get(key)
            if item is None:
                return None
            if item.expires_at <= now:
                self._idempotency.pop(key, None)
                return None
            return item.value

    def put_idempotency(self, key: str, value: str, *, ttl_seconds: int) -> None:
        with self._mutex:
            self._idempotency[key] = _ExpiringValue(value, time.monotonic() + ttl_seconds)

    def acquire_lock(self, key: str, *, ttl_seconds: int) -> LockLease | None:
        now = time.monotonic()
        token = secrets.token_hex(16)
        with self._mutex:
            current = self._locks.get(key)
            if current is not None and current.expires_at > now:
                return None
            self._locks[key] = _ExpiringValue(token, now + ttl_seconds)
        return _LocalLease(self, key, token)

    def _release_lock(self, key: str, token: str) -> None:
        with self._mutex:
            current = self._locks.get(key)
            if current is not None and current.value == token:
                self._locks.pop(key, None)

    def circuit_is_open(self, key: str) -> bool:
        now = time.monotonic()
        with self._mutex:
            failures, open_until = self._circuits.get(key, (0, 0.0))
            if open_until > now:
                return True
            if open_until:
                self._circuits[key] = (failures, 0.0)
            return False

    def circuit_success(self, key: str) -> None:
        with self._mutex:
            self._circuits.pop(key, None)

    def circuit_failure(self, key: str, *, threshold: int, open_seconds: int) -> None:
        now = time.monotonic()
        with self._mutex:
            failures, open_until = self._circuits.get(key, (0, 0.0))
            if open_until > now:
                return
            failures += 1
            self._circuits[key] = (
                failures,
                now + open_seconds if failures >= threshold else 0.0,
            )


class _RedisLease(LockLease):
    RELEASE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, client: Any, key: str, token: str) -> None:
        self.client = client
        self.key = key
        self.token = token

    def release(self) -> None:
        try:
            self.client.eval(self.RELEASE_SCRIPT, 1, self.key, self.token)
        except Exception as exc:
            raise CoordinationUnavailable("redis_lock_release_failed") from exc


class RedisCoordination(CoordinationBackend):
    name = "redis"
    RATE_SCRIPT = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return {current, redis.call('TTL', KEYS[1])}
    """
    CIRCUIT_FAILURE_SCRIPT = """
    local failures = redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    if failures >= tonumber(ARGV[1]) then
      redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
      redis.call('DEL', KEYS[1])
    end
    return failures
    """

    def __init__(self, client: Any, *, key_prefix: str = "autoseguro") -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")

    @classmethod
    def from_url(cls, url: str, *, key_prefix: str = "autoseguro") -> RedisCoordination:
        try:
            from redis import Redis
        except ImportError as exc:
            raise ValueError(
                "COORDINATION_BACKEND=redis requer instalação do extra 'redis'"
            ) from exc
        return cls(
            Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            ),
            key_prefix=key_prefix,
        )

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    def health(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def rate_limit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        try:
            result = cast(
                list[int], self.client.eval(self.RATE_SCRIPT, 1, self._key(key), window_seconds)
            )
            count, ttl = int(result[0]), max(1, int(result[1]))
            return RateLimitDecision(count <= limit, ttl if count > limit else 0)
        except Exception as exc:
            raise CoordinationUnavailable("redis_rate_limit_failed") from exc

    def get_idempotency(self, key: str) -> str | None:
        try:
            value = self.client.get(self._key(key))
            return str(value) if value is not None else None
        except Exception as exc:
            raise CoordinationUnavailable("redis_idempotency_read_failed") from exc

    def put_idempotency(self, key: str, value: str, *, ttl_seconds: int) -> None:
        try:
            self.client.set(self._key(key), value, ex=ttl_seconds)
        except Exception as exc:
            raise CoordinationUnavailable("redis_idempotency_write_failed") from exc

    def acquire_lock(self, key: str, *, ttl_seconds: int) -> LockLease | None:
        token = secrets.token_hex(16)
        try:
            namespaced = self._key(key)
            acquired = self.client.set(namespaced, token, nx=True, ex=ttl_seconds)
            return _RedisLease(self.client, namespaced, token) if acquired else None
        except Exception as exc:
            raise CoordinationUnavailable("redis_lock_failed") from exc

    def circuit_is_open(self, key: str) -> bool:
        try:
            return bool(self.client.exists(self._key(key)))
        except Exception as exc:
            raise CoordinationUnavailable("redis_circuit_read_failed") from exc

    def circuit_success(self, key: str) -> None:
        try:
            namespaced = self._key(key)
            self.client.delete(f"{namespaced}:failures", namespaced)
        except Exception as exc:
            raise CoordinationUnavailable("redis_circuit_reset_failed") from exc

    def circuit_failure(self, key: str, *, threshold: int, open_seconds: int) -> None:
        try:
            self.client.eval(
                self.CIRCUIT_FAILURE_SCRIPT,
                2,
                f"{self._key(key)}:failures",
                self._key(key),
                threshold,
                open_seconds,
            )
        except Exception as exc:
            raise CoordinationUnavailable("redis_circuit_write_failed") from exc


def opaque_key(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"autoseguro:{namespace}:{digest}"


def coordination_from_config(
    backend: str, redis_url: str, *, key_prefix: str = "autoseguro"
) -> CoordinationBackend:
    if backend == "local":
        return LocalCoordination()
    if backend == "redis":
        return RedisCoordination.from_url(redis_url, key_prefix=key_prefix)
    raise ValueError("COORDINATION_BACKEND deve ser 'local' ou 'redis'")
