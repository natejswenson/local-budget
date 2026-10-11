import multiprocessing
import stat



def hold(path, acquired, release):
    from local_budget.connectors.plaid import locks
    with locks.item_locks(path, [2, 1, 2]) as success:
        acquired.set() if success else None
        release.wait(5)


def test_process_contention_releases_partial_locks(plaid_db):
    from local_budget.connectors.plaid import locks
    ctx = multiprocessing.get_context('spawn')
    acquired, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=hold, args=(plaid_db, acquired, release))
    process.start()
    try:
        assert acquired.wait(5)
        with locks.item_locks(plaid_db, [3, 2]) as success:
            assert not success
        with locks.item_locks(plaid_db, [3]) as success:
            assert success
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    with locks.item_locks(plaid_db, [2, 1, 2]) as success:
        assert success
    directory = plaid_db.parent / '.plaid-locks'
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert sorted(p.name for p in directory.iterdir()) == ['item-1.lock', 'item-2.lock', 'item-3.lock']
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
