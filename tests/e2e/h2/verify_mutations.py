"""Calibrate four H2 guards using isolated runtime patches, NEVER product-file edits.

Run from agentpal/: python tests/e2e/h2/verify_mutations.py
Only assertion failures count as caught; collection/setup errors do not.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[3]
MUTATIONS = {
    "self_receiver": ("""from agentpal.platform.payouts import PayoutDesk
PayoutDesk.self_receiver = lambda *args: None
""", "test_authorization.py::test_self_payout_is_refused_for_every_owner_maintained_alias[email]"),
    "current_execute_grant": ("""from agentpal.wallet.permissions import PermissionChecker, DenyCode
PermissionChecker._decide = lambda *args: DenyCode.ALLOWED
""", "test_revocation_mfa_text.py::test_completed_policy_changes_override_prior_approval[remove_execute-payment]"),
    "independent_ledger_terms": ("""from agentpal.store import AssessmentStore
def unchecked(c, agent_id, request_id, terms):
    return c.execute('SELECT terms_sha256 FROM request_terms WHERE agent_id=? AND request_id=?',
                     (agent_id, request_id)).fetchone()[0]
AssessmentStore._check_terms = staticmethod(unchecked)
""", "test_authorization.py::test_payment_ledger_independently_rejects_changed_approved_terms[purpose-Changed purpose]"),
    "refund_capture_bound": ("""import inspect, textwrap
import agentpal.platform.refunds as module
source = textwrap.dedent(inspect.getsource(module.RefundDesk.create))
assert source.count('amount > remaining') == 1
source = source.replace('amount > remaining', 'False')
namespace = dict(vars(module))
exec(compile(source, '<H2 runtime mutation>', 'exec'), namespace)
module.RefundDesk.create = namespace['create']
""", "test_money_integrity.py::test_over_refund_is_refused_before_any_provider_mutation"),
}


def main():
    directory = Path("/tmp/m8-h2")
    directory.mkdir(exist_ok=True, mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("PAYPAL_", "CHANNEL3_", "KERNEL_", "H2_RUN_LIVE"))}
    results = []
    with TemporaryDirectory(prefix="mutation-", dir=directory) as temporary:
        for name, (patch, node) in MUTATIONS.items():
            xml = Path(temporary) / (name + ".xml")
            args = ["-o", "addopts=", "-q", "-p", "no:cacheprovider", "--junitxml=" + str(xml),
                    "tests/e2e/h2/" + node]
            script = patch + "\nimport pytest\nraise SystemExit(pytest.main(" + repr(args) + "))\n"
            result = subprocess.run([sys.executable, "-c", script], cwd=PROJECT, env=env,
                                    capture_output=True, text=True, timeout=90)
            suites = ET.parse(xml).getroot().findall("testsuite") if xml.exists() else []
            failures = sum(int(s.get("failures", 0)) for s in suites)
            errors = sum(int(s.get("errors", 0)) for s in suites)
            caught = result.returncode == 1 and failures > 0 and errors == 0
            results.append(dict(mutation=name, caught=caught, assertion_failures=failures, errors=errors))
            (directory / ("mutation-" + name + ".txt")).write_text(result.stdout + result.stderr)
    (directory / "mutation-evidence.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0 if all(r["caught"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
