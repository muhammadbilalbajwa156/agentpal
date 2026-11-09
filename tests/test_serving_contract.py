"""Serving must match training (ML-09): the llama.cpp analyzer's prompt includes rule
findings only if configured to, and the switch reaches the analyzer from the environment.
The `evaluate` CLI defaults to the validation split (the test split is retired)."""

from __future__ import annotations

from agentpal.__main__ import main as cli
from agentpal.analyzers import build_analyzer_chain
from agentpal.config import Settings


def test_rule_findings_switch_reaches_the_llamacpp_analyzer(monkeypatch):
    monkeypatch.setenv("AGENTPAL_LLAMACPP_INCLUDE_RULE_FINDINGS", "false")
    monkeypatch.setenv("AGENTPAL_ANALYZER_CHAIN", "llamacpp,heuristic")
    s = Settings.from_env()
    assert s.llamacpp_include_rule_findings is False
    llm = next(a for a in build_analyzer_chain(s) if a.name == "llamacpp")
    assert llm.include_rule_findings is False
    # the pinned llama.cpp model is the fine-tuned one, trained without rule findings
    assert Settings().llamacpp_include_rule_findings is False
    monkeypatch.setenv("AGENTPAL_LLAMACPP_INCLUDE_RULE_FINDINGS", "true")
    assert Settings.from_env().llamacpp_include_rule_findings is True


def test_evaluate_cli_defaults_to_validation(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_evaluate(data, ml, report, split, **kw):
        seen["split"] = split
        return {"notice": "stub", "split_status": "in_use", "approaches": {}}

    monkeypatch.setattr("agentpal.evaluation.evaluate", fake_evaluate)
    try:
        cli(["evaluate", "--report", str(tmp_path / "r.json")])
    except Exception:  # the stub report has no metrics to print; only the split matters
        pass
    assert seen["split"] == "validation"
