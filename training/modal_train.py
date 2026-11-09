"""Run the AgentPal LoRA training pipeline on a Modal GPU (alternative to Kaggle).

Same pipeline and safety checks as docs/TRAINING_HANDOFF.md, executed in a Modal container:
  * the code is the exact-commit source bundle (training.kaggle_bundle), re-verified by
    hash inside the container;
  * the SFT data must match the expected row counts and SHA-256 prefixes, else stop;
  * outputs, checkpoints and reports live on the Modal Volume `agentpal-train-out`
    (`/vol/agentpal-lora`), the Hugging Face cache on `agentpal-hf-cache`, so a crashed or
    preempted run resumes from the latest checkpoint (--resume is chosen automatically).

Requires MODAL_TOKEN_ID / MODAL_TOKEN_SECRET in the environment (never in files).

    python -m training.modal_train check              # GPU hwcheck + data build + dry run
    python -m training.modal_train train              # train (auto-resume)
    python -m training.modal_train evaluate           # fine-tuned + zero-shot evaluation
    python -m training.modal_train export             # GGUF + manifest + GGUF evaluation
    python -m training.modal_train all                # check -> train -> evaluate -> export
    modal volume get agentpal-train-out agentpal-lora/export build/modal_export
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import modal

APP_NAME = "agentpal-train"
# Fallback list: Modal picks the first type with capacity. All are >= 22 GB and support
# bf16, so the training configuration is the same on each. An L4-only request sat in the
# queue for 16 min with no container on 2026-10-30; L4 ~$0.80/h, A10 ~$1.10/h,
# L40S ~$1.95/h (Modal pricing page, 2026-10-30).
GPU = ["L4", "A10", "L40S"]
PYTHON = "3.13"               # the pinned training set was verified on Python 3.13.17
LLAMA_CPP_TAG = "b11989"      # converter version verified by the pipeline smoke test
REMOTE_SRC = "/root/src"
DEFAULT_RUN = "agentpal-lora"   # 2026-10-30 run 1 (aug-1 data); new runs need --run NAME
OUT = f"/vol/{DEFAULT_RUN}"
# SFT build of the bundled code (aug-2). Run 1 used aug-1: train (2054, c4cc406955e1),
# dev (371, d8ca8fd836c6); see docs/training_runs/2026-10-30-modal.
EXPECTED_SFT = {"train": (2194, "2a94fa755fbf"), "dev": (405, "0488aed4b73c")}


def _out(run: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", run):
        raise SystemExit(f"invalid run name {run!r}")
    return f"/vol/{run}"

_HERE = Path(__file__).resolve().parent            # agentpal/training
_AGENTPAL = _HERE.parent                            # agentpal/
_BUNDLE_DIR = _AGENTPAL / "build" / "modal"


def _bundle() -> Path:
    """The exact-commit source bundle shipped to the container (built on the caller side)."""
    found = sorted(_BUNDLE_DIR.glob("agentpal-src-*.zip"))
    if not found:
        raise SystemExit(f"no bundle in {_BUNDLE_DIR}: run "
                         "`python -m training.kaggle_bundle create --out build/modal` first")
    if len(found) > 1:
        raise SystemExit(f"several bundles in {_BUNDLE_DIR}; keep exactly one")
    return found[0]


image = (
    modal.Image.debian_slim(python_version=PYTHON)
    .apt_install("git", "unzip", "build-essential", "cmake")
    .pip_install_from_requirements(str(_AGENTPAL / "requirements-train.txt"))
    .pip_install_from_requirements(str(_AGENTPAL / "requirements.txt"))
)
if modal.is_local() and any(_BUNDLE_DIR.glob("agentpal-src-*.zip")):
    # the bundle exists only on the caller side; main() refuses to run without one
    image = image.add_local_file(str(_bundle()), "/root/bundle.zip")

app = modal.App(APP_NAME, image=image)
out_vol = modal.Volume.from_name("agentpal-train-out", create_if_missing=True)
hf_vol = modal.Volume.from_name("agentpal-hf-cache", create_if_missing=True)
VOLUMES = {"/vol": out_vol, "/hf": hf_vol}
ENV = {"HF_HOME": "/hf", "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false"}


def _sh(*cmd: str, check: bool = True) -> int:
    print("+", " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=f"{REMOTE_SRC}/agentpal").returncode
    if check and rc != 0:
        raise SystemExit(f"command failed (exit {rc}): {' '.join(cmd)}")
    return rc


def _prepare() -> None:
    """Unpack + verify the bundle, build the SFT data and check its hashes."""
    import json
    import shutil

    shutil.rmtree(REMOTE_SRC, ignore_errors=True)
    Path(REMOTE_SRC).mkdir(parents=True)
    subprocess.run(["unzip", "-q", "/root/bundle.zip", "-d", REMOTE_SRC], check=True)
    _sh(sys.executable, "-m", "training.kaggle_bundle", "verify", "--root", ".")
    _sh(sys.executable, "-m", "training.sft_data", "--out", "build/sft")
    m = json.loads(Path(f"{REMOTE_SRC}/agentpal/build/sft/manifest.json").read_text())
    got = {s: (f["rows"], f["sha256"][:12]) for s, f in m["files"].items()}
    if got != EXPECTED_SFT:
        raise SystemExit(f"SFT data mismatch: {got} != expected {EXPECTED_SFT}; stopping")
    print("SFT data verified:", got, flush=True)


@app.function(gpu=GPU, volumes=VOLUMES, env=ENV, timeout=30 * 60)
def check(run: str = DEFAULT_RUN) -> None:
    OUT = _out(run)  # noqa: N806 (per-run output directory)
    _prepare()
    _sh("nvidia-smi")
    _sh(sys.executable, "-m", "training.hwcheck", "--output-dir", OUT)
    _sh(sys.executable, "-m", "training.train_lora", "--data", "build/sft", "--dry-run")
    out_vol.commit()


@app.function(gpu=GPU, volumes=VOLUMES, env=ENV, timeout=6 * 60 * 60,
              retries=modal.Retries(max_retries=2, initial_delay=30.0))
def train(run: str = DEFAULT_RUN) -> None:
    OUT = _out(run)  # noqa: N806 (per-run output directory)
    _prepare()
    _sh(sys.executable, "-m", "training.hwcheck", "--output-dir", OUT)
    cmd = [sys.executable, "-m", "training.train_lora", "--data", "build/sft",
           "--output-dir", OUT]
    if any(Path(OUT, "checkpoints").glob("checkpoint-*")):
        cmd.append("--resume")            # preempted / retried / earlier session
    try:
        _sh(*cmd)
    finally:
        out_vol.commit()                  # keep checkpoints even if the run failed
        hf_vol.commit()


@app.function(gpu=GPU, volumes=VOLUMES, env=ENV, timeout=3 * 60 * 60)
def evaluate(run: str = DEFAULT_RUN) -> None:
    OUT = _out(run)  # noqa: N806 (per-run output directory)
    _prepare()
    if not Path(OUT, "final", "COMPLETE.json").exists():
        raise SystemExit("no completed training run (final/COMPLETE.json missing)")
    try:
        _sh(sys.executable, "-m", "training.evaluate_model", "--adapter", f"{OUT}/final",
            "--data", "build/sft", "--out", f"{OUT}/reports")
        _sh(sys.executable, "-m", "training.evaluate_model", "--zero-shot",
            "--data", "build/sft", "--out", f"{OUT}/reports")
    finally:
        out_vol.commit()


@app.function(gpu=GPU, volumes=VOLUMES, env=ENV, timeout=3 * 60 * 60)
def export(run: str = DEFAULT_RUN) -> None:
    OUT = _out(run)  # noqa: N806 (per-run output directory)
    _prepare()
    llama = "/root/llama.cpp"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", LLAMA_CPP_TAG,
                    "https://github.com/ggml-org/llama.cpp", llama], check=True)
    try:
        _sh(sys.executable, "-m", "training.export", "--adapter", f"{OUT}/final",
            "--out", f"{OUT}/export", "--llama-cpp", llama)
        # The GGUF (serving-path) evaluation is NOT run here: llama-cpp-python runs it on
        # the CPU while the GPU is billed (2026-10-30 run). Download the GGUF and run
        # `python -m training.evaluate_model --gguf ...` on the serving host instead.
        _sh("sha256sum", f"{OUT}/export/agentpal-qwen3-1.7b-q4_k_m.gguf")
    finally:
        out_vol.commit()


STAGES = {"check": check, "train": train, "evaluate": evaluate, "export": export}


def main(argv: list[str] | None = None) -> None:
    args = list(argv if argv is not None else sys.argv[1:]) or ["check"]
    detach = "--detach" in args
    args = [a for a in args if a != "--detach"]
    run = DEFAULT_RUN
    if "--run" in args:  # separate output dir per run: never resume another run's checkpoints
        i = args.index("--run")
        run = args[i + 1]
        del args[i:i + 2]
    _out(run)
    stage = args[0]
    order = list(STAGES) if stage == "all" else [stage]
    if any(s not in STAGES for s in order):
        raise SystemExit(f"stage must be one of {', '.join(STAGES)} or all")
    if detach and len(order) != 1:
        raise SystemExit("--detach runs exactly one stage")
    _bundle()  # exactly one exact-commit bundle must exist before anything runs remotely
    # --detach: the stage keeps running on Modal if this client disconnects (a long
    # training run must not depend on the caller's session); progress is on the volume.
    with modal.enable_output(), app.run(detach=detach):
        for s in order:
            print(f"=== {s} on Modal {'/'.join(GPU)} (run {run}) ===", flush=True)
            if detach:
                call = STAGES[s].spawn(run)
                print(f"spawned {s}: function call {call.object_id}; monitor with "
                      f"`modal app logs` and `modal volume ls agentpal-train-out {run}`",
                      flush=True)
            else:
                STAGES[s].remote(run)


if __name__ == "__main__":
    main()
