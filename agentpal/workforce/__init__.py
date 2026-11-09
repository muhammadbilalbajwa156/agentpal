"""AgentPal workforce: one registry, one permission model, one orchestrator for six roles
(research, shopping, finance, communication, verification, payment).

See docs/WORKFORCE_ARCHITECTURE.md. Import submodules directly (`workforce.roles`,
`workforce.orchestrator`); this package module stays import-free so the wallet can depend on
`workforce.roles` without a cycle.
"""
