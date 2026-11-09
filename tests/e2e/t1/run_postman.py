"""Run checked-in V1 security collection against T1's server, credentials in memory.

Install pinned dependencies under /tmp/e2e-t1-newman first (see E2E_T1.md).
Newman's normal JSON reporter persists authorization headers, so this runner
uses its callback to emit ONLY aggregate statistics and assertion names.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from run_acceptance import ROOT


def run(journey, base):
    variables = {"base_url": base, "owner_key": journey.owner}
    for letter in ("a", "b"):
        reg = journey.call("POST", "/v1/agents", body={"name": "T1 Newman " + letter,
            "capabilities": ["wallet.read", "payment.prepare", "owner.request_decision", "payment.execute_sandbox"]})
        aid = reg["agent"]["agent_id"]
        variables["agent_" + letter + "_id"] = aid
        variables["agent_" + letter + "_key"] = reg["api_key"]
        journey.call("POST", "/v1/wallets", body={"agent_id": aid, "currency": "USD",
            "monthly_budget": "100.00", "per_transaction_limit": "10.00"})
    collection = json.loads((ROOT / "postman/v1.collection.json").read_text())
    collection["item"] = [x for x in collection["item"] if x["name"] == "V1 security"]
    assert len(collection["item"]) == 1
    for variable in collection["variable"]:
        if variable["key"] in variables:
            variable["value"] = variables[variable["key"]]
    module = Path("/tmp/e2e-t1-newman/node_modules/newman")
    if not module.is_dir():
        return {"status": "BLOCKED", "reason": "Pinned Newman dependencies absent"}
    program = r"""
const newman = require('/tmp/e2e-t1-newman/node_modules/newman');
let input = '';
process.stdin.on('data', part => input += part);
process.stdin.on('end', () => newman.run({collection: JSON.parse(input),
  reporters: [], timeoutRequest: 15000, timeoutScript: 5000}, (err, summary) => {
  const run = summary && summary.run;
  process.stdout.write(JSON.stringify({fatal: !!err,
    requests: run && run.stats.requests, assertions: run && run.stats.assertions,
    failures: run && run.failures.map(f => ({name: f.error.test || f.error.name,
      source: f.source && f.source.name})),
    executions: run && run.executions.map(e => ({name: e.item.name,
      status: e.response && e.response.code}))}));
}));
"""
    result = subprocess.run(["node", "-e", program], input=json.dumps(collection),
                            text=True, capture_output=True, timeout=120)
    if result.returncode:
        return {"status": "BLOCKED", "reason": "Newman process failed; output suppressed"}
    return json.loads(result.stdout)
