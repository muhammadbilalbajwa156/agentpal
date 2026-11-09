"""Owner identity bootstrap CLI.

    python -m agentpal.identity --db identity.db create-org --org-id acme --name "Acme"
    python -m agentpal.identity --db identity.db create-owner --owner-id owner_alice \
        [--org-id acme] [--display-name Alice]
    python -m agentpal.identity --db identity.db issue-token --owner-id owner_alice \
        [--ttl-days 30] [--label laptop]
    python -m agentpal.identity --db identity.db list-tokens --owner-id owner_alice
    python -m agentpal.identity --db identity.db revoke-token --credential-id <id>

`--db` defaults to $AGENTPAL_IDENTITY_DB_PATH. `issue-token` prints the token ONCE to
stdout; only its hash is stored, so it cannot be shown again.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import timedelta

from ..auth import redact_sensitive_text
from ..infra_contracts import OwnerAuthError
from .service import IdentityError, OwnerIdentityService
from .store import IdentityStore


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agentpal.identity")
    p.add_argument("--db", default=os.environ.get("AGENTPAL_IDENTITY_DB_PATH"))
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("create-org")
    o.add_argument("--org-id", required=True)
    o.add_argument("--name", required=True)
    w = sub.add_parser("create-owner")
    w.add_argument("--owner-id", required=True)
    w.add_argument("--org-id")
    w.add_argument("--display-name", default="")
    i = sub.add_parser("issue-token")
    i.add_argument("--owner-id", required=True)
    i.add_argument("--ttl-days", type=int, default=30)
    i.add_argument("--label", default="")
    lt = sub.add_parser("list-tokens")
    lt.add_argument("--owner-id", required=True)
    r = sub.add_parser("revoke-token")
    r.add_argument("--credential-id", required=True)
    for command in ("enroll-totp", "confirm-totp", "step-up", "remove-totp"):
        sub.add_parser(command, help="TOTP operation; credentials/codes use hidden prompts")
    args = p.parse_args(argv)
    if not args.db:
        p.error("--db or AGENTPAL_IDENTITY_DB_PATH is required")
    svc = OwnerIdentityService(IdentityStore(args.db))
    try:
        if args.cmd in ("enroll-totp", "confirm-totp", "step-up", "remove-totp"):
            token = getpass.getpass("Owner credential: ")
            if args.cmd == "enroll-totp":
                enrollment = svc.begin_totp_enrollment(token)
                print("TOTP seed shown once; provision your authenticator securely.", file=sys.stderr)
                print(enrollment.secret)
            elif args.cmd == "confirm-totp":
                svc.confirm_totp_enrollment(token, getpass.getpass("Authenticator code: "))
                print("TOTP enrollment confirmed.")
            elif args.cmd == "step-up":
                elevated, info = svc.step_up(token, getpass.getpass("Authenticator code: "))
                print(f"Step-up token shown once; expires={info.expires_at.isoformat()}", file=sys.stderr)
                print(elevated)
            else:
                svc.remove_totp(token)
                print("TOTP factor removed; step-up sessions invalidated.")
        elif args.cmd == "create-org":
            svc.create_org(args.org_id, args.name)
            print(f"created org {args.org_id}")
        elif args.cmd == "create-owner":
            svc.create_owner(args.owner_id, args.display_name, args.org_id)
            print(f"created owner {args.owner_id}")
        elif args.cmd == "issue-token":
            token, info = svc.issue_token(args.owner_id, timedelta(days=args.ttl_days),
                                          args.label)
            print(f"credential_id={info.credential_id} expires_at="
                  f"{info.expires_at.isoformat()}", file=sys.stderr)
            print("This token is shown once; store it securely.", file=sys.stderr)
            print(token)
        elif args.cmd == "list-tokens":
            for t in svc.list_tokens(args.owner_id):
                state = "revoked" if t.revoked_at else "active"
                print(f"{t.credential_id}\t{state}\texpires={t.expires_at.isoformat()}"
                      f"\tlabel={redact_sensitive_text(t.label)}")
        elif args.cmd == "revoke-token":
            svc.revoke_token(args.credential_id)
            print(f"revoked {args.credential_id}")
    except (OwnerAuthError, IdentityError, ValueError):
        print("error: identity_operation_refused", file=sys.stderr)
        return 2
    finally:
        svc.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
