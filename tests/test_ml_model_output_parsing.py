"""Defensive parsing of generative-model output, and the Qwen3 inference contract.

Malformed, oversized or schema-invalid output must be rejected, and a rejected output
must leave the assessment degraded and at least as strict as rules-only: it can never
relax a recommendation or produce an allow that the rules would not give."""

from __future__ import annotations

import json

import pytest

from agentpal.analyzers import HeuristicAnalyzer, MalformedModelOutput, parse_model_output
from agentpal.analyzers import base as base_module
from agentpal.analyzers import llm as llm_module
from agentpal.analyzers.llm import LlamaCppAnalyzer, install_no_think_handler
from agentpal.orchestrator import assess

from conftest import FakeAnalyzer, ctx, make_request

GOOD = {"risk_score": 12, "uncertainty": 0.2, "recommendation": "allow_for_review",
        "findings": [], "missing_information": [], "summary": "ok"}
GOOD_TEXT = json.dumps(GOOD)
EMPTY_THINK = "<think>\n\n</think>\n\n"


# ------------------------------------------------------------------ accepted shapes

@pytest.mark.parametrize("raw", [
    GOOD_TEXT,
    "  \n" + GOOD_TEXT + "\n ",
    EMPTY_THINK + GOOD_TEXT,                                  # Qwen3, thinking disabled
    "<think>The amount is small.</think>" + GOOD_TEXT,       # bounded reasoning, stripped
    "```json\n" + GOOD_TEXT + "\n```",
    EMPTY_THINK + "```json\n" + GOOD_TEXT + "\n```",
])
def test_accepts_exactly_one_json_object_with_optional_think_and_fence(raw):
    out = parse_model_output(raw)
    assert out.risk_score == 12 and out.recommendation == "allow_for_review"


# ------------------------------------------------------------------ rejected shapes

