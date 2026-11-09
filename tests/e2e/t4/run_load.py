"""OFFLINE TCP load, threads plus spawned processes; no PayPal/Channel3 calls."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/e2e")]

import httpx  # noqa: E402
from t4.support import Server  # noqa: E402


def worker(base, credentials, count, create=False):
    with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
        def hit(i):
            key = credentials[i % len(credentials)]
            headers = {"Authorization": "Bearer " + key}
            start = time.monotonic()
            try:
                if create:
                    headers["Idempotency-Key"] = "t4-process-shared"
                    response = client.post("/v1/payment-requests", headers=headers, json={
                        "amount": "1.00", "currency": "USD", "merchant_name": "T4 Load Store",
                        "purpose": "Same request across independent processes"})
                else:
                    response = client.get("/v1/me/wallet", headers=headers)
                return response.status_code, time.monotonic() - start
            except httpx.HTTPError:
                return 0, time.monotonic() - start
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(hit, range(count)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    root = Path("/tmp/e2e-t4")
    root.mkdir(exist_ok=True, mode=0o700)
    server = Server(tempfile.mkdtemp(prefix="load-", dir=root)).start()
    process = None
    try:
        agents = [server.agent() for _ in range(8)]
        credentials = [a[1] for a in agents]
        if args.workers == 2:
            server.stop()
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "t4.worker:factory",
                "--factory", "--workers", "2", "--host", "127.0.0.1", "--port", "8104",
                "--app-dir", str(ROOT / "tests/e2e"), "--no-proxy-headers", "--log-level", "error"],
                cwd=ROOT, env=server.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if server.client.get("/v1/health", timeout=.2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            # Both workers must finish startup before financial mutations (documented
            # quiescent-startup restriction). Rolling restarts are tested separately.
            time.sleep(1)
        start = time.monotonic()
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(worker, [server.base] * 4, [credentials] * 4, [200] * 4))
            mutations = list(pool.map(worker, [server.base] * 4, [credentials] * 4, [40] * 4, [True] * 4))
        elapsed = time.monotonic() - start
        reads = [item for result in results for item in result]
        writes = [item for result in mutations for item in result]
        latency = sorted(t for _code, t in reads + writes)
        ledger = server.ledger("SELECT count(*) AS n, sum(CAST(amount AS NUMERIC)) AS total FROM ledger_entries")
        report = {"workers": args.workers, "client_processes": 4, "threads_per_process": 4,
                  "agents": 8, "reads": len(reads), "create_calls": len(writes),
                  "read_status": dict(Counter(c for c, _t in reads)),
                  "create_status": dict(Counter(c for c, _t in writes)), "elapsed_s": round(elapsed, 3),
                  "throughput_rps": round((len(reads) + len(writes)) / elapsed, 1),
                  "p50_ms": round(statistics.median(latency) * 1000, 2),
                  "p95_ms": round(latency[int(len(latency) * .95)] * 1000, 2),
                  "ledger_rows": ledger[0]["n"], "ledger_committed": str(ledger[0]["total"]),
                  "live_provider_orders": 0}
        print(json.dumps(report), flush=True)
        assert all(c == 200 for c, _t in reads)
        assert all(c in (200, 201, 409) for c, _t in writes)
        assert sum(c == 201 for c, _t in writes) == 8
        assert ledger[0]["n"] == 8 and ledger[0]["total"] == 8
        return 0
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
