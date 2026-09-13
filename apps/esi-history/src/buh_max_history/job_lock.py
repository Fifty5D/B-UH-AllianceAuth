"""Process-owned mirror lock, released when a worker's database session ends."""

import fcntl
import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connection

from .capture import archive_root

_SESSION = ContextVar("buh_archive_lock_session", default=None)


class ArchiveLockLost(RuntimeError):
    pass


def _lock_name():
    database = str(connection.settings_dict["NAME"])
    return "buh_archive_" + hashlib.sha256(database.encode()).hexdigest()[:40]


def assert_public_archive_lock():
    session = _SESSION.get()
    if session is not None:
        if connection.connection is not session:
            raise ArchiveLockLost(
                "Public mirror database session changed; stop this batch."
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT IS_USED_LOCK(%s) = CONNECTION_ID()", [_lock_name()])
            if cursor.fetchone()[0] != 1:
                raise ArchiveLockLost("Public mirror lock was lost; stop this batch.")


@contextmanager
def public_archive_lock():
    if _SESSION.get() is not None:
        yield False
        return
    if connection.vendor == "mysql":
        # MariaDB advisory locks are shared across all Auth containers and are
        # independent of cache expiry, transaction commits and archive mounts.
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0)", [_lock_name()])
            acquired = cursor.fetchone()[0] == 1
        session = connection.connection
        token = _SESSION.set(session) if acquired else None
        try:
            yield acquired
        finally:
            if token is not None:
                _SESSION.reset(token)
            if acquired and connection.connection is session:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", [_lock_name()])
        return
    if connection.vendor != "sqlite":
        raise RuntimeError("Public mirror locking supports MariaDB and local SQLite only.")
    # SQLite is used by the disposable fast tests and single-host development.
    # Never unlink this file: its inode is the lock identity.
    root = archive_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        root / ".public-archive.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)
