"""SDK contract tests use the real FastAPI app through httpx's ASGI transport."""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))

from agentpal_sdk import (  # noqa: E402
    AgentClient,
    AgentPalError,
    AuthenticationError,
    ConflictError,
    NotConfiguredError,
    NotFoundError,
    OwnerClient,
    PermissionDeniedError,
    TransportError,
    ValidationError,
)
from test_platform_fixtures import ALL_PAY, OWNER_A, OWNER_B, build_world  # noqa: E402

BASE = "http://testserver"
PAY = {"currency": "USD", "merchant_name": "Example Hosting",
       "merchant_domain": "example.com", "purpose": "Monthly hosting plan"}


def owner_client(world, credential=OWNER_A):
    return OwnerClient(base_url=BASE, credential=credential,
                       transport=httpx.ASGITransport(app=world.app))


def agent_client(world, key):
    return AgentClient(base_url=BASE, credential=key,
                       transport=httpx.ASGITransport(app=world.app))


def test_sdk_owner_agent_sandbox_flow():
    world = build_world()

    async def run():
        async with owner_client(world) as owner:
            assert (await owner.health())["is_real_money"] is False
            registered = await owner.register_agent(name="sdk-bot")
            aid = registered["agent"]["agent_id"]
            await owner.set_agent_permissions(aid, ALL_PAY)
            assert (await owner.get_agent_permissions(aid))["capabilities"] == sorted(ALL_PAY)
            assert (await owner.get_agent(aid))["name"] == "sdk-bot"
            assert len((await owner.list_agents())["agents"]) == 1
            wallet = await owner.create_wallet(agent_id=aid, currency="USD",
                                                monthly_budget=Decimal("100.00"),
                                                per_transaction_limit="60.00")
            wid = wallet["wallet_id"]
            assert wallet["is_real_money"] is False
            assert (await owner.get_wallet(wid))["limits"]["monthly_budget"] == "100.00"
            assert len((await owner.list_wallets())["wallets"]) == 1
            await owner.update_wallet(wid, monthly_budget=Decimal("110.00"))
            assert (await owner.freeze_wallet(wid))["status"] == "frozen"
            await owner.unfreeze_wallet(wid)
            async with agent_client(world, registered["api_key"]) as agent:
                assert (await agent.get_me())["agent_id"] == aid
                assert (await agent.get_wallet())["is_real_money"] is False
                payment = await agent.create_payment_request(amount=Decimal("49.99"), **PAY)
                rid = payment["request_id"]
                assert payment["terms"]["amount"] == "49.99"
                assert (await agent.get_payment_request(rid))["request_id"] == rid
                assert len((await owner.list_approvals())["payment_requests"]) == 1
                assert len((await agent.list_payment_requests())["payment_requests"]) == 1
                await owner.approve_payment_request(rid, terms_sha256=payment["terms_sha256"])
                execution = await agent.execute_payment_request(rid)
                assert execution["status"] == "submitted_to_provider"
                assert execution["is_real_money"] is False
                assert len((await agent.list_transactions())["transactions"]) == 1
                assert len((await owner.list_wallet_transactions(wid))["transactions"]) == 1
                assert (await owner.get_reconciliation(rid))["provider_references"]

    asyncio.run(run())


def test_sdk_automatic_keys_are_unique_and_explicit_key_replays():
    world = build_world()
    _, key, _ = world.funded_agent(caps=ALL_PAY)

    async def run():
        async with agent_client(world, key) as agent:
            first = await agent.create_payment_request(amount="1.00", **PAY)
            second = await agent.create_payment_request(amount="1.00", **PAY)
            assert first["request_id"] != second["request_id"]
            third = await agent.create_payment_request(amount="1.00", **PAY,
                                                       idempotency_key="sdk-stable-key")
            replay = await agent.create_payment_request(amount="1.00", **PAY,
                                                        idempotency_key="sdk-stable-key")
            assert replay == third
            with pytest.raises(ConflictError) as exc:
                await agent.create_payment_request(amount="2.00", **PAY,
                                                   idempotency_key="sdk-stable-key")
            assert exc.value.code == "idempotency_key_reused"
            assert exc.value.idempotency_key == "sdk-stable-key"
        rows = world.wallet_store.query("SELECT idem_key FROM platform_idempotency")
        assert len(rows) == 3
        assert len({r["idem_key"] for r in rows}) == 3

    asyncio.run(run())


