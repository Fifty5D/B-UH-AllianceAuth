"""Process-local cache backend for the source unit-test profile."""

from django.core.cache.backends.locmem import LocMemCache


class PatternLocMemCache(LocMemCache):
    """Provide django-redis's invalidation API without a Redis process."""

    def delete_pattern(self, _pattern, version=None, client=None, itersize=None):
        del version, client, itersize
        self.clear()
        return 0
