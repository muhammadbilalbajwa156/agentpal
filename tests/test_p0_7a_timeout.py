"""P0.7a regression: AGENTPAL_MODEL_TIMEOUT_SECONDS bounds every inference backend.

Before the fix the timeout setting was applied only to Ollama's HTTP client; llama.cpp
inference (and any backend that hangs) could block a request indefinitely.
"""

from __future__ import annotations

import json
import threading
import time

import httpx

from agentpal.analyzers import LlamaCppAnalyzer, OllamaAnalyzer
from agentpal.analyzers import llm as llm_module
from agentpal.config import Settings
from agentpal.orchestrator import assess
from agentpal.service import RiskService

from conftest import FakeAnalyzer, analysis, ctx, make_request

TIMEOUT = 0.3


class SlowAnalyzer(FakeAnalyzer):
    name = "slow"

    def __init__(self, seconds: float):
        super().__init__(analysis(score=90, rec="require_review"))
        self.seconds = seconds

    def analyze(self, data):
        time.sleep(self.seconds)
        return super().analyze(data)


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def test_hanging_backend_is_bounded_and_falls_back(policy):
    a, elapsed = timed(lambda: assess(make_request(), ctx(), policy, [SlowAnalyzer(5)],
                                      model_timeout_s=TIMEOUT))
    assert elapsed < TIMEOUT + 1.0, elapsed
    assert a.model.backend == "heuristic" and a.model.is_fallback
    assert any("timeout" in e for e in a.model.errors)
    # owner decision 5 (replaces the P0.7a "fall back without escalating" default): the
    # assessment is degraded and, with material risk left (MISSING_EVIDENCE), needs review
    assert a.degraded and a.rule_score > 0
    assert a.recommendation == "require_review"


def test_degraded_review_threshold_is_configurable_after_timeout(policy):
    lenient = policy.model_copy(deep=True)
    lenient.recommendation.degraded_mode.review_if_rule_score_at_or_above = 101
    a = assess(make_request(), ctx(), lenient, [SlowAnalyzer(5)], model_timeout_s=TIMEOUT)
    assert a.degraded and "MODEL_UNAVAILABLE_OR_TIMED_OUT" in {s.code for s in a.signals}
    assert a.recommendation == "allow_for_review"  # rules alone found no review condition


def test_fast_backend_within_timeout_is_used(policy):
    a = assess(make_request(), ctx(), policy, [SlowAnalyzer(0.01)], model_timeout_s=TIMEOUT)
    assert a.model.backend == "slow"


class FakeLlama:
    """Mirrors the llama-cpp-python 0.3.x `create_chat_completion` parameters actually
    used (no **kwargs, so an unsupported argument fails here exactly as in production)
    and streams one 'token' per 0.1 s."""

    def __init__(self):
        self.stopped_early = threading.Event()

    def create_chat_completion(self, messages, response_format=None, temperature=0.2,
                               max_tokens=None, stream=False):
        assert stream is True

        def gen():
            try:
                for _ in range(500):  # ~50 s of generation if never stopped
                    time.sleep(0.1)
                    yield {"choices": [{"delta": {"content": " "}}]}
                yield {"choices": [{"delta": {"content": json.dumps(
                    {"risk_score": 5, "uncertainty": 0.1,
                     "recommendation": "allow_for_review"})}}]}
            except GeneratorExit:
                self.stopped_early.set()  # generation was stopped by the consumer
                raise

        return gen()


def test_llamacpp_inference_is_bounded_and_cooperatively_stopped(policy, tmp_path,
                                                                 monkeypatch):
    model_file = tmp_path / "fake.gguf"
    model_file.write_bytes(b"GGUF-test")
    fake = FakeLlama()
    monkeypatch.setitem(llm_module._LLAMA_CACHE, str(model_file), fake)
    analyzer = LlamaCppAnalyzer(model_file, require_integrity=False)
    monkeypatch.setattr(analyzer, "available", lambda: True)
    a, elapsed = timed(lambda: assess(make_request(), ctx(), policy, [analyzer],
                                      model_timeout_s=TIMEOUT))
    assert elapsed < TIMEOUT + 1.0, elapsed
    assert a.model.backend == "heuristic"
    # the generation loop itself was asked to stop (not merely abandoned)
    assert fake.stopped_early.wait(2.0)


def test_ollama_slow_response_is_bounded(policy):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m"}]})
        time.sleep(3)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ollama = OllamaAnalyzer("http://ollama.test", "m", timeout=TIMEOUT, client=client,
                            require_integrity=False)
    a, elapsed = timed(lambda: assess(make_request(), ctx(), policy, [ollama],
                                      model_timeout_s=TIMEOUT))
    assert elapsed < TIMEOUT + 1.0, elapsed
    assert a.model.backend == "heuristic"


def test_setting_reaches_the_service(policy):
    s = RiskService.from_settings(Settings(model_timeout_seconds=0.25, db_path=":memory:",
                                           analyzer_chain=["heuristic"]))
    assert s.model_timeout_s == 0.25