@pytest.mark.parametrize("bad", [1.0, 1, True, None])
def test_sdk_refuses_non_decimal_amounts_before_sending(bad):
    world = build_world()
    aid, key, _ = world.funded_agent(caps=ALL_PAY)

    async def run():
        async with agent_client(world, key) as agent, owner_client(world) as owner:
            with pytest.raises(TypeError):
                await agent.create_payment_request(amount=bad, **PAY)
            with pytest.raises(TypeError):
                await owner.create_wallet(agent_id=aid, currency="EUR", monthly_budget=bad,
                                          per_transaction_limit="10")
            with pytest.raises(TypeError):
                await owner.update_wallet("wal_" + "0" * 32, monthly_budget=bad)
        assert world.wallet_store.query("SELECT * FROM platform_idempotency") == []

    asyncio.run(run())


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e2", "-1", Decimal("NaN"),
                                Decimal("Infinity")])
def test_sdk_refuses_invalid_amounts(bad):
    world = build_world()
    _, key, _ = world.funded_agent(caps=ALL_PAY)

    async def run():
        async with agent_client(world, key) as agent:
            with pytest.raises(ValueError):
                await agent.create_payment_request(amount=bad, **PAY)

    asyncio.run(run())


def test_sdk_request_decision_then_reject():
    world = build_world()
    _, key, _ = world.funded_agent(caps=ALL_PAY)

    async def run():
        async with agent_client(world, key) as agent, owner_client(world) as owner:
            pay = await agent.create_payment_request(amount="1", **PAY,
                                                     request_owner_decision=False)
            assert pay["decision_requested"] is False
            rid = pay["request_id"]
            assert (await agent.request_owner_decision(rid))["decision_requested"] is True
            assert (await owner.reject_payment_request(rid))["status"] == "rejected"

    asyncio.run(run())


def test_sdk_credentials_rotation_revocation_and_agent_status():
    world = build_world()

    async def run():
        async with owner_client(world) as owner:
            reg = await owner.register_agent(name="bot")
            aid = reg["agent"]["agent_id"]
            assert (await owner.suspend_agent(aid))["status"] == "suspended"
            async with agent_client(world, reg["api_key"]) as original:
                with pytest.raises(AuthenticationError):
                    await original.get_me()
                await owner.reactivate_agent(aid)
                assert (await original.get_me())["agent_id"] == aid
                rotated = await owner.rotate_agent_credential(aid)
                with pytest.raises(AuthenticationError):
                    await original.get_me()
            async with agent_client(world, rotated["api_key"]) as new:
                assert (await new.get_me())["agent_id"] == aid
                await owner.revoke_agent_credential(aid, rotated["key_id"])
                with pytest.raises(AuthenticationError):
                    await new.get_me()

    asyncio.run(run())


def test_sdk_typed_api_errors_and_secret_free_messages():
    world = build_world(configured=False)
    _, key, _ = world.funded_agent(caps=ALL_PAY)

    async def run():
        async with agent_client(world, key) as agent, owner_client(world) as owner:
            pay = await agent.create_payment_request(amount="1", **PAY)
            with pytest.raises(NotConfiguredError) as exc:
                await owner.approve_payment_request(pay["request_id"],
                                                   terms_sha256=pay["terms_sha256"])
            assert exc.value.status_code == 503
            with pytest.raises(ValidationError) as exc:
                await agent.create_payment_request(amount="1", **PAY, unknown=key)
            assert key not in str(exc.value)
            assert exc.value.status_code == 422
            with pytest.raises(NotFoundError):
                await owner.get_payment_request("pay_" + "0" * 32)
        async with owner_client(world, OWNER_B) as other:
            with pytest.raises(NotFoundError):
                await other.get_payment_request(pay["request_id"])
        world.grant(pay["agent_id"], [])
        async with agent_client(world, key) as agent:
            with pytest.raises(PermissionDeniedError):
                await agent.get_wallet()
        async with agent_client(world, "apk_invalid_secret") as agent:
            with pytest.raises(AuthenticationError) as exc:
                await agent.get_me()
            assert "secret" not in str(exc.value)
            assert isinstance(exc.value, AgentPalError)

    asyncio.run(run())


@pytest.mark.parametrize("cls,credential", [(OwnerClient, "apk_wrong_secret"),
                                           (AgentClient, OWNER_A)])
def test_sdk_refuses_crossed_credential_types(cls, credential):
    with pytest.raises(ValueError, match="wrong credential type"):
        cls(base_url=BASE, credential=credential)


def test_sdk_transport_errors_expose_retry_key_and_do_not_retry():
    calls = []

    async def unavailable(request):
        calls.append(request)
        raise httpx.ConnectError("sensitive upstream details", request=request)

    async def run():
        async with AgentClient(base_url=BASE, credential="apk_invalid_secret",
                               transport=httpx.MockTransport(unavailable)) as agent:
            with pytest.raises(TransportError) as exc:
                await agent.create_payment_request(amount="1", **PAY)
            assert exc.value.code == "transport_error"
            assert len(exc.value.idempotency_key) == 32
            assert calls[0].headers["idempotency-key"] == exc.value.idempotency_key
            assert "sensitive" not in str(exc.value)
        assert len(calls) == 1

    asyncio.run(run())
