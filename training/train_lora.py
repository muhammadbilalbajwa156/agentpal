"""LoRA fine-tuning of the single AgentPal base model (Qwen3-1.7B) on the SFT dataset.

Status: the training LOOP was verified only as a PIPELINE SMOKE TEST on CPU with a tiny
random Qwen3 test model (trl-internal-testing/tiny-Qwen3ForCausalLM), a few steps, a
handful of rows, including interrupt + --resume. That is NOT model training and says
nothing about model quality. The real Qwen3-1.7B run is for a GPU machine (Kaggle).

    # validate data + config, print estimates; imports no ML framework
    python -m training.train_lora --data build/sft --dry-run [--tokenizer DIR]
    # Kaggle (one GPU; outputs persist under /kaggle/working)
    python -m training.hwcheck --output-dir /kaggle/working/agentpal-lora
    python -m training.train_lora --data build/sft --output-dir /kaggle/working/agentpal-lora
    python -m training.train_lora --data build/sft --output-dir /kaggle/working/agentpal-lora \
        --resume                      # continue from the latest COMPLETE checkpoint

Inference contract (must match serving, agentpal/analyzers/llm.py):
  * prompt = tokenizer chat template with add_generation_prompt=True and
    enable_thinking=False, so it ends with an empty `<think>\\n\\n</think>\\n\\n` block;
  * completion = the target JSON + eos (`<|im_end|>`); loss on the completion only;
  * input built by build_messages(include_rule_findings=False).

Output layout (one --output-dir):
  checkpoints/checkpoint-<step>/   Trainer checkpoints (adapter, optimizer, rng, state);
                                   complete only when trainer_state.json exists (written
                                   last), so --resume skips a half-written checkpoint
  final/                           best adapter (lowest dev loss) + tokenizer +
                                   train_config.json + COMPLETE.json (SHA-256 of every
                                   file). Written to final.tmp-<pid>/ and renamed
                                   atomically, so an interrupted run never leaves a
                                   partial final/
  logs/train_log.jsonl             every Trainer log line
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from agentpal.schemas import ModelAnalysis

# Verified from the Hugging Face API on 2026-10-29: Qwen/Qwen3-1.7B main = this commit
# (lastModified 2025-07-26), LICENSE = Apache-2.0, Qwen3ForCausalLM, 28 layers, hidden
# 2048, GQA 16/8 heads, vocab 151936, context 32,768 (max_position_embeddings 40960).
QWEN3_1_7B_REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
EMPTY_THINK = "<think>\n\n</think>\n\n"
MIN_GPU_BYTES = 15_000_000_000          # decimal 15 GB: T4 (15.8e9) and P100 (17.1e9) pass
SMOKE_NOTICE = ("PIPELINE SMOKE TEST: tiny random test model, few steps. NOT a trained "
                "AgentPal model; its outputs and metrics mean nothing.")


@dataclass(frozen=True)
class TrainConfig:
    base_model: str = "Qwen/Qwen3-1.7B"         # Apache-2.0 (LICENSE file verified)
    base_revision: str = QWEN3_1_7B_REVISION
    # measured with the Qwen3 tokenizer on the sft-2 build: max 950, p99 860 tokens
    # (prompt + completion); 1536 leaves room for longer agent text
    max_seq_len: int = 1536
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = ("q_proj", "k_proj", "v_proj", "o_proj",
                                       "gate_proj", "up_proj", "down_proj")
    learning_rate: float = 2e-4
    epochs: int = 2
    # sized for one 16 GB GPU in fp16 (Kaggle T4/P100), effective batch 16. ESTIMATE,
    # not measured: weights 4.1 GB + logits (2 x 1536 x 151936 x ~6 B) 2.8 GB +
    # checkpointed activations ~0.5 GB + LoRA/Adam 0.3 GB + CUDA context ~0.6 GB = ~8.3 GB
    per_device_batch: int = 2
    grad_accum: int = 8
    gradient_checkpointing: bool = True
    seed: int = 7319
    eval_and_save_steps: int = 20               # ~260 optimizer steps in total
    save_every_minutes: float = 25.0            # also save on wall-clock time (Kaggle)
    save_total_limit: int = 4
    precision: str = "auto"                     # auto | bf16 | fp16 | fp32
    # loss only on the assistant JSON (the request text is untrusted input, not a target)
    train_on_completion_only: bool = True
    # the model must learn to read the request, not copy the rules (served the same way)
    include_rule_findings: bool = False
    enable_thinking: bool = False
    run_kind: str = "training"                  # "training" | "pipeline-smoke-test"
    notes: list[str] = field(default_factory=lambda: [
        "Thinking mode disabled at training and serving (enable_thinking=False).",
        "Checkpoint choice on dev loss only; never on any locked data.",
        "Labels are synthetic/author labels: format and behaviour training only."])


# ------------------------------------------------------------------ data checks (no ML)

def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def validate(data_dir: Path, cfg: TrainConfig, tokenizer=None) -> dict:
    """Checks that hold before any GPU time is spent. With a tokenizer, token counts are
    exact (the full rendered prompt + completion); otherwise estimated from characters."""
    manifest = json.loads((data_dir / "manifest.json").read_text())
    if manifest.get("retired_test_split_read") is not False:
        raise ValueError("dataset manifest does not certify that the retired test split "
                         "was not read")
    if manifest.get("include_rule_findings") != cfg.include_rule_findings:
        raise ValueError("dataset and training config disagree on include_rule_findings")
    bo = manifest.get("benchmark_overlap")
    if isinstance(bo, dict) and bo.get("passed") is False:
        raise ValueError("dataset manifest reports overlap with the benchmarks")
    stats = {}
    for split in ("train", "dev"):
        rows = load_records(data_dir / f"{split}.jsonl")
        if not rows:
            raise ValueError(f"{split} split is empty")
        lengths = []
        for r in rows:
            target = ModelAnalysis.model_validate_json(r["target"])  # schema-valid target
            if target.recommendation == "block":
                raise ValueError(f"{r['id']}: a model target may never be 'block'")
            roles = [m["role"] for m in r["messages"]]
            if roles != ["system", "user"]:
                raise ValueError(f"{r['id']}: unexpected roles {roles}")
            if "deterministic_rule_findings" in r["messages"][1]["content"] and \
                    not cfg.include_rule_findings:
                raise ValueError(f"{r['id']}: rule findings present in the input")
            if tokenizer is not None:
                ex = to_prompt_completion(r, tokenizer, cfg)
                lengths.append(len(tokenizer(ex["prompt"] + ex["completion"],
                                             add_special_tokens=False)["input_ids"]))
            else:
                chars = sum(len(m["content"]) for m in r["messages"]) + len(r["target"])
                lengths.append(chars / 3.0)  # conservative chars-per-token for this text
        lengths.sort()
        stats[split] = {"rows": len(rows), "tokens": "exact" if tokenizer else "estimate",
                        "max_tokens": round(lengths[-1]),
                        "p99_tokens": round(lengths[int(0.99 * (len(lengths) - 1))]),
                        "mean_tokens": round(sum(lengths) / len(rows)),
                        "total_tokens": round(sum(lengths))}
        if lengths[-1] > cfg.max_seq_len:
            raise ValueError(f"{split}: an example ({round(lengths[-1])} tokens) exceeds "
                             f"max_seq_len={cfg.max_seq_len}; it would be truncated")
    eff = cfg.per_device_batch * cfg.grad_accum
    steps = math.ceil(stats["train"]["rows"] / eff) * cfg.epochs
    stats["estimate"] = {
        "trained_tokens": stats["train"]["total_tokens"] * cfg.epochs,
        "optimizer_steps": steps,
        "gpu": "one 16 GB GPU in fp16 (T4/P100) or bf16 (Ampere+); peak ~8-9 GB with "
               "per_device_batch=2 and gradient checkpointing (ESTIMATE, not measured)",
        "gpu_time": "roughly 1-2 h on a T4 (ESTIMATE, not measured)",
        "disk": "~4.1 GB base download (HF cache) + ~0.3 GB per checkpoint x "
                f"{cfg.save_total_limit} + ~0.1 GB final adapter; export adds ~4 GB merged "
                "fp16 + ~3.4 GB f16 GGUF + ~1.1 GB Q4_K_M",
    }
    return stats


def to_prompt_completion(record: dict, tokenizer, cfg: TrainConfig) -> dict:
    """Render exactly what the served model sees, then the target it must produce."""
    prompt = tokenizer.apply_chat_template(record["messages"], tokenize=False,
                                           add_generation_prompt=True,
                                           enable_thinking=cfg.enable_thinking)
    if not cfg.enable_thinking and not prompt.endswith(EMPTY_THINK):
        raise ValueError("the chat template did not render the empty think block for "
                         "enable_thinking=False; serving and training would disagree")
    eos = tokenizer.eos_token
    if eos != "<|im_end|>":
        raise ValueError(f"unexpected eos token {eos!r} (Qwen chat models end turns with "
                         "<|im_end|>)")
    return {"prompt": prompt, "completion": record["target"] + eos}


# ------------------------------------------------------------------ hardware / precision

def select_precision(torch, requested: str = "auto") -> str:
    """bf16 only on GPUs with NATIVE bf16 (compute capability >= 8.0, Ampere+). Turing
    (T4, 7.5) and Pascal (P100, 6.0) get fp16; CPU gets fp32. `torch.cuda.
    is_bf16_supported()` alone is not enough: recent PyTorch reports True on Turing by
    counting emulation, which is slow and not what we want."""
    cuda = torch.cuda.is_available()
    native_bf16 = False
    if cuda:
        major, _minor = torch.cuda.get_device_capability(0)
        try:
            supported = torch.cuda.is_bf16_supported(including_emulation=False)
        except TypeError:  # older torch without the keyword
            supported = torch.cuda.is_bf16_supported()
        native_bf16 = bool(supported) and major >= 8
    if requested == "auto":
        return "bf16" if native_bf16 else ("fp16" if cuda else "fp32")
    if requested == "bf16" and not native_bf16:
        raise ValueError("bf16 requested but this device has no native bf16 support")
    if requested in ("bf16", "fp16") and not cuda:
        raise ValueError(f"{requested} requested but no CUDA device is available")
    if requested not in ("bf16", "fp16", "fp32"):
        raise ValueError(f"unknown precision {requested!r}")
    return requested


def check_gpu(torch, allow_cpu: bool = False) -> dict:
    """Refuse a GPU that is too small for this config (no QLoRA fallback is implemented:
    bitsandbytes 4-bit is not used, so the bar is fixed)."""
    if not torch.cuda.is_available():
        if allow_cpu:
            return {"device": "cpu", "note": "CPU allowed only for the pipeline smoke test"}
        raise SystemExit("refusing to train: no CUDA GPU found. Real training needs one "
                         f"GPU with >= {MIN_GPU_BYTES / 1e9:.0f} GB VRAM (Kaggle T4/P100). "
                         "Use --allow-cpu only for the tiny-model pipeline smoke test.")
    props = torch.cuda.get_device_properties(0)
    if props.total_memory < MIN_GPU_BYTES:
        raise SystemExit(f"refusing to train: {props.name} has "
                         f"{props.total_memory / 1e9:.1f} GB VRAM; this config needs >= "
                         f"{MIN_GPU_BYTES / 1e9:.0f} GB (4-bit QLoRA is not implemented)")
    return {"device": "cuda", "name": props.name, "vram_bytes": props.total_memory,
            "capability": list(torch.cuda.get_device_capability(0))}


# ------------------------------------------------------------------ checkpoints / final

def checkpoint_is_complete(path: Path) -> bool:
    """Trainer writes trainer_state.json LAST (transformers 5.x _save_checkpoint)."""
    return (path / "trainer_state.json").is_file() and any(
        (path / n).is_file() for n in ("adapter_model.safetensors", "adapter_model.bin"))


def latest_complete_checkpoint(ckpt_dir: Path) -> Path | None:
    if not ckpt_dir.is_dir():
        return None
    found = []
    for p in ckpt_dir.glob("checkpoint-*"):
        try:
            step = int(p.name.rsplit("-", 1)[1])
        except ValueError:
            continue
        if checkpoint_is_complete(p):
            found.append((step, p))
    return max(found)[1] if found else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha256_file(p)
            for p in sorted(root.rglob("*")) if p.is_file() and p.name != "COMPLETE.json"}


def publish_final(tmp: Path, final: Path, marker: dict) -> Path:
    """Write COMPLETE.json (hashes of every file) into `tmp`, fsync, then atomically move
    it to `final`. An existing final/ is kept as final.prev-<timestamp>/."""
    marker = {**marker, "files_sha256": hash_tree(tmp),
              "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (tmp / "COMPLETE.json").write_text(json.dumps(marker, indent=2) + "\n")
    for p in tmp.rglob("*"):
        if p.is_file():
            with p.open("rb") as fh:
                os.fsync(fh.fileno())
    if final.exists():
        final.rename(final.with_name(f"{final.name}.prev-{int(time.time())}"))
    os.replace(tmp, final)
    return final


def verify_final(final: Path) -> dict:
    """Re-hash a published final/ against its COMPLETE.json; raise if anything differs."""
    marker_path = final / "COMPLETE.json"
    if not marker_path.is_file():
        raise ValueError(f"{final} has no COMPLETE.json: not a finished training run")
    marker = json.loads(marker_path.read_text())
    actual = hash_tree(final)
    if actual != marker["files_sha256"]:
        diff = sorted(set(actual.items()) ^ set(marker["files_sha256"].items()))
        raise ValueError(f"{final}: files differ from COMPLETE.json: {diff[:5]}")
    return marker


def default_output_dir() -> Path:
    kaggle = Path("/kaggle/working")
    return kaggle / "agentpal-lora" if kaggle.is_dir() else Path("build/lora")


def library_versions() -> dict:
    import importlib.metadata as md
    out = {"python": platform.python_version()}
    for name in ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
                 "safetensors", "tokenizers"):
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    return out


# ------------------------------------------------------------------ training

def train(data_dir: Path, out_dir: Path, cfg: TrainConfig, resume: bool = False,
          allow_cpu: bool = False, max_steps: int | None = None,
          limit: tuple[int, int] | None = None,
          interrupt_after_step: int | None = None) -> Path:  # pragma: no cover - needs torch
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")   # one GPU; no DataParallel on 2xT4
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from transformers import (AutoModelForCausalLM, AutoTokenizer, TrainerCallback)
        from trl import SFTConfig, SFTTrainer
    except ImportError as exc:
        sys.exit(f"training dependencies missing ({exc}); pip install -r "
                 "requirements-train.txt. Nothing was trained.")
    if not cfg.base_revision or len(cfg.base_revision) != 40:
        sys.exit("TrainConfig.base_revision must be a full 40-character commit hash")
    hw = check_gpu(torch, allow_cpu=allow_cpu)
    precision = select_precision(torch, cfg.precision)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_dir, log_dir = out_dir / "checkpoints", out_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    resume_from = None
    if resume:
        resume_from = latest_complete_checkpoint(ckpt_dir)
        if resume_from is None:
            sys.exit(f"--resume: no complete checkpoint under {ckpt_dir}")
        print(f"resuming from {resume_from}", flush=True)
    elif latest_complete_checkpoint(ckpt_dir) is not None:
        sys.exit(f"{ckpt_dir} already has checkpoints: pass --resume to continue, or use a "
                 "new --output-dir (refusing to mix two runs)")

    tok = AutoTokenizer.from_pretrained(cfg.base_model, revision=cfg.base_revision)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def dataset(split: str, n: int | None) -> Dataset:
        rows = load_records(data_dir / f"{split}.jsonl")
        rows = rows[:n] if n else rows
        return Dataset.from_list([to_prompt_completion(r, tok, cfg) for r in rows])

    train_ds = dataset("train", limit[0] if limit else None)
    dev_ds = dataset("dev", limit[1] if limit else None)
    dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
    model = AutoModelForCausalLM.from_pretrained(cfg.base_model, revision=cfg.base_revision,
                                                 dtype=dtype)
    if cfg.gradient_checkpointing:
        model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(
        r=cfg.lora_r, lora_alpha=cfg.lora_alpha, lora_dropout=cfg.lora_dropout,
        target_modules=list(cfg.target_modules), task_type="CAUSAL_LM"))
    if precision == "fp16":  # AMP needs fp32 master weights for the trainable adapter
        for p in model.parameters():
            if p.requires_grad:
                p.data = p.data.float()
    log_path = log_dir / "train_log.jsonl"

    class Guard(TrainerCallback):
        """Logs to JSONL; stops on a non-finite loss (keeps the last good checkpoint);
        forces a checkpoint every `save_every_minutes` of wall-clock time."""

        def __init__(self):
            self.last_save = time.monotonic()
            self.bad_loss = False

        def on_log(self, args, state, control, logs=None, **kw):
            logs = logs or {}
            with log_path.open("a") as fh:
                fh.write(json.dumps({"step": state.global_step, **logs}) + "\n")
            loss = logs.get("loss", logs.get("eval_loss"))
            if loss is not None and not math.isfinite(loss):
                self.bad_loss = True
                control.should_training_stop = True

        def on_step_end(self, args, state, control, **kw):
            if time.monotonic() - self.last_save >= cfg.save_every_minutes * 60:
                control.should_save = True
            if interrupt_after_step is not None and state.global_step >= interrupt_after_step:
                control.should_save = True
                control.should_training_stop = True

        def on_save(self, args, state, control, **kw):
            self.last_save = time.monotonic()

    guard = Guard()
    args = SFTConfig(
        output_dir=str(ckpt_dir), num_train_epochs=cfg.epochs,
        max_steps=max_steps if max_steps else -1,
        learning_rate=cfg.learning_rate, lr_scheduler_type="cosine",
        warmup_steps=0.05,  # transformers 5.x: a float < 1 is a ratio (warmup_ratio removed)
        per_device_train_batch_size=cfg.per_device_batch,
        per_device_eval_batch_size=cfg.per_device_batch,
        gradient_accumulation_steps=cfg.grad_accum,
        gradient_checkpointing=cfg.gradient_checkpointing,
        seed=cfg.seed, data_seed=cfg.seed, max_length=cfg.max_seq_len,
        bf16=precision == "bf16", fp16=precision == "fp16", use_cpu=hw["device"] == "cpu",
        completion_only_loss=cfg.train_on_completion_only, packing=False,
        eval_strategy="steps", eval_steps=cfg.eval_and_save_steps,
        save_strategy="steps", save_steps=cfg.eval_and_save_steps,
        save_total_limit=cfg.save_total_limit, load_best_model_at_end=True,
        metric_for_best_model="eval_loss", greater_is_better=False,
        logging_steps=max(1, min(10, cfg.eval_and_save_steps)), report_to="none",
        dataloader_num_workers=0)
    trainer = SFTTrainer(model=model, args=args, train_dataset=train_ds,
                         eval_dataset=dev_ds, processing_class=tok, callbacks=[guard])
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    trainer.train(resume_from_checkpoint=str(resume_from) if resume_from else None)
    if guard.bad_loss:
        sys.exit("training stopped: non-finite loss. final/ was NOT written; the last good "
                 "checkpoint is kept. Try --precision fp32 or a lower learning rate.")
    if interrupt_after_step is not None and trainer.state.global_step >= interrupt_after_step \
            and (max_steps is None or trainer.state.global_step < max_steps):
        print(f"interrupted on purpose after step {trainer.state.global_step} "
              "(resume test); final/ NOT written", flush=True)
        return ckpt_dir
    tmp = out_dir / f"final.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    trainer.save_model(str(tmp))          # best adapter (load_best_model_at_end)
    tok.save_pretrained(str(tmp))
    (tmp / "train_config.json").write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    data_manifest = data_dir / "manifest.json"
    marker = {
        "run_kind": cfg.run_kind,
        "notice": SMOKE_NOTICE if cfg.run_kind != "training" else
        "LoRA adapter for Qwen3-1.7B. Advisory model only; evaluate before use.",
        "base_model": cfg.base_model, "base_revision": cfg.base_revision,
        "precision": precision, "hardware": hw, "libraries": library_versions(),
        "data_manifest_sha256": sha256_file(data_manifest),
        "data_files_sha256": {s: sha256_file(data_dir / f"{s}.jsonl") for s in ("train", "dev")},
        "rows": {"train": len(train_ds), "dev": len(dev_ds)},
        "global_step": trainer.state.global_step,
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "best_eval_loss": trainer.state.best_metric,
        "resumed_from": str(resume_from) if resume_from else None,
        "started_at": started, "train_seconds": round(time.monotonic() - t0, 1),
        "inference_contract": {"enable_thinking": cfg.enable_thinking,
                               "include_rule_findings": cfg.include_rule_findings,
                               "prompt_suffix": EMPTY_THINK if not cfg.enable_thinking else ""},
    }
    final = publish_final(tmp, out_dir / "final", marker)
    print(f"final adapter: {final} (COMPLETE.json written)", flush=True)
    return final


def smoke_config(base_model: str, revision: str) -> TrainConfig:
    """Tiny-model CPU smoke settings (pipeline test only)."""
    return replace(TrainConfig(), base_model=base_model, base_revision=revision,
                   per_device_batch=2, grad_accum=1, epochs=1, eval_and_save_steps=2,
                   save_total_limit=10, precision="auto", gradient_checkpointing=False,
                   run_kind="pipeline-smoke-test")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default="build/sft")
    p.add_argument("--output-dir", "--out", dest="out", default=None,
                   help="single output directory (default /kaggle/working/agentpal-lora "
                        "on Kaggle, else build/lora)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--tokenizer", default=None,
                   help="dry run: local tokenizer dir for exact token counts")
    p.add_argument("--resume", action="store_true",
                   help="resume from the latest complete checkpoint in the output dir")
    p.add_argument("--precision", default="auto", choices=["auto", "bf16", "fp16", "fp32"])
    p.add_argument("--smoke-model", default=None,
                   help="PIPELINE SMOKE TEST ONLY: tiny test model id or path (CPU ok)")
    p.add_argument("--smoke-revision", default=None)
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--limit", type=int, nargs=2, metavar=("TRAIN", "DEV"), default=None)
    p.add_argument("--interrupt-after-step", type=int, default=None,
                   help="test aid: stop after this step as if the session ended")
    a = p.parse_args(argv)
    cfg = replace(TrainConfig(), precision=a.precision)
    if a.smoke_model:
        cfg = replace(smoke_config(a.smoke_model, a.smoke_revision or "local"),
                      precision=a.precision)
    tokenizer = None
    if a.tokenizer:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(a.tokenizer)
    stats = validate(Path(a.data), cfg, tokenizer)
    print(json.dumps({"config": asdict(cfg), "validation": stats}, indent=2))
    if a.dry_run:
        return
    out = Path(a.out) if a.out else default_output_dir()
    if a.smoke_model and cfg.base_revision == "local":
        cfg = replace(cfg, base_revision="0" * 40)  # local path: revision is ignored
    train(Path(a.data), out, cfg, resume=a.resume, allow_cpu=bool(a.smoke_model),
          max_steps=a.max_steps, limit=tuple(a.limit) if a.limit else None,
          interrupt_after_step=a.interrupt_after_step)


if __name__ == "__main__":
    main()
