"""Channel3 market-check demo (PayPal AI Hackathon sponsor integration).

  python -m agentpal.demo.market_check "Logitech MX Master 3S" --amount 250 \\
      --merchant-domain shady-deals.example            # one live check (needs the key)
  python -m agentpal.demo.market_check --end-to-end    # full owner/agent story via /v1
  python -m agentpal.demo.market_check --end-to-end --offline   # recorded real sample

Live mode reads CHANNEL3_API_KEY from the environment and uses about 1 search credit
(identical queries are cached; the owner's refresh uses Channel3's free product
endpoint). Offline mode replays a sanitized response recorded from the live API on
2026-11-01; it never claims to be live. No money moves: AgentPal is sandbox-only, and
the market check is advisory evidence for the owner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from ..api import create_app
from ..auth import TokenRegistry
from ..config import Settings
from ..intelligence.market_check import MarketChecker
from ..platform.wiring import build_platform
from ..tools.channel3 import Channel3Client

SAMPLE = Path(__file__).with_name("channel3_sample_search.json")
QUERY = "Logitech MX Master 3S wireless mouse"


def _offline_client() -> Channel3Client:
    raw = SAMPLE.read_bytes()
    product = json.loads(raw)["products"][0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            return httpx.Response(200, content=raw)
        return httpx.Response(200, json=product)
    return Channel3Client("offline-demo", client=httpx.Client(
        transport=httpx.MockTransport(handler)))


def _summary(label: str, mc: dict) -> list[str]:
    lines = [f"--- {label}: advisory_level = {mc['advisory_level'].upper()}"]
    if mc.get("product"):
        p = mc["product"]
        lines.append(f"    product: {p['title']} (Channel3 id {p['id']}, match {p['match_confidence']})")
    if mc.get("market"):
        m = mc["market"]
        lines.append(f"    market ({m['offers_compared']} new in-stock offers): low {m['low']}, "
                     f"median {m['median']}, high {m['high']} {m['currency']}; claimed is "
                     f"{m['claimed_vs_median']}x the median")
    for s in mc["signals"]:
        lines.append(f"    [{s['severity']}] {s['code']}: {s['message']}")
    for d in mc["better_deals"]:
        sale = f" (on sale, was {d['was_price']})" if d.get("on_sale") else ""
        lines.append(f"    deal: {d['retailer']} {d['price']} {d['currency']}{sale}, save "
                     f"{d['you_save']} ({d['you_save_pct']}%)")
    if mc["better_deals"]:
        lines.append(f"    {mc['disclosure']}")
        lines.append(f"    {mc['verify_notice']}")
    return lines


async def end_to_end(checker: MarketChecker) -> dict:
    with TemporaryDirectory(prefix="agentpal-channel3-demo-") as directory:
        settings = Settings(db_path=str(Path(directory) / "risk.db"), auth_mode="required",
                            analyzer_chain=["heuristic"], allowed_hosts=["127.0.0.1"],
                            log_level="ERROR")
        deps = build_platform(settings=settings, environ={})
        deps.market_checker = checker
        try:
            identity = deps.owner_authenticator
            identity.create_owner("demo_owner")
            owner_token, _ = identity.issue_token("demo_owner")
            owner = {"Authorization": "Bearer " + owner_token}
            app = create_app(service=deps.risk_service, settings=settings,
                             registry=TokenRegistry(), platform=deps)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://127.0.0.1") as client:
                async def call(method, path, headers=owner, expected=200, **kw):
                    r = await client.request(method, path, headers=headers, **kw)
                    if r.status_code != expected:
                        raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
                    return r.json()
                agent = await call("POST", "/v1/agents", expected=201, json={
                    "name": "Office supplies agent", "capabilities": [
                        "wallet.read", "payment.prepare", "owner.request_decision"]})
                agent_h = {"Authorization": "Bearer " + agent["api_key"]}
                await call("POST", "/v1/wallets", expected=201, json={
                    "agent_id": agent["agent"]["agent_id"], "currency": "USD",
                    "monthly_budget": "1000.00", "per_transaction_limit": "500.00"})

                async def request(amount, merchant, domain):
                    return await call("POST", "/v1/payment-requests", expected=201,
                                      headers={**agent_h, "Idempotency-Key": "demo-" + uuid.uuid4().hex},
                                      json={"amount": amount, "currency": "USD",
                                            "merchant_name": merchant, "merchant_domain": domain,
                                            "purpose": "Wireless mouse for the office",
                                            "product_query": QUERY})
                risky = await request("250.00", "Shady Deals", "shady-deals.example")
                fair = await request("99.99", "Office Depot", "officedepot.com")
                queue = await call("GET", "/v1/approvals")
                refreshed = await call(
                    "POST", f"/v1/payment-requests/{risky['request_id']}/market-check")
                return {"risky": risky, "fair": fair, "refreshed": refreshed,
                        "approval_queue": [p["request_id"] for p in queue["payment_requests"]],
                        "credits": dict(checker.client.calls)}
        finally:
            deps.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("product", nargs="?", default=QUERY)
    ap.add_argument("--amount", default="250.00")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--merchant-domain", default="shady-deals.example")
    ap.add_argument("--product-url")
    ap.add_argument("--end-to-end", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="replay the recorded real response instead of calling Channel3")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.offline:
        client, mode = _offline_client(), "OFFLINE (recorded real Channel3 response)"
    elif os.environ.get("CHANNEL3_API_KEY", "").strip():
        client, mode = Channel3Client(), "LIVE Channel3 API"
    else:
        print("BLOCKED BY CREDENTIALS: set CHANNEL3_API_KEY, or use --offline", file=sys.stderr)
        return 2
    checker = MarketChecker(client)
    if a.end_to_end:
        out = asyncio.run(end_to_end(checker))
        if a.json:
            print(json.dumps(out, indent=1, sort_keys=True))
            return 0
        print(f"AgentPal x Channel3 end-to-end demo [{mode}] (sandbox only, no real money)")
        print("Agent asks to pay 250.00 USD at shady-deals.example for: " + QUERY)
        print("\n".join(_summary("owner sees, BEFORE approving", out["risky"]["market_check"])))
        print("\nAgent asks to pay 99.99 USD at officedepot.com for the same product")
        print("\n".join(_summary("owner sees", out["fair"]["market_check"])))
        r = out["refreshed"]
        print(f"\nOwner refreshed the risky request via Channel3's free product endpoint at "
              f"{r['checked_at']}: {r['advisory_level']}")
        print(f"Both requests remain awaiting the owner's decision: "
              f"{len(out['approval_queue'])} in queue; payment status "
              f"{out['risky']['status']}. Channel3 calls: {out['credits']}")
        return 0
    mc = checker.check(amount=Decimal(a.amount), currency=a.currency.upper(),
                       merchant_domain=a.merchant_domain, product_query=a.product,
                       product_url=a.product_url)
    if a.json:
        print(json.dumps(mc, indent=1, sort_keys=True))
    else:
        print(f"[{mode}]")
        print("\n".join(_summary(f"{a.amount} {a.currency} at {a.merchant_domain}", mc)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
