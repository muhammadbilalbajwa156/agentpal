"""OS-level permission checks use an unprivileged child; no real credentials."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

from .support import ROOT, Server, free_port


def test_read_only_database_refuses_financial_startup(tmp_path):
    server = Server(tmp_path).start()
    try:
        server.stop()
        # SQLite's mode=ro enforces read-only even for root. chmod alone would
        # give a false pass under this container's uid 0.
        env = {**server.env, "AGENTPAL_WALLET_DB_PATH":
               f"file:{tmp_path / 'wallet.db'}?mode=ro"}
        result = subprocess.run([sys.executable, "-m", "agentpal", "serve", "--platform", "--port", str(free_port())],
            cwd=ROOT, env=env, capture_output=True, timeout=15)
        assert result.returncode != 0
        assert b"readonly database" in result.stdout + result.stderr
        assert b"Application startup complete" not in result.stdout + result.stderr
    finally:
        server.close()


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T4-1).
@pytest.mark.skipif(os.geteuid() != 0, reason="requires root only to drop child to nobody")
def test_other_local_user_cannot_read_owner_identity():
    # A normal 0755 deployment directory, with no privileged parent directories
    # accidentally protecting the application's standard-umask 0644 files.
    from agentpal.identity.store import IdentityStore
    directory = Path(tempfile.mkdtemp(prefix="e2e-t4-disclosure-"))
    directory.chmod(0o755)
    previous = os.umask(0o022)
    try:
        store = IdentityStore(directory / "identity.db")
        store.close()
        def drop_user():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
        try:
            result = subprocess.run([sys.executable, "-c",
            "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
            "c.execute('SELECT count(*) FROM identity_owner_credentials').fetchone()",
                str(directory / "identity.db")], capture_output=True, timeout=5, preexec_fn=drop_user)
        except subprocess.SubprocessError:
            pytest.skip("container blocks setgroups/setgid/setuid; file-mode proof is separate")
        assert result.returncode != 0, "OS uid 65534 can read the owner's credential database"
    finally:
        os.umask(previous)
        shutil.rmtree(directory)


def test_sqlite_integrity_on_persistent_files(tmp_path):
    server = Server(tmp_path).start()
    try:
        _aid, key, _wallet = server.agent()
        payment = server.payment(key)
        server.approve(payment)
        server.restart()
        for path in tmp_path.glob("*.db"):
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        server.close()
