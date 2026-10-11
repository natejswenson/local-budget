"""Nonblocking host Item locks; acquire before intake locks and SQLite writes."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def item_locks(db_path: Path, item_ids: list[int]):
    """Yield acquisition success; hold every lock until the caller exits.

    Keep the context outside the database transaction so commit/rollback happens
    before unlock. A failed acquisition releases any earlier partial acquisition.
    """
    ids = sorted(set(item_ids))
    if any(type(i) is not int or i < 1 for i in ids):
        raise ValueError('invalid_item_id')
    directory = db_path.resolve().parent / '.plaid-locks'
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    acquired = []
    success = True
    try:
        for item_id in ids:
            fd = os.open(directory / f'item-{item_id}.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                success = False
                break
            acquired.append(fd)
        if not success:
            for fd in reversed(acquired):
                os.close(fd)
            acquired.clear()
        yield success
    finally:
        for fd in reversed(acquired):
            os.close(fd)
