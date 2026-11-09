"""Run the requested live CLIs without exposing prompts, URLs or credentials.

From agentpal/: python tests/e2e/t4/run_demos.py --live
The paypal_flow CLI buyer prompt is handled by the reviewed sandbox buyer harness.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def run_demo(module, *, buyer_prompt=False):
    from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
    start = time.monotonic()
    process = subprocess.Popen([sys.executable, "-m", module, "--live"], cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"}, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr, buyer_results = [], [], []

    def read_stderr():
        for line in process.stderr:
            if buyer_prompt and line.startswith("https://"):
                result = approve_sandbox_buyer(line.strip())
                buyer_results.append(result.status + ":" + result.reason)
                # Give PayPal time to register the buyer's action before the CLI's
                # single capture call. This changes no approval or provider evidence.
                if result.status == "approved":
                    time.sleep(4)
                process.stdin.write("APPROVED\n" if result.status == "approved" else "\n")
                process.stdin.flush()
            # Raw stderr is retained only in memory and never returned.
            stderr.append(line)
    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    for line in process.stdout:
        stdout.append(line)
    process.wait(timeout=360)
    reader.join(timeout=5)
    raw = "".join(stdout)
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"status": "UNVERIFIED", "reason": "CLI returned non-JSON output"}
    # Reports normally already redact IDs; cover unredacted uppercase provider IDs.
    cleaned = re.sub(r"(?<![\w])[A-Z0-9]{12,}(?![\w])", lambda m: "…" + m[0][-4:],
                     json.dumps(data))
    for stream in (process.stdin, process.stdout, process.stderr):
        stream.close()
    return {"command": module + " --live", "exit": process.returncode,
            "elapsed_s": round(time.monotonic() - start, 2), "report": json.loads(cleaned),
            "buyer": buyer_results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    if not args.live:
        return 2
    for module in ("agentpal.demo.paypal_world", "agentpal.demo.paypal_flow",
                   "agentpal.demo.payout_flow"):
        print(json.dumps(run_demo(module, buyer_prompt=module.endswith("paypal_flow"))), flush=True)
    # --list is the only authorized webhook setup operation.
    result = subprocess.run([sys.executable, "-m", "agentpal.tools.paypal_webhook_setup", "--list"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    print(json.dumps({"command": "paypal_webhook_setup --list", "exit": result.returncode,
                      "output": re.sub(r"\b[A-Z0-9]{10,}\b", lambda m: "…" + m[0][-4:],
                                       result.stdout.strip())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
