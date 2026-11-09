"""Command line: python -m agentpal <command> [options]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import PACKAGE_ROOT, get_settings

DATA_DIR = PACKAGE_ROOT / "data"
REPORTS_DIR = PACKAGE_ROOT / "reports"


def _cmd_data(args: argparse.Namespace) -> None:
    from .dataset import write_dataset

    manifest = write_dataset(args.out, seed=args.seed)
    print(json.dumps({k: manifest[k] for k in ("seed", "total_examples", "counts", "sha256")},
                     indent=2))


def _cmd_train(args: argparse.Namespace) -> None:
    from .evaluation import fit_baseline

    artifact = fit_baseline(args.data, args.out, seed=args.seed)
    print(json.dumps({k: artifact[k] for k in ("version", "threshold", "selected_on",
                                               "trained_on")}, indent=2))
    print(f"saved {args.out}")


def _cmd_evaluate(args: argparse.Namespace) -> None:
    from .evaluation import evaluate

    settings = get_settings()
    report = evaluate(args.data, args.ml_model or settings.ml_model_path, args.report,
                      split=args.split,
                      llm_model_path=settings.llamacpp_model_path if args.llm_sample else None,
                      llm_sample=args.llm_sample, seed=args.seed)
    print(report["notice"])
    if report.get("split_status", "in_use") != "in_use":
        print(f"WARNING: split '{args.split}' is {report['split_status']}: do not use these "
              "numbers to tune thresholds or select models.")
    for name, a in report["approaches"].items():
        b = a["binary"]
        print(f"{name:28s} n={b['n']:4d} P={b['precision']} R={b['recall']} F1={b['f1']} "
              f"FPR={b['false_positive_rate']} FNR={b['false_negative_rate']}")
        if "operational" in a:
            o = a["operational"]
            print(f"{'':28s} missed risky {o['risky_missed']} ({o['risky_miss_rate']}) | "
                  f"legit flagged {o['legitimate_flagged']} ({o['legitimate_flag_rate']}) | "
                  f"review rate {o['review_rate']} | block rate {o['block_rate']}")
        print(f"{'':28s} latency p50={a['latency_ms'].get('p50')}ms "
              f"p95={a['latency_ms'].get('p95')}ms")
    print(f"report: {args.report}")


def _cmd_assess(args: argparse.Namespace) -> None:
    from .schemas import PaymentRequest
    from .service import RiskService

    settings = get_settings()
    if args.db:
        settings = settings.model_copy(update={"db_path": args.db})
    service = RiskService.from_settings(settings)
    for path in args.files:
        req = PaymentRequest.model_validate_json(Path(path).read_text(encoding="utf-8"))
        a = service.assess(req)
        if args.full:
            print(a.model_dump_json(indent=2))
        else:
            print(f"{Path(path).name:40s} score={a.risk_score:3d} {a.risk_category:8s} "
                  f"{a.recommendation:16s} backend={a.model.backend} "
                  f"codes={[s.code for s in a.signals if s.source.value == 'rule']}")


def _cmd_pin_model(args: argparse.Namespace) -> None:
    from .integrity import pin

    manifest = args.manifest or get_settings().model_manifest_path
    digest = pin(manifest, args.backend, args.path, source=args.source, note=args.note)
    print(f"pinned {args.backend} sha256={digest} in {manifest}")
    print("A matching digest proves only byte-identity with this file, not model safety.")


def _cmd_bench(args: argparse.Namespace) -> None:
    from .benchmark import evaluate_benchmark
    from .config import AUTH_DISABLED_DEV, Settings

    # The benchmark replays episodes through an in-process TestClient (no socket), posting
    # as many agents; authentication is not what it measures. Production refuses this.
    settings = Settings(analyzer_chain=["ml", "heuristic"], db_path=":memory:",
                        auth_mode=AUTH_DISABLED_DEV)
    r = evaluate_benchmark(args.dir, args.split, args.report, settings=settings)
    print(f"{r['benchmark']} split={r['split']} items={r['items']} "
          f"independent={r['independent_items']}")
    print(r["notice"])
    if r["cases"]:
        for name, m in r["cases"]["metrics"].items():
            print(f"  cases {name:16s} n={m['n']} P={m['precision']} R={m['recall']} "
                  f"FPR={m['false_positive_rate']} recall95={m['ci95']['recall']}")
    if r["episodes"]:
        for name, e in r["episodes"].items():
            print(f"  episodes {name:13s} steps {e['steps_passed']}/{e['steps']}, "
                  f"episodes fully passed {e['episodes_fully_passed']}/{e['episodes']}")
    print(f"report: {args.report}")


def _cmd_bench_lock(args: argparse.Namespace) -> None:
    from .benchmark import lock

    print(json.dumps(lock(args.dir, args.note), indent=2))


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _bootstrap_hosted_owner(settings) -> None:
    """Hosted mode: create the first owner from secret env vars set on the host
    (AGENTPAL_BOOTSTRAP_OWNER_ID, AGENTPAL_BOOTSTRAP_OWNER_TOKEN). Idempotent; the token is
    never printed or logged, only its salted hash is stored."""
    import os
    from datetime import timedelta

    try:
        days = int(os.environ.get("AGENTPAL_OWNER_TOKEN_DAYS", "30"))
        if not 1 <= days <= 90:
            raise ValueError
    except ValueError:
        sys.exit("refusing to start: AGENTPAL_OWNER_TOKEN_DAYS must be an integer 1..90")
    owners = []
    for prefix, display_name in (("BOOTSTRAP", "Hosted owner"), ("JUDGE", "Judge workspace")):
        owner_id = os.environ.get(f"AGENTPAL_{prefix}_OWNER_ID", "").strip()
        token = os.environ.get(f"AGENTPAL_{prefix}_OWNER_TOKEN", "").strip()
        if owner_id and token:
            owners.append((owner_id, token, display_name))
    if not owners:
        return
    from .identity import OwnerIdentityService
    from .identity.service import IdentityError
    from .identity.store import IdentityStore

    path = os.environ.get("AGENTPAL_IDENTITY_DB_PATH") or settings.db_path + ".identity.db"
    identity = OwnerIdentityService(IdentityStore(path))
    try:
        for owner_id, token, display_name in owners:
            try:
                identity.create_owner(owner_id, display_name=display_name)
            except IdentityError as exc:
                if str(exc) != "OWNER_EXISTS":
                    sys.exit("refusing to start: owner bootstrap failed")
            try:
                identity.import_token(owner_id, token, ttl=timedelta(days=days))
            except IdentityError:
                sys.exit("refusing to start: owner token bootstrap failed")
    finally:
        identity.store.close()


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn
    from .api import uvicorn_log_config

    from .config import UnsafeConfiguration
    from .spending import load_spend_policy

    settings = get_settings()
    overrides = {key: value for key, value in {
        "tls_certfile": args.ssl_certfile, "tls_keyfile": args.ssl_keyfile}.items()
        if value is not None}
    settings = settings.model_copy(update=overrides)
    if settings.public_webhook_enabled and not args.platform:
        sys.exit("refusing to start: public webhook exposure requires --platform")
    try:  # SEC-04: refuse unsafe combinations before binding anything
        warnings = settings.check_runtime(
            host=args.host,
            spend_policy_configuration=load_spend_policy(
                settings.spend_policy_path).configuration)
    except UnsafeConfiguration as exc:
        sys.exit(f"refusing to start: {exc}")
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if args.platform:
        from .api import create_app
        from .platform.wiring import build_platform

        if settings.hosted_proxy == "render":
            _bootstrap_hosted_owner(settings)
        platform = build_platform(settings=settings)
        try:
            print(f"Dashboard: http{'s' if settings.tls_certfile else ''}://"
                  f"{'[' + args.host + ']' if ':' in args.host else args.host}:{args.port}/app/")
            code = platform.setup.console_code()
            if code:
                print(f"One-time setup code: {code}", flush=True)
            platform.risk_service.warm_up()
            app = create_app(service=platform.risk_service, settings=settings, platform=platform)
            uvicorn.run(app, host=args.host, port=args.port,
                        proxy_headers=False, server_header=False, log_config=uvicorn_log_config(),
                        **settings.tls_options())
        finally:
            platform.close()
        return
    # Never derive client identity from X-Forwarded-For / Forwarded headers (final owner
    # decision 8): the loopback check uses the socket peer address only.
    uvicorn.run("agentpal.api:create_app", factory=True, host=args.host, port=args.port,
                proxy_headers=False, server_header=False, log_config=uvicorn_log_config(),
                **settings.tls_options())


def _cmd_start(args: argparse.Namespace) -> None:
    """Convenient local platform startup; all serve security checks still apply."""
    import os

    data_dir = Path(os.environ.get("AGENTPAL_DATA_DIR") or Path.cwd() / "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.setdefault("AGENTPAL_DATA_DIR", str(data_dir))
    os.environ.setdefault("AGENTPAL_DB_PATH", str(data_dir / "agentpal.db"))
    get_settings.cache_clear()
    args.platform = True
    _cmd_serve(args)


def _cmd_new_owner(args: argparse.Namespace) -> None:
    """Bootstrap a human OWNER for the /v1 platform and print ONE owner token (apo_...).

    Uses the same identity database `serve --platform` uses (AGENTPAL_IDENTITY_DB_PATH, or
    the risk database path + ".identity.db"). The plaintext token is shown once and never
    stored; only its hash is kept. Run it on the server machine (loopback operator)."""
    import os
    from datetime import timedelta

    from .identity import OwnerIdentityService
    from .identity.service import IdentityError
    from .identity.store import IdentityStore

    settings = get_settings()
    path = os.environ.get("AGENTPAL_IDENTITY_DB_PATH") or (
        ":memory:" if settings.db_path == ":memory:" else settings.db_path + ".identity.db")
    if path == ":memory:":
        sys.exit("refusing: an in-memory identity database would lose the owner at exit")
    identity = OwnerIdentityService(IdentityStore(path))
    try:
        try:
            identity.create_owner(args.owner_id, display_name=args.name or "")
            created = True
        except IdentityError as exc:
            if str(exc) != "OWNER_EXISTS":
                sys.exit(f"refusing: {exc}")
            created = False
        token, info = identity.issue_token(args.owner_id, ttl=timedelta(days=args.days),
                                           label=args.label)
    finally:
        identity.store.close()
    print(token)
    print(json.dumps({"owner_id": args.owner_id, "created": created,
                      "credential_id": info.credential_id, "expires_at": str(info.expires_at),
                      "identity_db": path}), file=sys.stderr)


def _cmd_new_token(args: argparse.Namespace) -> None:
    """Create an API token. The plaintext is printed ONCE and never stored; only its
    SHA-256 entry is printed (and, with --append, added to the token file)."""
    from .auth import TokenEntry, TokenRegistry, generate_token, hash_token

    token = generate_token()
    entry = TokenEntry(id=args.id, role="operator" if args.operator else "agent",
                       agent_id=None if args.operator else args.agent, sha256=hash_token(token))
    if args.append:
        path = Path(args.append)
        current = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                   else {"tokens": []})
        registry = TokenRegistry.model_validate(
            {"tokens": [*current.get("tokens", []), entry.model_dump(exclude_none=True)]})
        path.write_text(json.dumps(registry.model_dump(exclude_none=True), indent=2) + "\n",
                        encoding="utf-8")
        print(f"appended hash entry {entry.id!r} to {path}", file=sys.stderr)
    else:
        print(json.dumps(entry.model_dump(exclude_none=True)))
    print(f"TOKEN (shown once, store it in the agent's secret store): {token}",
          file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="agentpal", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("data", help="generate the synthetic dataset")
    d.add_argument("--out", default=str(DATA_DIR))
    d.add_argument("--seed", type=int, default=20260)
    d.set_defaults(fn=_cmd_data)

    t = sub.add_parser("train", help="fit the logistic-regression baseline")
    t.add_argument("--data", default=str(DATA_DIR))
    t.add_argument("--out", default=get_settings().ml_model_path)
    t.add_argument("--seed", type=int, default=0)
    t.set_defaults(fn=_cmd_train)

    e = sub.add_parser("evaluate", help="evaluate approaches on a held-out split")
    e.add_argument("--data", default=str(DATA_DIR))
    # the test split is RETIRED for selection; it stays available as an explicit choice
    e.add_argument("--split", default="validation", choices=["train", "validation", "test"])
    e.add_argument("--ml-model", default=None)
    e.add_argument("--report", default=str(REPORTS_DIR / "evaluation_report.json"))
    e.add_argument("--llm-sample", type=int, default=0,
                   help="also evaluate the local LLM on a stratified sample of N examples")
    e.add_argument("--seed", type=int, default=0)
    e.set_defaults(fn=_cmd_evaluate)

    a = sub.add_parser("assess", help="assess request JSON files")
    a.add_argument("files", nargs="+")
    a.add_argument("--db", default=":memory:", help="SQLite path (default: in-memory)")
    a.add_argument("--full", action="store_true", help="print the full assessment JSON")
    a.set_defaults(fn=_cmd_assess)

    pm = sub.add_parser("pin-model", help="record a model artifact's SHA-256 in the "
                                          "trusted manifest (operator action)")
    pm.add_argument("backend", choices=["ml", "llamacpp", "ollama"])
    pm.add_argument("path")
    pm.add_argument("--manifest", default=None)
    pm.add_argument("--source", default="operator", help="where the digest was obtained")
    pm.add_argument("--note", default="")
    pm.set_defaults(fn=_cmd_pin_model)

    b = sub.add_parser("bench", help="evaluate AgentPal-Bench (single cases + API episodes)")
    b.add_argument("--dir", default=str(PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v1"))
    b.add_argument("--split", default="dev", choices=["dev", "locked"])
    b.add_argument("--report", default=str(REPORTS_DIR / "bench_report.json"))
    b.set_defaults(fn=_cmd_bench)

    bl = sub.add_parser("bench-lock", help="freeze benchmark files (SHA-256) before any "
                                           "model or threshold selection")
    bl.add_argument("--dir", default=str(PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v1"))
    bl.add_argument("--note", default="")
    bl.set_defaults(fn=_cmd_bench_lock)

    nt = sub.add_parser("new-token", help="create an API token (prints the token once; "
                                          "stores only its SHA-256)")
    who = nt.add_mutually_exclusive_group(required=True)
    who.add_argument("--agent", help="the one agent_id this token authenticates")
    who.add_argument("--operator", action="store_true",
                     help="operator token: may read any assessment, cannot submit")
    nt.add_argument("--id", required=True, help="non-secret label used in audit records")
    nt.add_argument("--append", default=None, help="add the hash entry to this token file")
    nt.set_defaults(fn=_cmd_new_token)

    no = sub.add_parser("new-owner", help="create a /v1 owner (if new) and print one owner "
                                           "token (apo_...) once; only its hash is stored")
    no.add_argument("owner_id", help="owner id, e.g. alice")
    no.add_argument("--name", default="", help="display name")
    no.add_argument("--days", type=int, default=30, help="token lifetime in days (1..90, max 90)")
    no.add_argument("--label", default="cli", help="non-secret token label")
    no.set_defaults(fn=_cmd_new_owner)

    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--ssl-certfile", default=None, help="TLS certificate chain PEM")
    s.add_argument("--ssl-keyfile", default=None, help="TLS private key PEM")
    s.add_argument("--platform", action="store_true", help="mount the strict /v1 sandbox platform")
    s.set_defaults(fn=_cmd_serve)

    start = sub.add_parser("start", help="start the local platform and dashboard (data in ./data)")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8000)
    start.add_argument("--ssl-certfile", default=None)
    start.add_argument("--ssl-keyfile", default=None)
    start.set_defaults(fn=_cmd_start)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
