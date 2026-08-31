"""Fast, process-local profile used only by the unit-test lane."""

from collections import defaultdict, deque

from redis import Redis

from .common import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
CACHES = {
    "default": {
        "BACKEND": "testauth.test_support.cache.PatternLocMemCache",
        "LOCATION": "buh-fast-source-tests",
    }
}


class _UnitRedisClient(Redis):
    """Minimal process-local stand-in for Alliance Auth startup statistics."""

    IS_STUB = True

    def __init__(self):
        self._counters = defaultdict(int)
        self._sorted_sets = defaultdict(dict)
        self._lists = defaultdict(deque)

    def ping(self):
        return True

    def info(self):
        return {"redis_version": "8.0.0"}

    def delete(self, *keys, **_kwargs):
        removed = 0
        for key in keys:
            for store in (self._counters, self._sorted_sets, self._lists):
                removed += int(store.pop(key, None) is not None)
        return removed

    def incr(self, key, amount=1):
        self._counters[key] += amount
        return self._counters[key]

    def zadd(self, key, mapping, **_kwargs):
        self._sorted_sets[key].update(mapping)
        return len(mapping)

    def zcount(self, key, min, max):
        lower = float("-inf") if min == "-inf" else float(min)
        upper = float("inf") if max == "+inf" else float(max)
        return sum(lower <= score <= upper for score in self._sorted_sets[key].values())

    def zrangebyscore(
        self,
        key,
        min,
        max,
        start=None,
        num=None,
        withscores=False,
        score_cast_func=float,
        **_kwargs,
    ):
        lower = float("-inf") if min == "-inf" else float(min)
        upper = float("inf") if max == "+inf" else float(max)
        rows = sorted(
            (member, score)
            for member, score in self._sorted_sets[key].items()
            if lower <= score <= upper
        )
        if start is not None:
            rows = rows[start : start + num if num is not None else None]
        if withscores:
            return [(member, score_cast_func(score)) for member, score in rows]
        return [member for member, _score in rows]

    def llen(self, key):
        return len(self._lists[key])

    def ltrim(self, key, start, stop):
        values = list(self._lists[key])[start : stop + 1]
        self._lists[key] = deque(values)
        return True

    def rpush(self, key, *values):
        self._lists[key].extend(str(value).encode("utf-8") for value in values)
        return len(self._lists[key])

    def lpop(self, key):
        return self._lists[key].popleft() if self._lists[key] else None


# Alliance Auth and aa-structures construct Redis-backed helper objects while
# Django apps populate. Unit tests do not exercise Redis semantics; those remain
# blocking integration tests. Patch only this disposable settings profile before
# app loading begins. The subclass keeps third-party isinstance guards intact.
import django_redis as _django_redis  # noqa: E402

_UNIT_REDIS_CLIENT = _UnitRedisClient()
_django_redis.get_redis_connection = lambda *_args, **_kwargs: _UNIT_REDIS_CLIENT

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
