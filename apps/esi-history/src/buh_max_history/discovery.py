"""Discover EVE Ref collections while respecting configured subtree boundaries."""

import hashlib
import time
from collections import deque
from datetime import timedelta
from urllib.parse import urlsplit

from django.utils.text import slugify
from django.utils.timezone import now

from .models import ArchiveConfiguration, PublicDataset
from .public_archive import _request_json, validate_everef_url


def discover_public_datasets(*, force=False):
    config = ArchiveConfiguration.get_solo()
    if not config.discover_public_datasets:
        return 0
    if (
        not force
        and config.last_public_discovery_at
        and config.last_public_discovery_at > now() - timedelta(days=1)
    ):
        return 0
    deadline = time.monotonic() + 60
    root, _ = _request_json("https://data.everef.net/index.json")
    directories = root.get("directories", [])
    if not isinstance(directories, list) or len(directories) > 250:
        raise ValueError("Unexpected public dataset index; discovery paused.")
    pending = deque(entry["index_url"] for entry in directories)
    created = requests = 0
    seen = set()
    existing = list(PublicDataset.objects.values_list("index_url", flat=True))
    while pending and requests < 20 and time.monotonic() < deadline:
        url = validate_everef_url(pending.popleft())
        if url in seen:
            continue
        seen.add(url)
        prefix = url.rsplit("/", 1)[0] + "/"
        # Includes disabled parents: discovery never re-enables their children.
        if any(url.startswith(old.rsplit("/", 1)[0] + "/") for old in existing):
            continue
        if any(old.startswith(prefix) for old in existing):
            # A selected subtree must not hide its unconfigured siblings.
            child_index, _ = _request_json(url)
            requests += 1
            children = child_index.get("directories", [])
            if not isinstance(children, list) or len(children) > 250:
                raise ValueError("Unexpected nested dataset index.")
            for child in children:
                child_url = validate_everef_url(child["index_url"])
                if child_url.startswith(prefix) and child_url != url:
                    pending.append(child_url)
            if child_index.get("files"):
                from .capture import _record_issue

                _record_issue("public_discovery", "partial_parent", url)
            continue
        path = urlsplit(url).path.strip("/").removesuffix("/index.json")
        slug = slugify(path)[:110]
        if not slug:
            continue
        if PublicDataset.objects.filter(slug=slug).exists():
            slug += "-" + hashlib.sha256(url.encode()).hexdigest()[:8]
        _, added = PublicDataset.objects.get_or_create(
            index_url=url,
            defaults={
                "name": "EVE Ref " + path[:110],
                "slug": slug,
                "priority": 100,
                "enabled": True,
            },
        )
        created += int(added)
        existing.append(url)
    if not pending:
        ArchiveConfiguration.objects.filter(singleton_id=1).update(
            last_public_discovery_at=now()
        )
    return created
