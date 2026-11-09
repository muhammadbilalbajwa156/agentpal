"""Run from agentpal/: python -m apimatic check|validate|generate|smoke|sdk-smoke."""

import argparse
import sys

from .workflow import WorkflowError, check_spec, remote


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "validate", "generate", "smoke", "sdk-smoke"])
    parser.add_argument("--node", default="node", help="Node 24+ executable")
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            print("OpenAPI SHA-256: " + check_spec())
        elif args.action in {"validate", "generate"}:
            remote(args.action, node=args.node)
            print("APIMatic " + args.action + " succeeded; sanitized report in apimatic/reports/")
        else:
            from .smoke import run_smoke
            run_smoke(generated=args.action == "smoke")
            print("Live local SDK smoke passed (local simulator; no verified completion)")
        return 0
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Generated exceptions and pip failures may include request credentials.
        print("SDK smoke failed; no raw credentials or subprocess output emitted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
