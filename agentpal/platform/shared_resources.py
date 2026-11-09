"""Durable owner admission for operator-provided sandbox resources.

Reservations are charged before I/O and never refunded on failures or policy blocks.
Workspace credentials bypass these limits, but never the normal permission/policy gates.
"""
from decimal import Decimal, InvalidOperation

from ..ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
from .errors import PlatformError


class SharedResources:
    DEFAULTS = {"errands": 25, "page_checks": 30, "market_checks": 60,
                "model_calls": 150, "paypal_checks": 30}
    CATEGORY = {"channel3": "market_checks", "kernel": "page_checks",
                "groq": "model_calls", "openrouter": "model_calls",
                "paypal_sandbox": "paypal_checks"}

    def __init__(self, integrations, hosted=False):
        self.integrations = integrations
        self.hosted = hosted
        env = integrations.env
        self.daily = {}
        self.global_daily = {}
        for name, default in self.DEFAULTS.items():
            value = int(env.get("AGENTPAL_SERVER_" + name.upper() + "_DAILY", default))
            if not 1 <= value <= 1_000_000:
                raise ValueError("invalid shared provider daily cap")
            self.daily[name] = SQLiteSlidingWindowLimiter(
                integrations.limiter_path, "server-" + name + "-daily", value, 86400.0)
            total = int(env.get("AGENTPAL_SERVER_" + name.upper() + "_GLOBAL_DAILY", default * 12))
            if not 1 <= total <= 10_000_000:
                raise ValueError("invalid shared provider daily cap")
            self.global_daily[name] = SQLiteSlidingWindowLimiter(
                integrations.limiter_path, "server-" + name + "-global-daily", total, 86400.0)
        # One ceiling for the whole operator sandbox account, across every workspace.
        total = int(env.get("AGENTPAL_SERVER_PAYPAL_EXECUTIONS_DAILY", 100))
        if not 1 <= total <= 1_000_000:
            raise ValueError("invalid shared provider daily cap")
        self.global_executions = SQLiteSlidingWindowLimiter(
            integrations.limiter_path, "server-paypal-executions-global-daily", total, 86400.0)
        self.max_amount = self._amount(env.get("AGENTPAL_HOSTED_MAX_AMOUNT", "50.00"))
        self.max_monthly = self._amount(env.get("AGENTPAL_HOSTED_MAX_MONTHLY_BUDGET", "500.00"))

    @staticmethod
    def _amount(value):
        try:
            number = Decimal(value)
            if not number.is_finite() or number <= 0 or number.as_tuple().exponent < -2:
                raise ValueError()
            return number
        except (ValueError, InvalidOperation):
            raise ValueError("invalid hosted amount cap") from None

    def uses_server(self, owner_id, provider):
        return (self.integrations.server_fields(provider) is not None
                and self.integrations.fields(owner_id, provider) is None)

    def _server_use(self, owner_id, provider, server_provided):
        # Callers with an already selected provider pin its source. Never reclassify an
        # in-flight server call when workspace credentials change concurrently.
        if server_provided is None:
            return self.uses_server(owner_id, provider)
        return server_provided and self.integrations.server_fields(provider) is not None

    def consume(self, owner_id, provider, category=None, *, server_provided=None):
        if not self._server_use(owner_id, provider, server_provided):
            return
        name = category or self.CATEGORY[provider]
        try:
            allowed, retry = self.daily[name].hit(owner_id)
        except LimiterUnavailable:
            raise PlatformError(503, "server_quota_unavailable") from None
        if not allowed:
            raise PlatformError(429, "server_" + name + "_daily_limit",
                                headers={"Retry-After": str(retry)})
        try:
            allowed, retry = self.global_daily[name].hit("all-workspaces")
        except LimiterUnavailable:
            raise PlatformError(503, "server_quota_unavailable") from None
        if not allowed:
            raise PlatformError(429, "server_" + name + "_global_daily_limit",
                                headers={"Retry-After": str(retry)})

    def amount_limits(self, owner_id, *, server_provided=None):
        if self.hosted and self._server_use(owner_id, "paypal_sandbox", server_provided):
            return {"max_amount": format(self.max_amount, ".2f"),
                    "max_monthly_budget": format(self.max_monthly, ".2f")}
        return None

    def check_amount(self, owner_id, amount, *, server_provided=None):
        if self.amount_limits(owner_id, server_provided=server_provided) and amount > self.max_amount:
            raise PlatformError(422, "hosted_amount_limit_exceeded")

    def check_wallet(self, owner_id, values):
        if not self.amount_limits(owner_id):
            return
        for name, value in values.items():
            ceiling = self.max_monthly if name in {"monthly_budget", "rolling_window_limit"} else self.max_amount
            if value > ceiling:
                raise PlatformError(422, "hosted_amount_limit_exceeded")

    def consume_execution(self, owner_id, *, server_provided):
        """Hosted only: count each money movement on the operator's PayPal sandbox account
        against one global daily ceiling shared by all workspaces."""
        if not (self.hosted and self._server_use(owner_id, "paypal_sandbox", server_provided)):
            return
        try:
            allowed, retry = self.global_executions.hit("all-workspaces")
        except LimiterUnavailable:
            raise PlatformError(503, "server_quota_unavailable") from None
        if not allowed:
            raise PlatformError(429, "server_paypal_global_daily_limit",
                                headers={"Retry-After": str(retry)})
