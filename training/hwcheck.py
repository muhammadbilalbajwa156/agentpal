"""Hardware check before training: GPU name/VRAM/compute capability, CUDA and torch
versions, the precision the run would use, free disk and RAM. Exits 1 with a clear
message when the machine cannot run the configured training.

    python -m training.hwcheck --output-dir /kaggle/working/agentpal-lora
    python -m training.hwcheck --allow-cpu      # tiny-model pipeline smoke test only

Thresholds (fixed for TrainConfig; no 4-bit QLoRA path exists, so no lower VRAM bar):
  * GPU VRAM >= 15 GB (decimal; Kaggle T4 reports ~15.8e9 bytes, P100 ~17.1e9);
  * free disk at the output dir >= 12 GB for training (base model cache ~4.1 GB if it
    lives there, checkpoints, final adapter), >= 20 GB if export runs there too;
  * free RAM >= 8 GB (loading a 2B model, tokenizing ~2.4k rows).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .train_lora import MIN_GPU_BYTES, TrainConfig, select_precision

MIN_DISK_TRAIN = 12_000_000_000
MIN_DISK_EXPORT = 20_000_000_000
MIN_RAM = 8_000_000_000


def _free_ram() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def check(output_dir: Path, torch=None, allow_cpu: bool = False, export: bool = False,
          cfg: TrainConfig | None = None, disk_free: int | None = None,
          ram_free: int | None = None) -> dict:
    """Return a report; `report["ok"]` is False with `problems` when training must not
    start. `torch`, `disk_free` and `ram_free` are injectable for tests."""
    cfg = cfg or TrainConfig()
    problems: list[str] = []
    report: dict = {"output_dir": str(output_dir)}
    if torch is None:
        try:
            import torch  # noqa: PLC0415
        except ImportError:
            return {**report, "ok": False,
                    "problems": ["PyTorch is not installed (pip install -r "
                                 "requirements-train.txt)"]}
    report["torch"] = getattr(torch, "__version__", "?")
    report["cuda_runtime"] = getattr(getattr(torch, "version", None), "cuda", None)
    if torch.cuda.is_available():
        n = torch.cuda.device_count()
        props = torch.cuda.get_device_properties(0)
        report["gpus"] = [{"index": i, "name": torch.cuda.get_device_properties(i).name,
                           "vram_bytes": torch.cuda.get_device_properties(i).total_memory,
                           "capability": list(torch.cuda.get_device_capability(i))}
                          for i in range(n)]
        report["training_uses_gpu"] = 0
        if n > 1:
            report["note"] = ("more than one GPU visible: training uses GPU 0 only "
                              "(CUDA_VISIBLE_DEVICES=0), no DataParallel")
        if props.total_memory < MIN_GPU_BYTES:
            problems.append(f"GPU 0 ({props.name}) has {props.total_memory / 1e9:.1f} GB "
                            f"VRAM; this config needs >= {MIN_GPU_BYTES / 1e9:.0f} GB "
                            "(4-bit QLoRA is not implemented)")
    elif not allow_cpu:
        problems.append("no CUDA GPU: real training needs a GPU with >= "
                        f"{MIN_GPU_BYTES / 1e9:.0f} GB VRAM (--allow-cpu is only for the "
                        "tiny-model pipeline smoke test)")
    try:
        report["precision"] = select_precision(torch, cfg.precision)
    except ValueError as exc:
        problems.append(str(exc))
    probe = output_dir if output_dir.exists() else next(
        (p for p in output_dir.parents if p.exists()), Path("/"))
    free = shutil.disk_usage(probe).free if disk_free is None else disk_free
    need = MIN_DISK_EXPORT if export else MIN_DISK_TRAIN
    report["disk_free_bytes"] = free
    if free < need:
        problems.append(f"only {free / 1e9:.1f} GB free at {probe}; need >= "
                        f"{need / 1e9:.0f} GB ({'export' if export else 'training'})")
    ram = _free_ram() if ram_free is None else ram_free
    report["ram_available_bytes"] = ram
    if ram is not None and ram < MIN_RAM:
        problems.append(f"only {ram / 1e9:.1f} GB RAM available; need >= "
                        f"{MIN_RAM / 1e9:.0f} GB")
    report["hf_home"] = os.environ.get("HF_HOME")
    report["problems"] = problems
    report["ok"] = not problems
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output-dir", default="/kaggle/working/agentpal-lora"
                   if Path("/kaggle/working").is_dir() else "build/lora")
    p.add_argument("--allow-cpu", action="store_true")
    p.add_argument("--export", action="store_true",
                   help="also require the disk space for merge + GGUF export")
    a = p.parse_args(argv)
    r = check(Path(a.output_dir), allow_cpu=a.allow_cpu, export=a.export)
    print(json.dumps(r, indent=2))
    if not r["ok"]:
        print("REFUSING: " + "; ".join(r["problems"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
