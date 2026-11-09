"""Nested wallet transactions preserve outer atomicity for idempotent preparation."""

import pytest

from agentpal.wallet.store import WalletStore


def test_nested_commit_is_rolled_back_with_outer_transaction():
    store = WalletStore()
    with pytest.raises(RuntimeError, match="outer interrupted"):
        with store.tx():
            with store.tx() as c:
                c.execute("INSERT INTO wallet_meta VALUES ('probe', 'inner')")
            raise RuntimeError("outer interrupted")
    assert store.query("SELECT * FROM wallet_meta WHERE key='probe'") == []


def test_inner_rollback_leaves_outer_transaction_active():
    store = WalletStore()
    with store.tx() as c:
        c.execute("INSERT INTO wallet_meta VALUES ('outer-probe', 'kept')")
        with pytest.raises(RuntimeError, match="inner interrupted"):
            with store.tx() as inner:
                inner.execute("INSERT INTO wallet_meta VALUES ('inner-probe', 'removed')")
                raise RuntimeError("inner interrupted")
        assert c.in_transaction
    assert store.query("SELECT value FROM wallet_meta WHERE key='outer-probe'") == [{"value": "kept"}]
    assert store.query("SELECT * FROM wallet_meta WHERE key='inner-probe'") == []


def test_nested_release_keeps_sqlite_write_lock_across_connections(tmp_path):
    import sqlite3
    first = WalletStore(tmp_path / "wallet.db")
    second = WalletStore(tmp_path / "wallet.db", busy_timeout_s=0.01)
    with first.tx():
        with first.tx() as c:
            c.execute("INSERT INTO wallet_meta VALUES ('lock-probe', 'held')")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with second.tx():
                raise AssertionError("a nested commit released the outer SQLite lock")
    with second.tx() as c:
        c.execute("INSERT INTO wallet_meta VALUES ('next-probe', 'allowed')")
    assert len(first.query("SELECT * FROM wallet_meta WHERE key LIKE '%-probe'")) == 2
