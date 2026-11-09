"""Reviewed demo configuration: companies, agents, budgets, capabilities and errands.

Everything here is trusted configuration (version-controlled). Freelancer payees are bound
ONLY here; model output and catalogue text can never add or change a payee.
"""

from __future__ import annotations

from dataclasses import dataclass, field



@dataclass(frozen=True)
class Payee:
    payee_id: str
    name: str
    email: str
    trade: str


# Sandbox PERSONAL payees from agentpal/demo/paypal_sandbox_personas.json (fake money).
FREELANCERS = {
    "priya-shah": Payee("priya-shah", "Priya Shah", "agentpal.priya.cbb860@personal.example.com",
                        "freelance designer"),
    "tom-walsh": Payee("tom-walsh", "Tom Walsh", "agentpal.tom.53025e@personal.example.com",
                       "freelance translator"),
}


@dataclass(frozen=True)
class Invoice:
    payee_id: str
    amount: str
    work: str


@dataclass(frozen=True)
class Errand:
    errand_id: str
    goal: str                       # trusted business purpose; becomes the request purpose
    budget: str                     # errand ceiling (USD); wallet limits still apply
    channel3: bool = False          # may search Channel3
    vendors: tuple[str, ...] = ()   # catalogue vendors this errand may buy from
    invoices: tuple[Invoice, ...] = ()  # payout errands: approved, delivered work
    search_query: str = ""          # hint for the scripted (no-model) planner
    preferred_item: str = ""        # hint for the scripted planner
    demo: str = ""                  # which AgentPal behaviour this errand shows

    @property
    def kind(self) -> str:
        return "payout" if self.invoices else "payment"


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    mission: str
    monthly_budget: str
    per_transaction_limit: str
    errands: tuple[Errand, ...]

    @property
    def capabilities(self) -> list[str]:
        """Minimal grants derived from the errands (execution covers sandbox payouts too)."""
        kinds = {e.kind for e in self.errands}
        caps = ["wallet.read"]
        if "payment" in kinds:
            caps += ["payment.prepare", "owner.request_decision"]
        caps.append("payment.execute_sandbox")
        if "payout" in kinds:
            caps.append("payout.request")
        return caps


@dataclass(frozen=True)
class Company:
    company_id: str
    name: str
    tagline: str
    agents: tuple[AgentSpec, ...] = field(default_factory=tuple)

    def agent(self, key: str) -> AgentSpec:
        for agent in self.agents:
            if agent.key == key:
                return agent
        raise KeyError(key)

    def slug(self, agent: AgentSpec) -> str:
        return f"{self.company_id}-{agent.key}"

    def agentpal_name(self, agent: AgentSpec) -> str:
        return f"{self.name} · {agent.name}"


NORTHSTAR = Company("northstar", "Northstar Digital",
                    "An autonomous marketing and web-building agency", (
    AgentSpec("atlas", "Atlas · Ads & Media Buyer",
              "Buys marketing gear for client shoots. Budget-conscious: cheapest suitable "
              "in-stock offer wins.", "40.00", "25.00", (
        Errand("ring-light", "Compact LED ring light for client video shoots", "25.00",
               channel3=True, search_query="LED ring light",
               demo="Channel3 search; owner approves exact terms; advisory market check."),
        Errand("lav-mic", "Clip-on lavalier microphone for podcast ad recordings", "25.00",
               channel3=True, search_query="lavalier microphone",
               demo="Second request draws down the same wallet: remaining allowance shrinks."),
    )),
    AgentSpec("forge", "Forge · Web Builder",
              "Buys website tooling for client launches from the CloudDesk Software vendor.",
              "40.00", "20.00", (
        Errand("hosting", "Starter hosting plan for the Acme Bakery client site", "12.00",
               vendors=("clouddesk",), preferred_item="clouddesk-hosting-starter",
               demo="Payee-bound catalogue purchase: merchant_paypal_email is part of "
                    "terms_sha256."),
        Errand("theme", "Website theme license for the Acme Bakery client site", "15.00",
               vendors=("clouddesk",), preferred_item="clouddesk-theme-license",
               demo="The 24.00 bundle is above the errand budget and never offered."),
    )),
    AgentSpec("muse", "Muse · Content Studio",
              "Buys content-production supplies for the studio.", "35.00", "20.00", (
        Errand("sd-card", "128GB SD card for the studio content camera", "20.00",
               channel3=True, search_query="128GB SD card",
               demo="Channel3 search within a per-transaction limit of 20.00."),
        Errand("reflector", "Collapsible photo reflector for product shots", "20.00",
               channel3=True, search_query="collapsible photo reflector",
               demo="Owner may reject; rejection is final for the errand."),
    )),
    AgentSpec("ledger", "Ledger · Freelancer Payroll",
              "Pays approved freelancers for delivered work via PayPal sandbox Payouts.",
              "60.00", "25.00", (
        Errand("pay-priya", "Payout to Priya Shah for the Acme Bakery logo refresh", "20.00",
               invoices=(Invoice("priya-shah", "20.00", "Logo refresh for Acme Bakery"),),
               demo="Payout to a config-bound payee; owner approves the exact terms."),
        Errand("pay-tom", "Payout to Tom Walsh for the Spanish landing-page translation",
               "15.00",
               invoices=(Invoice("tom-walsh", "15.00", "Spanish translation of landing page"),),
               demo="Payout; an invented or model-supplied email is refused locally."),
    )),
))

LUMEN = Company("lumen", "Lumen Cloud", "An autonomous SaaS company", (
    AgentSpec("orbit", "Orbit · DevOps",
              "Buys infrastructure and developer tooling seats and small hardware.",
              "60.00", "25.00", (
        Errand("ci-seat", "CI/CD pipeline seat for the new backend engineer", "12.00",
               vendors=("clouddesk",), preferred_item="clouddesk-ci-seat",
               demo="Payee-bound SaaS seat from CloudDesk Software."),
        Errand("usb-hub", "USB-C hub for the on-call laptop", "20.00",
               vendors=("bluepeak",), preferred_item="bluepeak-usb-c-hub",
               demo="Payee-bound hardware purchase from Bluepeak Electronics."),
    )),
    AgentSpec("pulse", "Pulse · Growth",
              "Buys event and marketing items for community meetups.", "50.00", "25.00", (
        Errand("lanyards", "Conference lanyards for the community meetup", "15.00",
               channel3=True, search_query="conference lanyards pack",
               demo="Channel3 search; ordinary approval."),
        Errand("speaker-deal", "Portable Bluetooth speaker for the meetup booth "
               "(Shady Deals Outlet clearance offer)", "10.00",
               vendors=("shadydeals",), preferred_item="shadydeals-jbl-speaker",
               demo="Too good to be true: suspicious TLD, unapproved merchant and a Channel3 "
                    "market check far below market. The owner rejects it on camera."),
    )),
    AgentSpec("keel", "Keel · Finance Ops",
              "Keeps the office stocked and pays contractors for approved work.",
              "50.00", "20.00", (
        Errand("office-supplies", "Notebooks for the quarterly planning offsite", "10.00",
               vendors=("northwind",), preferred_item="northwind-notebooks",
               demo="Payee-bound office supplies from Northwind Office Supply."),
        Errand("pay-contractor", "Payout to Priya Shah for onboarding illustrations",
               "18.00",
               invoices=(Invoice("priya-shah", "18.00", "Onboarding illustrations"),),
               demo="Contractor payout from a finance agent with payout.request."),
    )),
))

COMPANIES = {c.company_id: c for c in (NORTHSTAR, LUMEN)}