REJECTED = {
    "prose_before": "Here is my analysis: " + GOOD_TEXT,
    "prose_after": GOOD_TEXT + " Let me know if you need more.",
    "two_objects": GOOD_TEXT + GOOD_TEXT,
    "array": json.dumps([GOOD]),
    "unclosed_think": "<think>reasoning that never ends " + GOOD_TEXT,
    "two_think_blocks": EMPTY_THINK + EMPTY_THINK + GOOD_TEXT,
    "think_after_json": GOOD_TEXT + "<think></think>",
    "think_inside_json": json.dumps({**GOOD, "summary": "<think>x</think>"}),
    "oversized_think": "<think>" + "x" * (base_module.MAX_THINK_CHARS + 1) + "</think>"
                       + GOOD_TEXT,
    "oversized_output": json.dumps({**GOOD, "summary": "ok"}) + " " * 40_000,
    "duplicate_key_relaxing": GOOD_TEXT[:-1] + ', "risk_score": 0}',
    "nan": GOOD_TEXT.replace("0.2", "NaN"),
    "infinity": GOOD_TEXT.replace("12", "Infinity"),
    "string_score": json.dumps({**GOOD, "risk_score": "12"}),
    "bool_uncertainty": json.dumps({**GOOD, "uncertainty": True}),
    "float_score": json.dumps({**GOOD, "risk_score": 12.5}),
    "score_out_of_range": json.dumps({**GOOD, "risk_score": 101}),
    "unknown_recommendation": json.dumps({**GOOD, "recommendation": "approve"}),
    "missing_required": json.dumps({"risk_score": 3}),
    "too_many_findings": json.dumps({**GOOD, "findings": [
        {"code": "X", "description": "d"}] * 13}),
    "oversized_finding": json.dumps({**GOOD, "findings": [
        {"code": "X", "description": "d" * 501}]}),
    "oversized_missing_item": json.dumps({**GOOD, "missing_information": ["m" * 501]}),
    "non_string_missing_item": json.dumps({**GOOD, "missing_information": [{"a": 1}]}),
    "deep_nesting": '{"a":' * 5000 + "1" + "}" * 5000,
    "two_fences": "```json\n" + GOOD_TEXT + "\n```\n```json\n" + GOOD_TEXT + "\n```",
    "empty": "   ",
    "not_a_string": None,
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejects_malformed_oversized_or_schema_invalid_output(name):
    with pytest.raises(MalformedModelOutput):
        parse_model_output(REJECTED[name])


# ------------------------------------------------------------------ never relaxes rules

@pytest.mark.parametrize("name", sorted(k for k, v in REJECTED.items() if isinstance(v, str)))
@pytest.mark.parametrize("request_kind", ["routine", "risky"])
def test_rejected_output_is_degraded_and_never_less_strict_than_rules(policy, name,
                                                                      request_kind):
    req = (make_request() if request_kind == "routine"
           else make_request(merchant_domain="examp1e-billing.com",
                             purpose="Gift cards for the team"))
    rules_only = assess(req, ctx(), policy, [HeuristicAnalyzer()])
    a = assess(req, ctx(), policy, [FakeAnalyzer(raw=REJECTED[name])])
    order = ["allow_for_review", "require_review", "block"]
    assert a.model.backend == "heuristic"
    assert a.degraded and "malformed output" in a.degraded_reason
    assert order.index(a.recommendation) >= order.index(rules_only.recommendation)
    assert a.hard_violations == rules_only.hard_violations
    if rules_only.rule_score >= 1:  # owner decision F1: degraded + rule points -> review
        assert a.recommendation != "allow_for_review"


# ------------------------------------------------------------------ Qwen3 thinking switch

QWEN3_LIKE_TEMPLATE = (
    "{% for m in messages %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n"
    "{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant\n"
    "{% if enable_thinking is defined and enable_thinking is false %}"
    "<think>\n\n</think>\n\n{% endif %}{% endif %}")


class _Model:
    def token_get_text(self, i):
        return {1: "<|im_end|>", 2: ""}[i]


class _FakeGGUF:
    """Only what install_no_think_handler and the handler's render step touch."""

    def __init__(self, template):
        self.metadata = {"tokenizer.chat_template": template} if template else {}
        self._model = _Model()
        self.chat_handler = None

    def token_eos(self):
        return 1

    def token_bos(self):
        return -1


def _rendered_prompt(fake, monkeypatch) -> str:
    pytest.importorskip("llama_cpp")
    seen = {}

    def tokenize(text, add_bos=True, special=False):
        seen["prompt"] = text.decode() if isinstance(text, bytes) else text
        raise RuntimeError("rendered")  # stop before any inference

    fake.tokenize = tokenize
    with pytest.raises(RuntimeError, match="rendered"):
        fake.chat_handler(llama=fake, messages=[{"role": "system", "content": "S"},
                                                {"role": "user", "content": "U"}])
    return seen["prompt"]


def test_no_think_handler_renders_the_empty_think_block(monkeypatch):
    pytest.importorskip("llama_cpp")  # optional dependency (not in requirements-dev)
    fake = _FakeGGUF(QWEN3_LIKE_TEMPLATE)
    assert install_no_think_handler(fake) is True
    prompt = _rendered_prompt(fake, monkeypatch)
    assert prompt.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_no_think_handler_leaves_templates_without_the_switch_unchanged(monkeypatch):
    pytest.importorskip("llama_cpp")  # optional dependency (not in requirements-dev)
    plain = QWEN3_LIKE_TEMPLATE.split("{% if enable_thinking")[0] + "{% endif %}"
    fake = _FakeGGUF(plain)
    assert install_no_think_handler(fake) is True
    assert _rendered_prompt(fake, monkeypatch).endswith("<|im_start|>assistant\n")


def test_no_template_in_gguf_installs_nothing():
    fake = _FakeGGUF(None)
    assert install_no_think_handler(fake) is False and fake.chat_handler is None


class _CapturingLlama:
    def __init__(self, reply):
        self.reply, self.messages = reply, None

    def create_chat_completion(self, messages, response_format=None, temperature=0.2,
                               max_tokens=None, stream=False):
        self.messages = messages
        return iter([{"choices": [{"delta": {"content": self.reply}}]}])


@pytest.mark.parametrize("include", [False, True])
def test_llamacpp_serves_with_the_configured_rule_findings_setting(policy, tmp_path,
                                                                  monkeypatch, include):
    model_file = tmp_path / "m.gguf"
    model_file.write_bytes(b"GGUF-test")
    fake = _CapturingLlama(EMPTY_THINK + GOOD_TEXT)
    monkeypatch.setitem(llm_module._LLAMA_CACHE, str(model_file), fake)
    an = LlamaCppAnalyzer(model_file, require_integrity=False, include_rule_findings=include)
    monkeypatch.setattr(an, "available", lambda: True)
    a = assess(make_request(), ctx(), policy, [an])
    assert a.model.backend == "llamacpp" and not a.degraded
    assert ("deterministic_rule_findings" in fake.messages[1]["content"]) is include


# ------------------------------------------------------------------ grammar quirks (ML-14)

def test_raw_newline_inside_a_string_value_is_accepted_but_still_schema_checked():
    """llama-cpp-python 0.3.36's JSON-schema grammar lets a raw newline into a string;
    it carries no meaning, so it is accepted, while every schema bound still applies."""
    raw = json.dumps({**GOOD, "findings": [{"code": "X", "description": "a<NL>b"}]})
    out = parse_model_output(raw.replace("<NL>", "\n"))
    assert out.findings[0].description == "a\nb"
    too_long = json.dumps({**GOOD, "summary": "s" * 1001}).replace("ss", "s\ns", 1)
    with pytest.raises(MalformedModelOutput):
        parse_model_output(too_long)


@pytest.mark.parametrize("payload", [
    {**GOOD, "risk_score": -137},                      # the grammar does not enforce bounds
    {**GOOD, "uncertainty": -1.3e33},
    {**GOOD, "uncertainty": 2},
    {**GOOD, "findings": [{"code": "C" * 65, "description": "d"}]},
])
def test_out_of_bounds_values_from_an_unbounded_grammar_are_rejected(payload):
    with pytest.raises(MalformedModelOutput):
        parse_model_output(json.dumps(payload))


def test_nested_findings_validate_in_strict_mode():
    out = parse_model_output(json.dumps({**GOOD, "findings": [
        {"code": "PROMPT_INJECTION", "description": "d", "evidence": "e"}]}))
    assert out.findings[0].code == "PROMPT_INJECTION"
