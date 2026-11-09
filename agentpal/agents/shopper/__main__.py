"""python -m agentpal.agents.shopper 'Buy an office mouse' --budget 40"""

import argparse
import json
import os
import tempfile

from .core import Shopper, ShopperError
from .journal import Journal, run_errand


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentPal Shopper — sandbox only")
    parser.add_argument("task")
    parser.add_argument("--budget", required=True, help="Positive USD decimal string")
    parser.add_argument("--errand-id", help="Reuse with identical terms after an interrupted request")
    parser.add_argument("--merchant-paypal-email", help="Sandbox merchant email named in the task")
    parser.add_argument("--journal", default=os.environ.get(
        "SHOPPER_JOURNAL", os.path.join(tempfile.gettempdir(), "agentpal-shopper", "errands.sqlite3")))
    parser.add_argument("--wait-seconds", type=float, default=3600)
    args = parser.parse_args()
    shopper = journal = None
    try:
        shopper = Shopper(emit=lambda event: print(json.dumps(event), flush=True))
        journal = Journal(args.journal)
        result = run_errand(shopper, journal, args.task, args.budget, errand_id=args.errand_id,
                            merchant_paypal_email=args.merchant_paypal_email,
                            wait_seconds=max(0, args.wait_seconds))
        print(json.dumps(result), flush=True)
        return 0
    except ShopperError as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    except KeyboardInterrupt:
        print('{"message":"Stopped. Resume using the same errand ID."}', flush=True)
        return 130
    except Exception:
        # No remote exception, credential, task text, or traceback reaches the terminal.
        print('{"error":"shopper_unavailable"}', flush=True)
        return 1
    finally:
        if shopper:
            shopper.close()
        if journal:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
