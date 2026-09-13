"""Retain immutable public payloads and record each observed state change."""

import hashlib
import os
import time

from django.utils.timezone import now

from .capture import archive_root
from .models import PublicArchiveRevision


def hash_file(path, deadline):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Preserving the public revision reached the batch limit."
                )
            hasher.update(chunk)
    return hasher.hexdigest()


def retain_public_revision(record, path, *, deadline, digest=None, observed_at=None):
    if not path.is_file():
        return
    digest = digest or hash_file(path, deadline)
    root = archive_root().resolve()
    target = root / "public-revisions" / str(record.pk) / digest
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # Current and historical paths share an inode; the replacement of the
        # current path never changes the retained payload or doubles disk use.
        os.link(path, target)
    except FileExistsError:
        if (
            target.is_symlink()
            or target.stat().st_size != path.stat().st_size
            or hash_file(target, deadline) != digest
        ):
            raise ValueError(
                "Existing public revision is inconsistent; preserve the current file."
            )
    stamp = observed_at or now()
    latest = record.revisions.first()
    if latest and latest.payload_sha256 == digest:
        PublicArchiveRevision.objects.filter(pk=latest.pk).update(last_observed_at=stamp)
    else:
        PublicArchiveRevision.objects.create(
            file=record,
            payload_sha256=digest,
            relative_path=target.relative_to(root).as_posix(),
            stored_bytes=path.stat().st_size,
            first_observed_at=stamp,
            last_observed_at=stamp,
        )
