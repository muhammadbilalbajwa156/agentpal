"""Runtime settings (environment) and the risk policy (JSON file).

All thresholds, limits and rule weights live in the policy file so they can be
tuned against evaluation results without touching code.
"""

from __future__ import annotations

import json
import os
import ssl
import tempfile
from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = PACKAGE_ROOT / "config" / "risk_policy.json"


class ScoreBands(BaseModel):
    model_config = ConfigDict(extra="forbid")
    medium: int = Field(25, ge=1, le=100)
    high: int = Field(50, ge=1, le=100)
    critical: int = Field(75, ge=1, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> "ScoreBands":
        if not (self.medium < self.high < self.critical):
            raise ValueError("score bands must satisfy medium < high < critical")
        return self


class CombinationBlock(BaseModel):
    """Owner decision 2: a named, explicit combination of deterministic RULE signals that
    may block. Model findings can never satisfy it. Ship disabled until validated on an
    independent benchmark."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    all_of: list[str] = Field(min_length=2)
    enabled: bool = False
    rationale: str = Field(min_length=10)


class DegradedMode(BaseModel):
    """Owner decision 5: when a configured model backend fails, times out or is unavailable,
    the assessment falls back to the rules and is marked degraded. If material risk remains
    (rule score at or above this value), human review is required."""

    model_config = ConfigDict(extra="forbid")
    review_if_rule_score_at_or_above: int = Field(1, ge=0, le=101)


class RecommendationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_at_or_above: int = Field(25, ge=0, le=100)
    # Owner decision 2: only hard (deterministic policy) violations and enabled combination
    # blocks produce `block`; a high score from soft signals alone means human review.
    combination_blocks: list[CombinationBlock] = Field(default_factory=list)
    review_on_any_uncertainty: bool = False
    review_if_model_uncertainty_at_or_above: float = Field(0.7, ge=0.0, le=1.0)
    # Owner decision 9: "score_only" = a model escalates only via its bounded score.
    model_escalation: Literal["score_only", "label_or_score"] = "score_only"
    degraded_mode: DegradedMode = Field(default_factory=DegradedMode)


class ModelBlending(BaseModel):
    """How far a model's opinion may move the deterministic score."""

    model_config = ConfigDict(extra="forbid")
    max_upward_adjustment: int = Field(25, ge=0, le=100)
    max_downward_adjustment: int = Field(10, ge=0, le=100)


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_currencies: list[str]
    budget_share_review_ratio: float = Field(gt=0, le=1)
    block_on_budget_exceeded: bool = True
    duplicate_intent_window_hours: float = Field(gt=0)
    amount_spike_ratio_vs_history: float = Field(gt=1)
    max_uncertainty_points: int = Field(30, ge=0, le=100)
    # Owner decision 9 (review P04): same agent, merchant and currency within the duplicate
    # window, amount differing by at most this fraction (or one minor unit) -> review.
    near_duplicate_tolerance_ratio: float = Field(0.02, ge=0, le=0.5)


class TimePolicy(BaseModel):
    """Server-side time rules. All windows are measured from the server receipt time;
    client timestamps are untrusted metadata (P0.3)."""

    model_config = ConfigDict(extra="forbid")
    default_ttl_minutes: int = Field(60, gt=0)        # applied when expires_at is absent
    max_lifetime_hours: int = Field(48, gt=0)         # expires_at - receipt time must not exceed
    max_client_clock_skew_minutes: int = Field(10, ge=0)  # created_at ahead of server time
    max_client_age_hours: int = Field(24, gt=0)       # created_at older than this is stale

    @model_validator(mode="after")
    def _ttl_within_lifetime(self) -> "TimePolicy":
        if self.default_ttl_minutes > self.max_lifetime_hours * 60:
            raise ValueError("default_ttl_minutes must not exceed max_lifetime_hours")
        return self


class EvidencePolicy(BaseModel):
    """Owner decision 9 (review P13/P16): agent-supplied evidence cannot be verified in
    Phase 1, so by default it never lowers risk (rules or ML input). Evidence that
    contradicts the request still raises risk."""

    model_config = ConfigDict(extra="forbid")
    claimed_evidence_reduces_risk: bool = False


class ApprovedMerchant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    domain: str
    category: str
    # Approval covers exactly `domain`. Subdomains are approved only when the operator
    # opts in, and even then only within the same registrable domain (PSL) (P0.6).
    include_subdomains: bool = False


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules_version: str
    score_bands: ScoreBands
    recommendation: RecommendationPolicy
    model_blending: ModelBlending
    limits: Limits
    time_policy: TimePolicy
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    approved_merchants: list[ApprovedMerchant]
    blocked_domains: list[str]
    suspicious_tlds: list[str]
    rule_points: dict[str, int]

    def points(self, code: str) -> int:
        return self.rule_points.get(code, 0)

    @model_validator(mode="after")
    def _combinations_use_rule_codes(self) -> "RiskPolicy":
        for combo in self.recommendation.combination_blocks:
            unknown = [c for c in combo.all_of if c not in self.rule_points]
            if unknown:
                raise ValueError(f"combination {combo.name!r} references unknown rule codes")
        return self

    @property
    def approved_domains(self) -> dict[str, ApprovedMerchant]:
        return {m.domain.lower(): m for m in self.approved_merchants}


def load_policy(path: str | Path | None = None) -> RiskPolicy:
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    if not policy_path.is_absolute() and not policy_path.exists():
        policy_path = PACKAGE_ROOT / policy_path
    with policy_path.open("r", encoding="utf-8") as fh:
        return RiskPolicy.model_validate(json.load(fh))


AUTH_REQUIRED = "required"
AUTH_DISABLED_DEV = "disabled_insecure_dev"
LOOPBACK_HOSTNAMES = ("127.0.0.1", "::1", "localhost")


class UnsafeConfiguration(RuntimeError):
    """The settings combine options that are refused (see Settings.check_runtime)."""


def _env(key: str, default: str) -> str:
    return os.environ.get(key) or default


def _env_list(key: str, default: str) -> list[str]:
    return [p.strip() for p in _env(key, default).split(",") if p.strip()]


class Settings(BaseModel):
    """Process settings sourced from environment variables (see .env.example).

    Security settings (environment, authentication, rate limits, allowed hosts) default to
    the process environment even when `Settings(...)` is built in code, so an embedding
    harness cannot silently drop the operator's security configuration. Explicit
    constructor arguments still win (tests use that)."""

    model_config = ConfigDict(extra="forbid")

    # ---- security (SEC-01..SEC-04, SEC-11); see docs/SECURITY_AUDIT.md
    environment: Literal["development", "production"] = Field(
        default_factory=lambda: _env("AGENTPAL_ENV", "development"))
    # "required" (default): every endpoint except /health needs a per-agent or operator
    # bearer token. "disabled_insecure_dev": no authentication or authorization at all;
    # refused in production and for a non-loopback bind, logged loudly at start-up.
    auth_mode: Literal["required", "disabled_insecure_dev"] = Field(
        default_factory=lambda: _env("AGENTPAL_AUTH_MODE", AUTH_REQUIRED))
    # JSON file holding SHA-256 hashes of API tokens (never plaintext); see auth.py
    api_tokens_file: str | None = Field(
        default_factory=lambda: os.environ.get("AGENTPAL_API_TOKENS_FILE") or None)
    # SQLite is the default. Memory requires an explicit development configuration.
    rate_limit_backend: Literal["sqlite", "memory"] = Field(
        default_factory=lambda: _env("AGENTPAL_RATE_LIMIT_BACKEND", "sqlite"))
    rate_limit_db_path: str | None = Field(
        default_factory=lambda: os.environ.get("AGENTPAL_RATE_LIMIT_DB_PATH") or None)
    # Sliding windows of 60 seconds
    rate_limit_per_token_per_minute: int = Field(
        default_factory=lambda: int(_env("AGENTPAL_RATE_LIMIT_PER_TOKEN", "60")), ge=1)
    rate_limit_per_peer_per_minute: int = Field(
        default_factory=lambda: int(_env("AGENTPAL_RATE_LIMIT_PER_PEER", "300")), ge=1)
    require_mfa_for_approval: bool = Field(
        default_factory=lambda: _env("AGENTPAL_REQUIRE_MFA_FOR_APPROVAL", "false"), validate_default=True)
    tls_certfile: str | None = Field(
        default_factory=lambda: os.environ.get("AGENTPAL_TLS_CERTFILE") or None)
    tls_keyfile: str | None = Field(
        default_factory=lambda: os.environ.get("AGENTPAL_TLS_KEYFILE") or None, repr=False)
    public_webhook_enabled: bool = Field(
        default_factory=lambda: _env("AGENTPAL_PUBLIC_WEBHOOK_ENABLED", "false"), validate_default=True)
    public_webhook_hosts: list[str] = Field(
        default_factory=lambda: _env_list("AGENTPAL_PUBLIC_WEBHOOK_HOSTS", ""))
    webhook_rate_limit_global_per_minute: int = Field(
        default_factory=lambda: int(_env("AGENTPAL_WEBHOOK_RATE_LIMIT_GLOBAL", "120")), ge=1)
    webhook_rate_limit_per_peer_per_minute: int = Field(
        default_factory=lambda: int(_env("AGENTPAL_WEBHOOK_RATE_LIMIT_PER_PEER", "30")), ge=1)
    # Hosted mode (2026-11-05): "render" trusts Render's TLS-terminating front proxy, which
    # is the ONLY network path into a Render web service. It opens the authenticated /v1
    # API and health to remote clients; every other route stays loopback-only. Refused
    # unless the process really runs on Render (RENDER=true). See docs/DEPLOYMENT.md.
    hosted_proxy: Literal["none", "render"] = Field(
        default_factory=lambda: _env("AGENTPAL_HOSTED_PROXY", "none"))
    # Host header allow-list (DNS-rebinding defence for a loopback service)
    allowed_hosts: list[str] = Field(
        default_factory=lambda: _env_list("AGENTPAL_ALLOWED_HOSTS", ",".join(LOOPBACK_HOSTNAMES)))

    policy_path: str = str(DEFAULT_POLICY_PATH)
    db_path: str = "agentpal_dev.db"
    spend_policy_path: str = str(PACKAGE_ROOT / "config" / "spend_policy.json")
    # Default chosen on the validation split (see README "Evaluation"). The local LLM
    # backends are opt-in: on CPU they were slow and did not discriminate risk.
    analyzer_chain: list[str] = ["ml", "heuristic"]
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:0.5b-instruct"
    llamacpp_model_path: str = str(
        PACKAGE_ROOT / "artifacts" / "models" / "agentpal-qwen3-1.7b-q4_k_m.gguf"
    )
    # Must equal the value the served model was trained with (ML-09). The pinned llama.cpp
    # model is the fine-tuned AgentPal Qwen3-1.7B (docs/training_runs/2026-10-30-modal),
    # trained WITHOUT rule findings, so the default is false. Set it to true only for a
    # zero-shot model that expects them.
    llamacpp_include_rule_findings: bool = False
    model_timeout_seconds: float = 30.0
    ml_model_path: str = str(PACKAGE_ROOT / "artifacts" / "baseline_classifier.json")
    # Trusted digests of model artifacts (P0.7d); kept in config/, not next to artifacts.
    model_manifest_path: str = str(PACKAGE_ROOT / "config" / "model_manifest.json")
    require_model_integrity: bool = True
    # decision 7: GGUF files must be on read-only storage and are loaded from a private,
    # hash-verified staging copy
    require_read_only_model_storage: bool = True
    model_staging_dir: str = str(Path(tempfile.gettempdir()) / f"agentpal-models-{os.getuid()}")
    log_level: str = "INFO"
    max_request_bytes: int = Field(32_768, ge=1)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        values: dict[str, object] = {}
        mapping = {
            "AGENTPAL_POLICY_PATH": "policy_path",
            "AGENTPAL_DB_PATH": "db_path",
            "AGENTPAL_SPEND_POLICY_PATH": "spend_policy_path",
            "AGENTPAL_OLLAMA_URL": "ollama_url",
            "AGENTPAL_OLLAMA_MODEL": "ollama_model",
            "AGENTPAL_LLAMACPP_MODEL_PATH": "llamacpp_model_path",
            "AGENTPAL_LLAMACPP_INCLUDE_RULE_FINDINGS": "llamacpp_include_rule_findings",
            "AGENTPAL_MODEL_TIMEOUT_SECONDS": "model_timeout_seconds",
            "AGENTPAL_ML_MODEL_PATH": "ml_model_path",
            "AGENTPAL_MODEL_MANIFEST_PATH": "model_manifest_path",
            "AGENTPAL_REQUIRE_MODEL_INTEGRITY": "require_model_integrity",
            "AGENTPAL_REQUIRE_READ_ONLY_MODEL_STORAGE": "require_read_only_model_storage",
            "AGENTPAL_MODEL_STAGING_DIR": "model_staging_dir",
            "AGENTPAL_LOG_LEVEL": "log_level",
            "AGENTPAL_MAX_REQUEST_BYTES": "max_request_bytes",
        }
        for env_key, field in mapping.items():
            if env.get(env_key):
                values[field] = env[env_key]
        if env.get("AGENTPAL_ANALYZER_CHAIN"):
            values["analyzer_chain"] = [
                p.strip() for p in env["AGENTPAL_ANALYZER_CHAIN"].split(",") if p.strip()
            ]
        return cls.model_validate(values)

    def tls_options(self) -> dict:
        """Validate TLS material before binding. Never include key data in an error."""
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise UnsafeConfiguration("TLS requires both certificate and key")
        if not self.tls_certfile:
            return {}
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.tls_certfile, self.tls_keyfile, password=lambda: "")
        except (OSError, ValueError, ssl.SSLError):
            raise UnsafeConfiguration("TLS certificate/key could not be loaded") from None
        return {"ssl_certfile": self.tls_certfile, "ssl_keyfile": self.tls_keyfile}

    @property
    def auth_disabled(self) -> bool:
        return self.auth_mode == AUTH_DISABLED_DEV

    def check_runtime(self, host: str | None = None,
                      spend_policy_configuration: str | None = None) -> list[str]:
        """Refuse unsafe combinations (raises UnsafeConfiguration) and return warnings.

        Always refused: authentication disabled with a non-loopback bind host.
        Refused when AGENTPAL_ENV=production: authentication disabled, no token file, a
        demo spend policy, model integrity or read-only model storage switched off, an
        in-memory database, or a Host allow-list that contains anything but loopback
        names. External binds additionally require direct TLS and the narrow, explicit
        webhook option. General API routes remain loopback-only (owner decision 10)."""
        errors: list[str] = []
        self.tls_options()  # load the key pair now; do not bind on invalid material
        hosted = self.hosted_proxy == "render"
        if hosted:
            if os.environ.get("RENDER") != "true":
                errors.append("AGENTPAL_HOSTED_PROXY=render is only valid on Render (RENDER=true)")
            if self.auth_disabled:
                errors.append("hosted mode requires authentication mode required")
            if self.rate_limit_backend != "sqlite" or self.rate_limit_db_path == ":memory:":
                errors.append("hosted mode requires a durable shared SQLite limiter")
            if self.db_path == ":memory:":
                errors.append("hosted mode refuses an in-memory database")
            disk = os.environ.get("AGENTPAL_DATA_DIR", "").rstrip("/")
            paths = [self.db_path, self.rate_limit_db_path or self.db_path + ".ratelimit.db",
                     os.environ.get("AGENTPAL_IDENTITY_DB_PATH") or self.db_path + ".identity.db",
                     os.environ.get("AGENTPAL_WALLET_DB_PATH") or self.db_path + ".wallet.db"]
            if not disk or any(not str(p).startswith(disk + "/") for p in paths):
                # a redeploy without the persistent disk would wipe identities and ledgers
                errors.append("hosted mode requires every database under AGENTPAL_DATA_DIR "
                              "(the persistent disk)")
            if self.environment != "production":
                errors.append("hosted mode requires AGENTPAL_ENV=production")
            public = [h for h in self.allowed_hosts if h not in LOOPBACK_HOSTNAMES]
            if (not public or any(not h or any(c in h for c in "/*:@?#[] ") for h in public)
                    or any(not h.endswith(".onrender.com") for h in public)):
                errors.append("hosted mode requires exact *.onrender.com Host names")
            if self.public_webhook_enabled and set(self.public_webhook_hosts) - set(public):
                errors.append("hosted webhook hosts must be among the allowed Host names")
        if host is not None and host not in LOOPBACK_HOSTNAMES and not hosted:
            if not (self.tls_certfile and self.tls_keyfile):
                errors.append("a non-loopback bind requires TLS")
            if not self.public_webhook_enabled:
                errors.append("non-loopback binds require the explicit public webhook option")
        if self.public_webhook_enabled:
            if self.auth_disabled:
                errors.append("public webhooks require authentication mode required")
            if (not self.tls_certfile or not self.tls_keyfile) and not hosted:
                errors.append("public webhooks require TLS")  # hosted: Render terminates TLS
            if self.rate_limit_backend != "sqlite" or self.rate_limit_db_path == ":memory:":
                errors.append("public webhooks require a durable shared SQLite limiter")
            if not self.public_webhook_hosts or any(
                    not h or any(c in h for c in "/*:@?#[] ")
                    for h in self.public_webhook_hosts):
                errors.append("public webhooks require exact approved DNS Host names")
        warnings: list[str] = []
        if self.auth_disabled and host is not None and host not in LOOPBACK_HOSTNAMES:
            errors.append("authentication is disabled, so the API may only bind loopback")
        if self.rate_limit_backend == "memory" and self.environment != "development":
            errors.append("in-memory rate limits require development mode")
        if self.environment == "production" and self.rate_limit_db_path == ":memory:":
            errors.append("production requires durable shared rate limits")
        if self.environment == "production":
            if self.auth_disabled:
                errors.append("AGENTPAL_AUTH_MODE=disabled_insecure_dev is refused in "
                              "production")
            if not self.api_tokens_file:
                errors.append("production requires AGENTPAL_API_TOKENS_FILE")
            if spend_policy_configuration == "demo":
                errors.append("production refuses a spend policy labelled \"demo\"")
            if not self.require_model_integrity or not self.require_read_only_model_storage:
                errors.append("production requires model integrity and read-only storage")
            if self.db_path == ":memory:":
                errors.append("production refuses an in-memory database (audit loss)")
            if any(h not in LOOPBACK_HOSTNAMES for h in self.allowed_hosts) and not hosted:
                errors.append("production allows only loopback Host names in this phase")
        if errors:
            raise UnsafeConfiguration("; ".join(errors))
        if self.auth_disabled:
            warnings.append("AUTHENTICATION AND AUTHORIZATION ARE DISABLED "
                            "(AGENTPAL_AUTH_MODE=disabled_insecure_dev): any local process "
                            "can assess as any agent and read every assessment. "
                            "Loopback demo use only.")
        elif not self.api_tokens_file:
            warnings.append("legacy /assess API: AGENTPAL_API_TOKENS_FILE is not set, so its "
                            "authenticated endpoints answer 401 (the /v1 platform and the "
                            "dashboard use owner tokens and agent keys and are unaffected)")
        if spend_policy_configuration == "demo":
            warnings.append("legacy /assess API: the spend policy is a DEMO configuration "
                            "(placeholder budgets); /v1 agents use the wallet limits you set "
                            "in the dashboard")
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
