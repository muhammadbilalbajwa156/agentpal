"""Payment requests through the developer API.

What this module does itself:
* agent-side authorization through the registry-backed `PermissionChecker` (deny by
  default, every decision audited in `wallet_permission_audit`);
* wallet checks (one ACTIVE wallet for the currency);
* `Idempotency-Key` handling (same key + same body = same request; different body = 409);
* owner scoping for every owner read/decision (unknown and not-yours are both 404).

What it never does itself:
* assess, reserve or validate budgets: `WalletService.prepare_payment` -> `RiskService`
  (deterministic rules + the ledger reservation, with the wallet limits installed by
  `WalletBackedSpendPolicy`);
* approve or decline: `ApprovalGateway` (Specialist 1);
* move a ledger entry, call a provider or decide a lifecycle state: `LifecycleGateway`
  (Specialist 1). It never calls `record_payment_attempt` or `record_payment_outcome`.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from pydantic import ValidationError

from ..contracts import AgentContext, Capability, SourceEvidence
from ..intelligence.extractors import CompositeProductExtractor
from ..ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
from ..schemas import normalize_domain
from ..tools.browser_contracts import (
    BrowserProvider, PageChecker, MAX_HTML_BYTES, MAX_SCREENSHOT_BYTES, MAX_TEXT_CHARS,
)
from ..tools.netguard import vet_url
from ..infra_contracts import AuthenticatedOwner, PaymentLifecycle, ProviderError
from ..spending import ApprovalTerms, InvalidLedgerTransition
from ..wallet.permissions import DenyCode, PermissionChecker
from ..wallet.registry import AgentRegistry
from .deps import CaptureGateway, PlatformDeps, ProviderReferenceReader
from .errors import PlatformError, not_configured, not_found
from .models import BrowseOut, PageCheckOut, PaymentCreate, PaymentOut, contains_credential
from .wallets import WalletBook
from .timeline import RequestHistory

log = logging.getLogger("agentpal.platform")

_pending_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agentpal_pending_request_id")
_NONE = "-none-"
_HIDDEN = (DenyCode.OTHER_AGENT, DenyCode.CROSS_OWNER)


def take_pending_request_id() -> str:
    """`id_factory` for WalletService: the server-generated id reserved by the
    idempotency record of the request being created (never client-chosen, SEC-13)."""
    return _pending_request_id.get()


def new_request_id() -> str:
    import uuid
    return "pay_" + uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def body_digest(body: PaymentCreate) -> str:
    data = body.model_dump(mode="json")
    if body.merchant_paypal_email is None:
        data.pop("merchant_paypal_email")
    canonical = json.dumps(data, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# A timed-out synchronous provider cannot be forcibly stopped safely. Keep its slot
# until it actually finishes, with no queued work and no late payment-table writes.
BROWSER_DEADLINE_S = 45.0
_BROWSER_SLOTS = threading.BoundedSemaphore(4)


def _bounded_browser_call(call):
    slots = _BROWSER_SLOTS
    if not slots.acquire(blocking=False):
        raise TimeoutError("browser capacity unavailable")
    finished = threading.Event()
    result = []

    def work():
        try:
            result.append(call())
        except Exception:
            result.append(None)  # provider exception text must never escape
        finally:
            slots.release()
            finished.set()

    try:
        threading.Thread(target=work, name="agentpal-browser", daemon=True).start()
    except BaseException:
        slots.release()
        raise
    if not finished.wait(BROWSER_DEADLINE_S):
        raise TimeoutError("browser deadline exceeded")
    return result[0] if result else None


def _browser_target(url: str):
    # Resolver is called inside the overall deadline, never on the request thread.
    return vet_url(url, allow_http=True)


def _merchant_page_url(url: str, merchant_domain: str | None) -> None:
    """Exact merchant host or its subdomain, with a label boundary (never a suffix)."""
    try:
        parsed = httpx.URL(url)
        merchant = normalize_domain(merchant_domain or "")
        host = parsed.raw_host.decode("ascii").lower().rstrip(".")
        if (parsed.scheme not in {"https", "http"} or parsed.userinfo
                or (parsed.port is not None and parsed.port != (443 if parsed.scheme == "https" else 80))
                or not (host == merchant or host.endswith("." + merchant))):
            raise ValueError("off merchant")
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise PlatformError(422, "invalid_page_url") from None


class PaymentDesk:
    def __init__(self, registry: AgentRegistry, checker: PermissionChecker,
                 wallets: WalletBook, deps: PlatformDeps, risk_getter: Callable,
                 clock: Callable[[], datetime]):
        self.registry = registry
        self.store = registry.store
        self.checker = checker
        self.wallets = wallets
        self.deps = deps
        self.risk = risk_getter
        self.clock = clock
        self._wallet_service = None
        rate_path = (":memory:" if self.store.path == ":memory:"
                     else self.store.path + ".browser-limits.db")
        self._browse_rate_path = rate_path
        self._browse_limiter = None
        self._browse_limiter_lock = threading.Lock()

    # ------------------------------------------------------------ plumbing
    def wallet_service(self):
        if self._wallet_service is None:
            factory = self.deps.wallet_service_factory
            if factory is None:
                from ..wallet.service import WalletService

                def factory(registry, checker, risk, id_factory):
                    return WalletService(registry, checker, risk, id_factory=id_factory)
            self._wallet_service = factory(self.registry, self.checker, self.risk(),
                                           take_pending_request_id)
        return self._wallet_service

    def _deny(self, decision) -> PlatformError:
        if decision.code in _HIDDEN:
            return not_found(f"permission:{decision.code}")
        return PlatformError(403, "permission_denied", audit=f"permission:{decision.code}")

    def _require(self, ctx: AgentContext, cap: Capability, action: str, *,
                 resource_agent_id: str | None = None,
                 resource_owner_id: str | None = None) -> None:
        d = self.checker.check(ctx, cap, resource_agent_id=resource_agent_id,
                               resource_owner_id=resource_owner_id, action=action)
        if not d.allowed:
            raise self._deny(d)

    def _require_read(self, ctx: AgentContext, resource_agent_id: str, action: str) -> None:
        """Reading own payment requests: wallet.read OR payment.prepare."""
        d = self.checker.check(ctx, Capability.wallet_read,
                               resource_agent_id=resource_agent_id, action=action)
        if d.allowed:
            return
        if d.code == DenyCode.NOT_GRANTED:
            d = self.checker.check(ctx, Capability.payment_prepare,
                                   resource_agent_id=resource_agent_id, action=action)
            if d.allowed:
                return
        raise self._deny(d)

    def meta(self, request_id: str) -> dict | None:
        rows = self.store.query("SELECT * FROM platform_payment_meta WHERE request_id=?",
                                (request_id,))
        return rows[0] if rows else None

    def _prepared_row(self, request_id: str) -> dict | None:
        rows = self.store.query("SELECT * FROM wallet_payment_requests WHERE request_id=?",
                                (request_id,))
        return rows[0] if rows else None

    def _owned_meta(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        meta = self.meta(request_id)
        if meta is None:
            raise not_found()
        if meta["owner_id"] != owner.owner_id:
            raise not_found("cross_owner_payment")
        return meta

    def status(self, request_id: str, agent_id: str) -> PaymentLifecycle | None:
        lifecycle = self.deps.lifecycle
        if lifecycle is None:
            return None
        return PaymentLifecycle(lifecycle.status(request_id, agent_id=agent_id))

    # ------------------------------------------------- Channel3 market check
    def _run_market_check(self, agent_id: str, request_id: str, body: PaymentCreate,
                          idem_key: str | None = None, digest: str | None = None) -> None:
        checker = self.deps.market_for(self.registry.get_agent(agent_id).owner_id)
        if checker is None or not (body.product_query or body.product_url):
            return
        if self.store.query("SELECT 1 FROM platform_market_checks WHERE request_id=?",
                            (request_id,)):
            return  # crash-recovery retry of the same claim: keep the first snapshot
        if idem_key and digest:
            # a retry of the SAME logical request (same agent, key and body) whose claim was
            # released or recovered under a new request id: reuse, never re-spend a credit
            prior = self.store.query(
                "SELECT payload_json FROM platform_market_checks WHERE agent_id=? AND "
                "idem_key=? AND body_sha256=? ORDER BY created_at DESC LIMIT 1",
                (agent_id, idem_key, digest))
            if prior:
                self._store_market_check(agent_id, request_id,
                                         json.loads(prior[0]["payload_json"]), idem_key, digest)
                return
        if self.deps.resources:
            self.deps.resources.consume(self.registry.get_agent(agent_id).owner_id, "channel3",
                                        server_provided=checker is self.deps.market_checker)
        try:
            check = checker.check(amount=body.amount, currency=body.currency,
                                  merchant_domain=body.merchant_domain,
                                  product_query=body.product_query,
                                  product_url=body.product_url)
        except Exception:  # noqa: BLE001 - advisory evidence must never block a payment
            log.warning("market check failed", extra={"request_id": request_id})
            return
        self._store_market_check(agent_id, request_id, check, idem_key, digest)

    def _store_market_check(self, agent_id: str, request_id: str, check: dict,
                            idem_key: str | None = None, digest: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        product_id = (check.get("product") or {}).get("id")
        with self.store.tx() as c:
            c.execute("INSERT INTO platform_market_checks (request_id, agent_id, idem_key, "
                      "body_sha256, product_id, payload_json, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                      "product_id=excluded.product_id, payload_json=excluded.payload_json, "
                      "updated_at=excluded.updated_at",
                      (request_id, agent_id, idem_key, digest, product_id,
                       json.dumps(check, sort_keys=True), now, now))

    def market_check(self, request_id: str) -> dict | None:
        rows = self.store.query("SELECT payload_json FROM platform_market_checks WHERE "
                                "request_id=?", (request_id,))
        return json.loads(rows[0]["payload_json"]) if rows else None

    def refresh_market_check(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        """Owner-only: re-read the matched product's live offers through Channel3's FREE
        product endpoint (Channel3 asks integrators to refresh prices before display)."""
        meta = self._owned_meta(owner, request_id)
        checker = self.deps.market_for(owner.owner_id)
        if checker is None:
            raise not_configured()
        rows = self.store.query("SELECT product_id, payload_json FROM platform_market_checks "
                                "WHERE request_id=?", (request_id,))
        if not rows or not rows[0]["product_id"]:
            raise PlatformError(409, "market_check_unavailable")
        previous = json.loads(rows[0]["payload_json"])
        row = self._prepared_row(request_id)
        terms = json.loads(row["terms_json"])
        if self.deps.resources:
            self.deps.resources.consume(owner.owner_id, "channel3",
                                        server_provided=checker is self.deps.market_checker)
        check = checker.check(amount=Decimal(str(terms["amount"])), currency=terms["currency"],
                              merchant_domain=terms.get("merchant_domain"),
                              product_query=previous["input"].get("product_query"),
                              product_url=previous["input"].get("product_url"),
                              product_id=rows[0]["product_id"])
        if check.get("product") is None:
            raise PlatformError(409, "market_check_unavailable")
        self._store_market_check(meta["agent_id"], request_id, check)
        return check

    # ---------------------------------------------------- KERNEL advisory visits
    def page_check(self, request_id: str) -> dict | None:
        rows = self.store.query("SELECT payload_json FROM platform_page_checks WHERE request_id=?",
                                (request_id,))
        return json.loads(rows[0]["payload_json"]) if rows and rows[0]["payload_json"] else None

    def check_merchant_page(self, owner: AuthenticatedOwner, request_id: str,
                            url: str | None = None) -> dict:
        self._owned_meta(owner, request_id)  # scope before config, URL or network checks
        row = self._prepared_row(request_id)
        if row is None:
            raise not_found()
        terms = json.loads(row["terms_json"])
        saved = self.store.query("SELECT product_url FROM platform_page_checks WHERE request_id=?",
                                 (request_id,))
        url = url or (saved[0]["product_url"] if saved else None)
        if not url:
            raise PlatformError(409, "page_url_unavailable")
        _merchant_page_url(url, terms.get("merchant_domain"))
        fallback = PageCheckOut(
            checked_at=_iso(self.clock()), advisory_level="no_data",
            input={"url": url, "claimed_amount": str(terms["amount"]),
                   "currency": terms["currency"], "merchant_domain": terms.get("merchant_domain")},
            signals=[{"code": "PAGE_DATA_UNAVAILABLE", "severity": "info",
                      "message": "Browser evidence is unavailable; owner approval is still required."}])

        tools = self.deps.kernel_for(owner.owner_id)
        checker: PageChecker | None = tools[1] if tools else None
        if checker is not None and self.deps.resources:
            self.deps.resources.consume(owner.owner_id, "kernel",
                                        server_provided=checker is self.deps.page_checker)

        def check():
            if checker is None:
                return None
            _browser_target(url)
            # PageChecker is the only analysis interface: no model or browser code here.
            return checker.check(url=url, amount=Decimal(str(terms["amount"])),
                currency=terms["currency"], merchant_domain=terms.get("merchant_domain"))

        try:
            value = _bounded_browser_call(check)
            result = PageCheckOut.model_validate(value)
            if contains_credential(result.model_dump(mode="json")):
                raise ValueError("credential in evidence")
        except Exception:
            result = fallback
        payload = result.model_dump(mode="json")
        now = _iso(self.clock())
        with self.store.tx() as c:
            c.execute("INSERT INTO platform_page_checks (request_id, product_url, payload_json, "
                      "created_at, updated_at) VALUES (?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE "
                      "SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                      (request_id, None, json.dumps(payload, sort_keys=True), now, now))
        return payload

    def browse(self, ctx: AgentContext, url: str) -> dict:
        self._require(ctx, Capability.research_fetch, "v1.research.browse",
                      resource_agent_id=ctx.agent_id)
        try:
            with self._browse_limiter_lock:
                if self._browse_limiter is None:
                    self._browse_limiter = SQLiteSlidingWindowLimiter(
                        self._browse_rate_path, "v1.kernel.browse", 6)
                limiter = self._browse_limiter
            allowed, retry = limiter.hit(ctx.agent_id)
        except LimiterUnavailable:
            raise PlatformError(503, "browser_admission_unavailable") from None
        if not allowed:
            raise PlatformError(429, "rate_limited", headers={"Retry-After": str(retry)})
        tools = self.deps.kernel_for(ctx.owner_id)
        browser: BrowserProvider | None = tools[0] if tools else None
        if browser is not None and self.deps.resources:
            self.deps.resources.consume(ctx.owner_id, "kernel",
                                        server_provided=browser is self.deps.browser)
        fallback = BrowseOut(status="no_data", requested_url=url)

        def capture():
            _browser_target(url)
            if browser is None or not browser.configured():
                return None
            shot = browser.capture(url, timeout_s=min(30.0, BROWSER_DEADLINE_S))
            if (len(shot.html) > MAX_HTML_BYTES or len(shot.visible_text) > MAX_TEXT_CHARS
                    or (shot.screenshot_png is not None
                        and len(shot.screenshot_png) > MAX_SCREENSHOT_BYTES)):
                return None
            # Recompute hashes from exact bytes; never trust a provider-supplied digest.
            html_sha = hashlib.sha256(shot.html).hexdigest()
            png_sha = (hashlib.sha256(shot.screenshot_png).hexdigest()
                       if shot.screenshot_png is not None else None)
            source = SourceEvidence(url=url, final_url=shot.final_url, method="browser",
                retrieved_at=shot.captured_at, content_sha256=html_sha, http_status=shot.http_status)
            facts = CompositeProductExtractor().extract(source, shot.html)
            result = BrowseOut(status="ok", requested_url=url, final_url=shot.final_url,
                redirect_chain=list(shot.redirect_chain), http_status=shot.http_status,
                title=shot.title, facts=facts, html_sha256=html_sha,
                screenshot_sha256=png_sha, replay_url=shot.replay_url,
                captured_at=_iso(shot.captured_at))
            if contains_credential(result.model_dump(mode="json")) or contains_credential(
                    shot.html.decode("utf-8", errors="replace")):
                return None
            evidence = self.deps.evidence_store
            if evidence is not None:
                evidence.put(shot.html)
                if shot.screenshot_png is not None:
                    evidence.put(shot.screenshot_png)
            return result

        try:
            result = _bounded_browser_call(capture)
            return (result or fallback).model_dump(mode="json")
        except Exception:
            return fallback.model_dump(mode="json")

    def view(self, request_id: str) -> dict:
        meta = self.meta(request_id)
        row = self._prepared_row(request_id)
        if meta is None or row is None:
            raise not_found()
        terms = json.loads(row["terms_json"])
        terms["amount"] = Decimal(str(terms["amount"]))
        # E2E-F1: the payee is part of the approved terms, so it is shown INSIDE `terms`
        # (and, for compatibility, also top-level).
        paypal_email = terms.get("merchant_paypal_email")
        from ..providers.paypal_sandbox import KNOWN_ISSUES
        facts = self.risk().store.lifecycle_facts(request_id, agent_id=row["agent_id"])
        reason = facts.get("last_reason") if facts else None
        stable_error = reason if reason and reason.upper() in KNOWN_ISSUES else None
        order = self.risk().store.provider_order(request_id, agent_id=row["agent_id"])
        provider = self.deps.paypal_for(meta["owner_id"])
        provider_name = order["provider"] if order else getattr(provider, "name", None)
        provider_mode = ("simulated" if provider_name == "local_simulation" else
                         "paypal_sandbox" if provider_name == "paypal" else None)
        return {"verification": self.history(request_id).verification(),
                "provider_mode": provider_mode,
                "page_check": self.page_check(request_id),
                "market_check": self.market_check(request_id),"request_id": request_id, "agent_id": meta["agent_id"],
                "owner_id": meta["owner_id"], "wallet_id": meta["wallet_id"],
                "terms": terms, "merchant_paypal_email": paypal_email,
                "provider_error_code": stable_error,
                "terms_sha256": row["terms_sha256"],
                "recommendation": row["recommendation"], "risk_score": row["risk_score"],
                "status": self.status(request_id, meta["agent_id"]),
                "decision_requested": bool(meta["decision_requested"]),
                "created_at": row["created_at"]}

    # -------------------------------------------------------------- create
    def create(self, ctx: AgentContext, body: PaymentCreate, idem_key: str
               ) -> tuple[dict, bool]:
        self._require(ctx, Capability.payment_prepare, "v1.payment.create",
                      resource_agent_id=ctx.agent_id)
        if body.request_owner_decision:
            self._require(ctx, Capability.owner_request_decision, "v1.payment.create",
                          resource_agent_id=ctx.agent_id)
        if self.deps.resources:
            self.deps.resources.check_amount(ctx.owner_id, body.amount)
        digest = body_digest(body)
        # Replay survives wallet freezes and later lifecycle changes. Authentication
        # and current permissions still apply before any saved response is disclosed.
        rows = self.store.query("SELECT * FROM platform_idempotency WHERE agent_id=? "
                                "AND idem_key=?", (ctx.agent_id, idem_key))
        if rows:
            if not hmac.compare_digest(rows[0]["body_sha256"], digest):
                raise PlatformError(409, "idempotency_key_reused")
            if rows[0]["status"] == "done" and rows[0]["response_json"] is not None:
                return json.loads(rows[0]["response_json"]), True
        wallets = self.wallets.for_agent(ctx.agent_id, body.currency)
        if not wallets:
            raise PlatformError(409, "no_wallet_for_currency")
        wallet = wallets[0]
        if wallet["status"] != "active":
            raise PlatformError(409, "wallet_frozen")
        request_id, replay = self._claim(ctx.agent_id, idem_key, digest)
        if replay:
            self._ensure_meta(request_id, ctx, wallet, body)
            return self._save_response(ctx.agent_id, request_id), True
        # Channel3 market check: advisory evidence for the owner. Runs once per claimed
        # request, OUTSIDE the wallet write transaction (network I/O never holds the
        # lock). Provider failures are advisory; server-quota refusal fails closed.
        # Replays return the stored snapshot (no extra credit).
        token = _pending_request_id.set(request_id)
        try:
            self._run_market_check(ctx.agent_id, request_id, body, idem_key, digest)
            # SQLite serializes preparation across threads AND processes. The
            # initial claim is already durable, so a process death between the risk
            # commit and this wallet commit remains recoverable with the same ID.
            with self.store.tx() as c:
                claim = c.execute("SELECT * FROM platform_idempotency WHERE agent_id=? "
                                  "AND idem_key=?", (ctx.agent_id, idem_key)).fetchone()
                if claim is None or claim["request_id"] != request_id:
                    # A newer worker won before we entered the transaction. Fence
                    # this delayed worker before any risk assessment/reservation.
                    if claim is not None and claim["response_json"] is not None:
                        return json.loads(claim["response_json"]), True
                    raise PlatformError(409, "idempotency_in_progress")
                if claim["response_json"] is not None:
                    return json.loads(claim["response_json"]), True
                from ..wallet.models import PaymentDraft
                draft = PaymentDraft(**body.model_dump(
                    exclude={"request_owner_decision", "product_query", "product_url",
                             "merchant_paypal_email"},
                    exclude_none=True))
                if self.risk().store.request_status(request_id) is not None:
                    self._restore_prepared(ctx, request_id, draft)
                else:
                    self.wallet_service().prepare_payment(ctx, draft)
                if body.merchant_paypal_email is not None:
                    self._bind_payee(ctx.agent_id, request_id, body.merchant_paypal_email)
                self._ensure_meta(request_id, ctx, wallet, body)
                return self._save_response(ctx.agent_id, request_id), False
        except Exception as exc:
            self._release_claim(ctx.agent_id, idem_key, request_id)
            raise self._prepare_error(exc) from None
        finally:
            _pending_request_id.reset(token)

    def _bind_payee(self, agent_id: str, request_id: str, email: str) -> None:
        from ..lifecycle.paypal_core import PayPalCoreBinding
        row = self._prepared_row(request_id)
        original = ApprovalTerms.model_validate_json(row["terms_json"])
        if original.merchant_paypal_email not in (None, email):
            raise PlatformError(409, "terms_mismatch")
        terms = ApprovalTerms.model_validate({**original.model_dump(), "merchant_paypal_email": email})
        PayPalCoreBinding(self.risk().store).bind_prepared(terms, self.clock())
        with self.store.tx() as c:
            c.execute("UPDATE wallet_payment_requests SET terms_json=?,terms_sha256=? "
                      "WHERE agent_id=? AND request_id=?",
                      (terms.model_dump_json(), terms.digest(), agent_id, request_id))

    def _restore_prepared(self, ctx: AgentContext, request_id: str, draft) -> None:
        """Recover the wallet projection of a committed assessment; never assess again.

        A claimed but incomplete assessment is owned by WS1 recovery. Keep the claim
        and fail closed instead of allocating a second reservation.
        """
        assessment = self.risk().store.get_assessment(request_id)
        if assessment is None:
            raise PlatformError(409, "preparation_interrupted")
        terms = ApprovalTerms(request_id=request_id, agent_id=ctx.agent_id,
                              amount=draft.amount, currency=draft.currency,
                              merchant_name=draft.merchant_name,
                              merchant_domain=draft.merchant_domain, purpose=draft.purpose)
        with self.store.tx() as c:
            c.execute("INSERT OR IGNORE INTO wallet_payment_requests (request_id, agent_id, "
                      "owner_id, terms_json, terms_sha256, recommendation, risk_score, "
                      "created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (request_id, ctx.agent_id, ctx.owner_id, terms.model_dump_json(),
                       terms.digest(), str(getattr(assessment.recommendation, "value",
                                                   assessment.recommendation)),
                       assessment.risk_score, _iso(self.clock())))

    def _save_response(self, agent_id: str, request_id: str) -> dict:
        view = PaymentOut(**self.view(request_id)).model_dump(mode="json")
        snapshot = json.dumps(view, sort_keys=True, separators=(",", ":"))
        with self.store.tx() as c:
            c.execute("UPDATE platform_idempotency SET status='done', response_json="
                      "COALESCE(response_json, ?), updated_at=? WHERE agent_id=? AND "
                      "request_id=?", (snapshot, _iso(self.clock()), agent_id, request_id))
            row = c.execute("SELECT response_json FROM platform_idempotency WHERE "
                            "agent_id=? AND request_id=?", (agent_id, request_id)).fetchone()
        return json.loads(row["response_json"])

    @staticmethod
    def _prepare_error(exc: Exception) -> Exception:
        from ..wallet.permissions import PermissionDenied
        if isinstance(exc, PermissionDenied):
            return PlatformError(403, "permission_denied", audit=f"permission:{exc.code}")
        if isinstance(exc, ValidationError):
            return PlatformError(422, "invalid_request")
        code = getattr(exc, "code", None)
        if type(exc).__name__ == "WalletRefused" and isinstance(code, str):
            return PlatformError(409, code.lower() if code.isupper() else "payment_refused")
        return exc

    def _claim(self, agent_id: str, key: str, digest: str) -> tuple[str, bool]:
        """Returns (request_id, is_replay). Raises 409 on a body mismatch or while another
        attempt with the same key is in progress."""
        now = self.clock()
        timeout = timedelta(seconds=self.deps.limits.idempotency_in_progress_timeout_s)
        with self.store.tx() as c:
            row = c.execute("SELECT * FROM platform_idempotency WHERE agent_id=? AND "
                            "idem_key=?", (agent_id, key)).fetchone()
            if row is None:
                request_id = new_request_id()
                c.execute("INSERT INTO platform_idempotency (agent_id, idem_key, "
                          "body_sha256, status, request_id, created_at, updated_at) "
                          "VALUES (?,?,?,'in_progress',?,?,?)",
                          (agent_id, key, digest, request_id, _iso(now), _iso(now)))
                return request_id, False
            if not hmac.compare_digest(row["body_sha256"], digest):
                raise PlatformError(409, "idempotency_key_reused")
            if row["status"] == "done":
                return row["request_id"], True
            started = datetime.fromisoformat(row["updated_at"])
            if now - started < timeout:
                raise PlatformError(409, "idempotency_in_progress")
            # stale in-progress record (the worker died): finish or restart it
            if c.execute("SELECT 1 FROM wallet_payment_requests WHERE request_id=?",
                         (row["request_id"],)).fetchone():
                return row["request_id"], True
            # Preserve any identity already claimed by the risk DB. A new identity
            # here would reserve twice after a crash between the two DB commits.
            request_id = (row["request_id"] if self.risk().store.request_status(
                row["request_id"]) is not None else new_request_id())
            c.execute("UPDATE platform_idempotency SET request_id=?, updated_at=? WHERE "
                      "agent_id=? AND idem_key=?", (request_id, _iso(now), agent_id, key))
            log.warning("idempotency_stale_claim_restarted")
            return request_id, False

    def _release_claim(self, agent_id: str, key: str, request_id: str) -> None:
        if self.risk().store.request_status(request_id) is not None:
            return
        with self.store.tx() as c:
            c.execute("DELETE FROM platform_idempotency WHERE agent_id=? AND idem_key=? AND "
                      "request_id=? AND status='in_progress'", (agent_id, key, request_id))

    def _ensure_meta(self, request_id: str, ctx: AgentContext, wallet: dict,
                     body: PaymentCreate) -> None:
        now = _iso(self.clock())
        with self.store.tx() as c:
            c.execute("INSERT OR IGNORE INTO platform_payment_meta (request_id, agent_id, "
                      "owner_id, wallet_id, decision_requested, decision_requested_at, "
                      "created_at) VALUES (?,?,?,?,?,?,?)",
                      (request_id, ctx.agent_id, wallet["owner_id"], wallet["wallet_id"],
                       int(body.request_owner_decision),
                       now if body.request_owner_decision else None, now))
            c.execute("INSERT OR IGNORE INTO platform_agent_context (request_id, "
                      "business_justification, product_query, created_at) VALUES (?,?,?,?)",
                      (request_id, body.business_justification, body.product_query, now))
            c.execute("INSERT OR IGNORE INTO platform_page_checks "
                      "(request_id, product_url, payload_json, created_at, updated_at) "
                      "VALUES (?,?,NULL,?,?)", (request_id, body.product_url, now, now))

    # --------------------------------------------------------------- reads
    def get_for_agent(self, ctx: AgentContext, request_id: str) -> dict:
        meta = self.meta(request_id)
        self._require_read(ctx, meta["agent_id"] if meta else _NONE, "v1.payment.get")
        return self.view(request_id)

    def list_for_agent(self, ctx: AgentContext) -> list[dict]:
        self._require_read(ctx, ctx.agent_id, "v1.payment.list")
        rows = self.store.query("SELECT request_id FROM platform_payment_meta WHERE "
                                "agent_id=? ORDER BY created_at, request_id", (ctx.agent_id,))
        return [self.view(r["request_id"]) for r in rows]

    def history(self, request_id: str) -> RequestHistory:
        return RequestHistory(self.risk().store, self.store, self._prepared_row(request_id))

    def timeline(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        self._owned_meta(owner, request_id)
        return self.history(request_id).timeline()

    def owner_view(self, request_id: str) -> dict:
        """The owner's view adds the risk engine's reasons (codes and server messages only;
        raw evidence stays internal). Agents never receive them."""
        view = self.view(request_id)
        history = self.history(request_id)
        view["owner_decision"] = history.owner_decision()
        blocks = history.blocks()
        view["policy_block"] = blocks[0] if blocks else None
        meta = self.meta(request_id)
        assessment = self.risk().store.get_assessment(request_id, agent_id=meta["agent_id"])
        view["risk_signals"] = [{"code": s.code, "kind": s.kind, "points": s.points,
                                 "message": s.message}
                                for s in (assessment.signals if assessment else [])][:100]
        ctx_rows = self.store.query("SELECT business_justification, product_query FROM "
                                    "platform_agent_context WHERE request_id=?", (request_id,))
        url_rows = self.store.query("SELECT product_url FROM platform_page_checks WHERE "
                                    "request_id=?", (request_id,))
        if ctx_rows or url_rows:
            view["agent_context"] = {
                "business_justification": ctx_rows[0]["business_justification"] if ctx_rows else "",
                "product_query": ctx_rows[0]["product_query"] if ctx_rows else None,
                "product_url": url_rows[0]["product_url"] if url_rows else None}
        return view

    def get_for_owner(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        self._owned_meta(owner, request_id)
        return self.owner_view(request_id)

    def list_for_owner(self, owner: AuthenticatedOwner) -> list[dict]:
        rows = self.store.query("SELECT request_id FROM platform_payment_meta WHERE "
                                "owner_id=? ORDER BY created_at, request_id",
                                (owner.owner_id,))
        return [self.owner_view(r["request_id"]) for r in rows]

    def approvals(self, owner: AuthenticatedOwner) -> list[dict]:
        if self.deps.lifecycle is None:
            raise not_configured()
        rows = self.store.query("SELECT request_id FROM platform_payment_meta WHERE "
                                "owner_id=? AND decision_requested=1 "
                                "ORDER BY created_at, request_id", (owner.owner_id,))
        views = [self.owner_view(r["request_id"]) for r in rows]
        return [v for v in views if v["status"] == PaymentLifecycle.awaiting_approval]

    # ------------------------------------------------------- agent actions
    def request_decision(self, ctx: AgentContext, request_id: str) -> dict:
        meta = self.meta(request_id)
        self._require(ctx, Capability.owner_request_decision, "v1.payment.request_decision",
                      resource_agent_id=meta["agent_id"] if meta else _NONE)
        if meta is None:
            raise not_found()
        if not meta["decision_requested"]:
            st = self.status(request_id, meta["agent_id"])
            if st not in (None, PaymentLifecycle.prepared, PaymentLifecycle.awaiting_approval):
                raise PlatformError(409, "not_awaiting_decision")
            with self.store.tx() as c:
                c.execute("UPDATE platform_payment_meta SET decision_requested=1, "
                          "decision_requested_at=? WHERE request_id=?",
                          (_iso(self.clock()), request_id))
        return self.view(request_id)

    def execute(self, ctx: AgentContext, request_id: str) -> PaymentLifecycle:
        lifecycle = self.deps.lifecycle
        meta = self.meta(request_id)
        self._require(ctx, Capability.payment_execute_sandbox, "v1.payment.execute",
                      resource_agent_id=meta["agent_id"] if meta else _NONE,
                      resource_owner_id=meta["owner_id"] if meta else _NONE)
        provider = self.deps.paypal_for(meta["owner_id"])
        if lifecycle is None or provider is None:
            raise not_configured()
        if getattr(provider, "environment", None) != "sandbox":
            raise PlatformError(503, "not_configured")
        wallet = self.wallets.get(meta["wallet_id"])
        if wallet is None or wallet["status"] != "active":
            raise PlatformError(409, "wallet_frozen")
        row = self._prepared_row(request_id)
        terms = ApprovalTerms.model_validate_json(row["terms_json"])
        if self.deps.resources:
            self.deps.resources.check_amount(meta["owner_id"], terms.amount,
                                             server_provided=provider is self.deps.provider)
        try:
            ticket = lifecycle.begin_execution(ctx.agent_id, request_id, terms, provider)
        except PermissionError:
            raise PlatformError(403, "execution_refused",
                                audit="execution_refused") from None
        except (ValueError, LookupError, RuntimeError):
            raise PlatformError(409, "execution_refused") from None
        try:
            return PaymentLifecycle(lifecycle.submit_to_provider(ticket, provider))
        except ProviderError:
            log.error("provider_error_escaped_lifecycle")
            raise PlatformError(502, "provider_error") from None

    # ------------------------------------------------------- owner actions
    def capture(self, owner: AuthenticatedOwner, request_id: str) -> PaymentLifecycle:
        meta = self._owned_meta(owner, request_id)
        lifecycle = self.deps.lifecycle
        if not isinstance(lifecycle, CaptureGateway):
            raise not_configured()
        agent = self.registry.get_agent(meta["agent_id"])
        if agent is None:
            raise not_found()
        ctx = AgentContext(agent_id=agent.agent_id, owner_id=owner.owner_id, role=agent.role.value)
        self._require(ctx, Capability.payment_execute_sandbox, "v1.payment.capture",
                      resource_agent_id=meta["agent_id"], resource_owner_id=owner.owner_id)
        wallet = self.wallets.get(meta["wallet_id"])
        if wallet is None or wallet["status"] != "active":
            raise PlatformError(409, "wallet_frozen")
        if self.deps.resources:
            terms = ApprovalTerms.model_validate_json(self._prepared_row(request_id)["terms_json"])
            self.deps.resources.check_amount(owner.owner_id, terms.amount)
        try:
            return PaymentLifecycle(lifecycle.capture(request_id))
        except InvalidLedgerTransition as exc:
            code = getattr(exc, "code", None)
            if code in ("PAYER_NOT_APPROVED", "ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED",
                        "BUYER_APPROVAL_EXPIRED"):
                stable = "order_not_approved" if code == "PAYER_NOT_APPROVED" else code.lower()
                raise PlatformError(409, stable, audit=stable) from None
            if code == "PAYER_READ_UNAVAILABLE":
                # nothing was captured; the payment stays pending, so the owner may retry
                raise PlatformError(503, "provider_unavailable",
                                    audit="capture_payer_read_unavailable") from None
            raise PlatformError(409, "capture_refused", audit="capture_refused") from None
        except PermissionError:
            raise PlatformError(403, "capture_refused", audit="capture_refused") from None
        except (ValueError, LookupError, RuntimeError):
            raise PlatformError(409, "capture_refused", audit="capture_refused") from None

    def reconcile(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        self._owned_meta(owner, request_id)
        lifecycle = self.deps.lifecycle
        if lifecycle is None:
            raise not_configured()
        try:
            lifecycle.reconcile(request_id)
        except (ValueError, LookupError):
            raise PlatformError(409, "reconciliation_unavailable") from None
        except RuntimeError:
            raise PlatformError(503, "reconciliation_unavailable") from None
        return self.reconciliation(owner, request_id)

    def approve(self, owner: AuthenticatedOwner, request_id: str, terms_sha256: str) -> dict:
        gateway = self.deps.approvals
        if gateway is None:
            raise not_configured()
        meta = self._owned_meta(owner, request_id)
        if not meta["decision_requested"]:
            raise PlatformError(409, "decision_not_requested")
        if self.deps.require_mfa_for_approval and not owner.mfa_satisfied:
            raise PlatformError(403, "mfa_required", audit="mfa_required")
        row = self._prepared_row(request_id)
        # defence in depth: Specialist 1's gateway binds the digest too
        if row is None or not hmac.compare_digest(row["terms_sha256"], terms_sha256):
            raise PlatformError(409, "terms_mismatch", audit="terms_mismatch")
        try:
            gateway.approve(owner, request_id, terms_sha256=terms_sha256)
        except PermissionError:
            raise PlatformError(403, "approval_refused", audit="approval_refused") from None
        except (ValueError, LookupError, RuntimeError):
            raise PlatformError(409, "approval_refused") from None
        return {"request_id": request_id, "decision": "approved",
                "status": self.status(request_id, meta["agent_id"])}

    def reject(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        gateway = self.deps.approvals
        if gateway is None:
            raise not_configured()
        meta = self._owned_meta(owner, request_id)
        try:
            gateway.decline(owner, request_id)
        except PermissionError:
            raise PlatformError(403, "decision_refused", audit="decision_refused") from None
        except (ValueError, LookupError, RuntimeError):
            raise PlatformError(409, "decision_refused") from None
        return {"request_id": request_id, "decision": "rejected",
                "status": self.status(request_id, meta["agent_id"])}

    def reconciliation(self, owner: AuthenticatedOwner, request_id: str) -> dict:
        lifecycle = self.deps.lifecycle
        if lifecycle is None:
            raise not_configured()
        meta = self._owned_meta(owner, request_id)
        st = PaymentLifecycle(lifecycle.status(request_id, agent_id=meta["agent_id"]))
        refs = []
        if isinstance(lifecycle, ProviderReferenceReader):
            refs = [{"provider": r.provider, "environment": r.environment,
                     "provider_order_id": r.provider_order_id, "status": r.status.value,
                     "payer_action_url": r.payer_action_url}
                    for r in lifecycle.provider_references(request_id)]
        return {"request_id": request_id, "status": st,
                "reconciliation_required": st == PaymentLifecycle.reconciliation_required,
                "provider_references": refs}
