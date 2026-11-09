"""Kaggle readiness of training/train_lora.py and training/hwcheck.py, without PyTorch:
precision selection (mocked torch capability calls), the VRAM gate, checkpoint
completeness for --resume, atomic publication of final/ with a hash marker, and the
thinking-disabled prompt contract. An optional real tiny-model run is in
test_training_smoke.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from training import hwcheck, train_lora
from training.train_lora import (EMPTY_THINK, TrainConfig, check_gpu, latest_complete_checkpoint,
                                 publish_final, select_precision, to_prompt_completion,
                                 verify_final)


class FakeCuda:
    def __init__(self, available=True, cap=(7, 5), bf16=False, kwarg=True,
                 mem=15_828_320_256, name="Tesla T4", count=1):
        self._a, self._cap, self._bf16, self._kwarg = available, cap, bf16, kwarg
        self._mem, self._name, self._count = mem, name, count

    def is_available(self):
        return self._a

    def get_device_capability(self, i=0):
        return self._cap

    def is_bf16_supported(self, **kw):
        if kw and not self._kwarg:
            raise TypeError("unexpected keyword")
        if not kw and not self._kwarg:  # old torch: counts emulation
            return True
        return self._bf16 if kw.get("including_emulation") is False else True

    def get_device_properties(self, i=0):
        return SimpleNamespace(name=self._name, total_memory=self._mem)

    def device_count(self):
        return self._count


def torch_with(**kw):
    return SimpleNamespace(cuda=FakeCuda(**kw), __version__="2.x-test",
                           version=SimpleNamespace(cuda="12.x"))


# ------------------------------------------------------------------ precision

@pytest.mark.parametrize("kw,expected", [
    ({"cap": (8, 0), "bf16": True, "name": "A100"}, "bf16"),      # Ampere: native bf16
    ({"cap": (8, 9), "bf16": True, "name": "L4"}, "bf16"),
    ({"cap": (7, 5), "bf16": False}, "fp16"),                      # Kaggle T4
    ({"cap": (7, 5), "kwarg": False}, "fp16"),                     # old torch says True (emulated)
    ({"cap": (6, 0), "bf16": False, "name": "Tesla P100"}, "fp16"),  # Kaggle P100
    ({"available": False}, "fp32"),                                # CPU smoke test
])
def test_auto_precision_uses_bf16_only_with_native_support(kw, expected):
    assert select_precision(torch_with(**kw), "auto") == expected


@pytest.mark.parametrize("kw,requested", [
    ({"cap": (7, 5), "bf16": False}, "bf16"),
    ({"available": False}, "fp16"),
    ({"available": False}, "bf16"),
    ({"cap": (8, 0), "bf16": True}, "int8"),
])
def test_impossible_precision_requests_are_refused(kw, requested):
    with pytest.raises(ValueError):
        select_precision(torch_with(**kw), requested)


def test_explicit_fp32_is_always_allowed():
    assert select_precision(torch_with(cap=(7, 5)), "fp32") == "fp32"


# ------------------------------------------------------------------ GPU gate

@pytest.mark.parametrize("mem,name", [(15_828_320_256, "Tesla T4"),
                                      (17_071_734_784, "Tesla P100-PCIE-16GB")])
def test_kaggle_gpus_pass_the_vram_gate(mem, name):
    assert check_gpu(torch_with(mem=mem, name=name))["device"] == "cuda"


def test_small_gpu_and_missing_gpu_are_refused():
    with pytest.raises(SystemExit, match="VRAM"):
        check_gpu(torch_with(mem=12_000_000_000, name="small"))
    with pytest.raises(SystemExit, match="no CUDA GPU"):
        check_gpu(torch_with(available=False))
    assert check_gpu(torch_with(available=False), allow_cpu=True)["device"] == "cpu"


def test_hwcheck_reports_and_refuses(tmp_path):
    ok = hwcheck.check(tmp_path, torch=torch_with(count=2), disk_free=50 * 10**9,
                       ram_free=30 * 10**9)
    assert ok["ok"] and ok["precision"] == "fp16" and len(ok["gpus"]) == 2
    assert "GPU 0 only" in ok["note"]
    bad = hwcheck.check(tmp_path, torch=torch_with(mem=8 * 10**9), disk_free=5 * 10**9,
                        ram_free=2 * 10**9, export=True)
    assert not bad["ok"]
    joined = " ".join(bad["problems"])
    assert "VRAM" in joined and "free at" in joined and "RAM" in joined
    cpu = hwcheck.check(tmp_path, torch=torch_with(available=False), disk_free=50 * 10**9,
                        ram_free=30 * 10**9)
    assert not cpu["ok"] and "no CUDA GPU" in cpu["problems"][0]
    assert hwcheck.check(tmp_path, torch=torch_with(available=False), allow_cpu=True,
                         disk_free=50 * 10**9, ram_free=30 * 10**9)["ok"]


# ------------------------------------------------------------------ config

def test_config_pins_the_verified_base_revision_and_kaggle_sizing():
    cfg = TrainConfig()
    assert cfg.base_model == "Qwen/Qwen3-1.7B"
    assert cfg.base_revision == "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
    assert cfg.per_device_batch * cfg.grad_accum == 16 and cfg.gradient_checkpointing
    assert cfg.precision == "auto" and cfg.enable_thinking is False
    assert cfg.include_rule_findings is False and cfg.save_every_minutes <= 30


# ------------------------------------------------------------------ checkpoints

def _ckpt(root: Path, step: int, complete: bool = True) -> Path:
    p = root / f"checkpoint-{step}"
    p.mkdir(parents=True)
    (p / "adapter_model.safetensors").write_bytes(b"x")
    if complete:
        (p / "trainer_state.json").write_text("{}")
    return p


def test_resume_picks_the_latest_complete_checkpoint(tmp_path):
    assert latest_complete_checkpoint(tmp_path / "none") is None
    _ckpt(tmp_path, 20)
    best = _ckpt(tmp_path, 100)
    _ckpt(tmp_path, 120, complete=False)        # interrupted while saving
    (tmp_path / "checkpoint-junk").mkdir()
    assert latest_complete_checkpoint(tmp_path) == best


def test_final_is_published_atomically_with_hashes(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    tmp = out / "final.tmp-1"
    tmp.mkdir()
    (tmp / "adapter_model.safetensors").write_bytes(b"weights")
    (tmp / "sub").mkdir()
    (tmp / "sub" / "a.json").write_text("{}")
    final = publish_final(tmp, out / "final", {"run_kind": "training"})
    assert not tmp.exists() and final.is_dir()
    marker = verify_final(final)
    assert set(marker["files_sha256"]) == {"adapter_model.safetensors", "sub/a.json"}
    # a second run keeps the previous final/ instead of overwriting it
    tmp2 = out / "final.tmp-2"
    tmp2.mkdir()
    (tmp2 / "adapter_model.safetensors").write_bytes(b"weights-2")
    publish_final(tmp2, out / "final", {"run_kind": "training"})
    assert len(list(out.glob("final.prev-*"))) == 1
    # tampering is detected
    (final / "adapter_model.safetensors").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="differ"):
        verify_final(final)


def test_unfinished_final_is_rejected(tmp_path):
    (tmp_path / "final").mkdir()
    with pytest.raises(ValueError, match="COMPLETE.json"):
        verify_final(tmp_path / "final")


# ------------------------------------------------------------------ prompt contract

class FakeTokenizer:
    eos_token = "<|im_end|>"

    def __init__(self, think_block=True):
        self.think_block = think_block
        self.kwargs = None

    def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kw):
        self.kwargs = {"tokenize": tokenize, "add_generation_prompt": add_generation_prompt,
                       **kw}
        body = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n"
                       for m in messages)
        tail = EMPTY_THINK if (self.think_block and kw.get("enable_thinking") is False) else ""
        return body + "<|im_start|>assistant\n" + tail


RECORD = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
          "target": json.dumps({"risk_score": 5, "uncertainty": 0.1,
                                "recommendation": "allow_for_review"})}


def test_prompt_is_rendered_with_thinking_disabled_and_completion_ends_with_eos():
    tok = FakeTokenizer()
    ex = to_prompt_completion(RECORD, tok, TrainConfig())
    assert tok.kwargs == {"tokenize": False, "add_generation_prompt": True,
                          "enable_thinking": False}
    assert ex["prompt"].endswith("<|im_start|>assistant\n" + EMPTY_THINK)
    assert ex["completion"] == RECORD["target"] + "<|im_end|>"


def test_template_without_the_no_think_block_is_refused():
    with pytest.raises(ValueError, match="think"):
        to_prompt_completion(RECORD, FakeTokenizer(think_block=False), TrainConfig())


def test_wrong_eos_is_refused():
    tok = FakeTokenizer()
    tok.eos_token = "</s>"
    with pytest.raises(ValueError, match="eos"):
        to_prompt_completion(RECORD, tok, TrainConfig())


def test_smoke_config_is_labelled_as_a_smoke_test():
    cfg = train_lora.smoke_config("tiny", "0" * 40)
    assert cfg.run_kind == "pipeline-smoke-test"
    assert TrainConfig().run_kind == "training"
