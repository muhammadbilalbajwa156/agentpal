"""P0.1 regression: atomic replay protection (review probe P09).

Before the fix, concurrent submissions of one request_id were all assessed as new
requests because the service read history and saved in separate steps.
"""

from __future__ import annotations

import multiprocessing as mp
import threading

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.store import AssessmentStore

from conftest import VALID

N = 8


def _service(db_path) -> RiskService:
    return RiskService(load_policy(), AssessmentStore(db_path), [HeuristicAnalyzer()])


def _concurrent(service: RiskService, payload: dict, n: int = N) -> list:
    req = PaymentRequest.model_validate(payload)
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(service.assess(req))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    return results


def _statuses(service: RiskService, request_id: str) -> list[str]:
    return [r["status"] for r in service.store.audit_records(request_id)]


def test_concurrent_same_request_id_only_one_new(tmp_path):
    service = _service(tmp_path / "a.db")
    results = _concurrent(service, VALID)
    statuses = _statuses(service, VALID["request_id"])
    assert statuses.count("assessed") == 1, statuses
    assert statuses.count("replay_rejected") == N - 1, statuses
    dup = [a for a in results if "DUPLICATE_REQUEST_ID" in a.hard_violations]
    assert len(dup) == N - 1
    assert all(a.recommendation == "block" for a in dup)
    # the single winner was not itself treated as a duplicate
    winners = [a for a in results if "DUPLICATE_REQUEST_ID" not in a.hard_violations]
    assert len(winners) == 1
    # the stored assessment is the winner's, not a duplicate rejection
    stored = service.store.get_assessment(VALID["request_id"])
    assert stored is not None and "DUPLICATE_REQUEST_ID" not in stored.hard_violations


def test_duplicates_rejected_after_restart(tmp_path):
    db = tmp_path / "restart.db"
    first = _service(db)
    first.assess(PaymentRequest.model_validate(VALID))
    first.store.close()

    restarted = _service(db)  # new process-equivalent: new connection, no in-memory state
    results = _concurrent(restarted, VALID)
    assert all("DUPLICATE_REQUEST_ID" in a.hard_violations for a in results)
    statuses = _statuses(restarted, VALID["request_id"])
    assert statuses.count("assessed") == 1 and statuses.count("replay_rejected") == N


def _process_worker(db_path: str, start, out) -> None:
    service = _service(db_path)
    start.wait(30)
    a = service.assess(PaymentRequest.model_validate(VALID))
    out.put("DUPLICATE_REQUEST_ID" in a.hard_violations)


def test_concurrent_across_processes(tmp_path):
    """Separate processes (as separate API workers would be) share only the database."""
    ctx = mp.get_context("spawn")
    start, out = ctx.Event(), ctx.Queue()
    db = str(tmp_path / "multi.db")
    _service(db).store.close()  # create schema once
    procs = [ctx.Process(target=_process_worker, args=(db, start, out)) for _ in range(N)]
    [p.start() for p in procs]
    start.set()
    [p.join(60) for p in procs]
    flags = [out.get(timeout=5) for _ in range(N)]
    assert flags.count(False) == 1, flags  # exactly one process claimed the id
    statuses = _statuses(_service(db), VALID["request_id"])
    assert statuses.count("assessed") == 1, statuses


def _open_fresh_stores(paths, barrier, out) -> None:
    for path in paths:
        barrier.wait(60)
        try:
            AssessmentStore(path).close()
            out.put("ok")
        except Exception as exc:  # report, do not hide
            out.put(f"{type(exc).__name__}: {exc}")


def test_concurrent_first_open_of_a_fresh_database(tmp_path):
    """Several workers starting together on a new database file (the multi-worker
    deployment case). Before the fix, switching the journal mode raised 'database is
    locked' without waiting for the busy timeout, which made a uvicorn worker fail at
    startup and the uvicorn supervisor stop the whole server."""
    ctx = mp.get_context("spawn")
    procs_n, trials = 4, 10
    barrier, out = ctx.Barrier(procs_n), ctx.Queue()
    paths = [str(tmp_path / f"fresh{i}.db") for i in range(trials)]
    procs = [ctx.Process(target=_open_fresh_stores, args=(paths, barrier, out))
             for _ in range(procs_n)]
    [p.start() for p in procs]
    results = [out.get(timeout=120) for _ in range(procs_n * trials)]
    [p.join(30) for p in procs]
    assert results == ["ok"] * (procs_n * trials), sorted(set(results))


def test_concurrent_across_real_uvicorn_workers(tmp_path):
    """End to end: two uvicorn worker processes behind one port, one SQLite file."""
    import os
    import socket
    import subprocess
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "AGENTPAL_DB_PATH": str(tmp_path / "workers.db"),
           "AGENTPAL_ANALYZER_CHAIN": "heuristic", "AGENTPAL_LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentpal.api:create_app", "--factory",
         "--workers", "2", "--port", str(port), "--log-level", "warning"],
        env=env, cwd=os.path.dirname(os.path.dirname(__file__)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        payload = {**VALID, "request_id": "req_workers_001"}
        with ThreadPoolExecutor(N) as pool:
            bodies = list(pool.map(
                lambda _: httpx.post(f"{base}/assess", json=payload, timeout=30).json(),
                range(N)))
        dup = [b for b in bodies if "DUPLICATE_REQUEST_ID" in b["hard_violations"]]
        assert len(dup) == N - 1, [b["hard_violations"] for b in bodies]
    finally:
        proc.terminate()
        proc.wait(10)


def test_legacy_schema_database_is_refused(tmp_path):
    import sqlite3

    import pytest

    from agentpal.store import IncompatibleDatabase

    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE request_history (request_id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(IncompatibleDatabase):
        AssessmentStore(db)
