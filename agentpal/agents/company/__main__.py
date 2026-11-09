"""python -m agentpal.agents.company {cast,provision,run} — PayPal SANDBOX demo only."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from ..common import AgentError, Redactor
from .provision import provision
from .runner import run_company
from .scenarios import COMPANIES


def printer(as_json: bool, redact: Redactor):
    def emit(event: dict):
        event = redact.clean(event)
        if as_json:
            print(json.dumps(event), flush=True)
            return
        who = event.get("agent") or event.get("company") or "AgentPal"
        details = " ".join(f"{k}={json.dumps(v) if isinstance(v, (dict, list)) else v}"
                           for k, v in event.items() if k not in {"agent", "message", "company"})
        print(f"[{who}] {event.get('message', '')}" + (f"  ({details})" if details else ""),
              flush=True)
    return emit


def cast() -> None:
    for company in COMPANIES.values():
        print(f"{company.name} ({company.company_id}): {company.tagline}")
        for agent in company.agents:
            print(f"  {agent.name} [{company.slug(agent)}] monthly {agent.monthly_budget} USD, "
                  f"per transaction {agent.per_transaction_limit} USD; "
                  f"capabilities: {', '.join(agent.capabilities)}")
            for errand in agent.errands:
                print(f"    - {errand.errand_id} ({errand.kind}, budget {errand.budget}): "
                      f"{errand.goal}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentpal.agents.company",
                                     description="AgentPal autonomous companies — sandbox only")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cast", help="show companies, agents, budgets and errands (no network)")
    p = sub.add_parser("provision", help="OWNER: create agents and wallets (needs "
                                         "AGENTPAL_OWNER_TOKEN in the environment)")
    p.add_argument("--base-url", required=True)
    p.add_argument("--keys-out", required=True, help="private 0600 JSON file for agent keys")
    p.add_argument("--company", choices=["all", *COMPANIES], default="all")
    r = sub.add_parser("run", help="run a company's business day")
    r.add_argument("--company", choices=list(COMPANIES), required=True)
    r.add_argument("--agents", help="comma-separated agent keys, e.g. atlas,forge")
    r.add_argument("--dry-run", action="store_true", help="plan only; no AgentPal writes")
    r.add_argument("--wait", action="store_true", help="wait for owner decisions and execute "
                                                       "approved requests in the sandbox")
    r.add_argument("--wait-seconds", type=float, default=3600)
    r.add_argument("--day", help="business day YYYY-MM-DD (default: today, UTC)")
    r.add_argument("--planner", choices=["llm", "scripted"],
                   default=os.environ.get("COMPANY_PLANNER", "llm"))
    r.add_argument("--replan", action="store_true",
                   help="allow planning again for errands whose planning stopped safely")
    r.add_argument("--journal", default=os.environ.get("COMPANY_JOURNAL", os.path.join(
        tempfile.gettempdir(), "agentpal-company", "errands.sqlite3")))
    r.add_argument("--base-url", help="AgentPal origin (default: AGENTPAL_BASE_URL)")
    for command in (p, r):
        command.add_argument("--json", action="store_true", help="JSON lines output")
    args = parser.parse_args(argv)
    if args.command == "cast":
        cast()
        return 0
    env = dict(os.environ)
    redact = Redactor(env, extra=[env.get("COMPANY_AGENT_KEYS", "")])
    emit = printer(args.json, redact)
    try:
        if args.command == "provision":
            companies = (list(COMPANIES.values()) if args.company == "all"
                         else [COMPANIES[args.company]])
            result = provision(companies, base=args.base_url,
                               owner_token=env.get("AGENTPAL_OWNER_TOKEN", ""),
                               keys_out=args.keys_out, emit=emit)
            emit({"message": "Provisioning complete. Agent keys are in the private keys file "
                             "(never printed).", "keys_file": result["keys_file"]})
            return 0
        if args.base_url:
            env["AGENTPAL_BASE_URL"] = args.base_url
        result = run_company(args.company, env=env, journal_path=args.journal, day=args.day,
                             agents=args.agents.split(",") if args.agents else None,
                             dry_run=args.dry_run,
                             wait_seconds=max(0.0, args.wait_seconds) if args.wait else 0,
                             replan=args.replan, planner=args.planner, emit=emit)
        stopped = [r for r in result["results"] if r["status"] in {"stopped_safely", "skipped"}]
        return 2 if stopped and len(stopped) == len(result["results"]) else 0
    except AgentError as exc:
        emit({"message": "Stopped safely.", "error": str(exc)})
        return 1
    except KeyError:
        emit({"message": "Stopped safely.", "error": "unknown_agent"})
        return 1
    except KeyboardInterrupt:
        emit({"message": "Interrupted. Rerun the same day: the journal resumes safely."})
        return 130
    except Exception:
        # No remote exception, credential or traceback reaches the terminal.
        emit({"message": "Stopped safely.", "error": "company_runner_unavailable"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
