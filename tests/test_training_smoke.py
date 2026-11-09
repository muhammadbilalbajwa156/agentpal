"""PIPELINE SMOKE TEST (opt-in): a real LoRA run of training/train_lora.py on CPU with a
tiny random Qwen3 test model, interrupted and resumed, then the evaluation path on its
adapter. It checks that the code runs; it trains nothing meaningful.

Skipped unless PyTorch + transformers + peft + trl are installed AND the environment
variable AGENTPAL_TINY_MODEL_DIR points to a local copy of
trl-internal-testing/tiny-Qwen3ForCausalLM (so the suite never downloads anything):

    AGENTPAL_TINY_MODEL_DIR=/path/to/tiny-Qwen3ForCausalLM python -m pytest \
        tests/test_training_smoke.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

TINY = os.environ.get("AGENTPAL_TINY_MODEL_DIR")
for mod in ("torch", "transformers", "peft", "trl", "datasets"):
    pytest.importorskip(mod)
if not TINY or not Path(TINY).is_dir():
    pytest.skip("set AGENTPAL_TINY_MODEL_DIR to a local tiny Qwen3 model to run",
                allow_module_level=True)

from training import evaluate_model, sft_data, train_lora  # noqa: E402


@pytest.fixture(scope="module")
def sft(tmp_path_factory):
    out = tmp_path_factory.mktemp("sft")
    sft_data.build(out)
    return out


def test_tiny_model_trains_interrupts_resumes_and_evaluates(sft, tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    out = tmp_path / "run"
    common = ["--data", str(sft), "--output-dir", str(out), "--smoke-model", TINY,
              "--limit", "8", "4", "--max-steps", "4"]
    train_lora.main(common + ["--interrupt-after-step", "2"])
    ckpts = out / "checkpoints"
    assert train_lora.latest_complete_checkpoint(ckpts).name == "checkpoint-2"
    assert not (out / "final").exists()          # interrupted: nothing published
    with pytest.raises(SystemExit):               # refuses to mix runs without --resume
        train_lora.main(common)
    train_lora.main(common + ["--resume"])
    marker = train_lora.verify_final(out / "final")
    assert marker["run_kind"] == "pipeline-smoke-test"
    assert marker["global_step"] == 4 and marker["resumed_from"].endswith("checkpoint-2")
    assert marker["inference_contract"]["enable_thinking"] is False
    log = [json.loads(x) for x in (out / "logs" / "train_log.jsonl").read_text().splitlines()]
    assert any("eval_loss" in r for r in log)

    rep = tmp_path / "reports"
    evaluate_model.main(["--adapter", str(out / "final"), "--data", str(sft), "--out",
                         str(rep), "--limit-dev", "3", "--max-bench-cases", "2",
                         "--max-bench-episodes", "1"])
    r = json.loads((rep / "eval_finetuned.json").read_text())
    assert r["meta"]["banner"].startswith("PIPELINE SMOKE TEST")
    s = r["sft_dev"]
    assert s["units"] == 3 and s["never_less_strict_than_rules"]
    # whatever the tiny model emits, an invalid output degrades to rules, never relaxes
    assert s["validity"]["invalid"] == 0 or s["rules_plus_model_degraded_rate"] > 0
    assert r["bench_v2_dev"]["units"] >= 2
