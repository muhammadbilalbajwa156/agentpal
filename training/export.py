"""Export a finished LoRA run to the GGUF the service loads: merge -> GGUF f16 ->
Q4_K_M -> verify -> SHA-256 manifest.

    python -m training.export --adapter /kaggle/working/agentpal-lora/final \
        --out /kaggle/working/agentpal-lora/export --llama-cpp /kaggle/working/llama.cpp

Steps (each command is printed and recorded in EXPORT_MANIFEST.json):
  1. verify final/COMPLETE.json (refuses a pipeline-smoke-test adapter unless
     --allow-smoke) and merge the adapter into the pinned base revision
     (PEFT merge_and_unload, fp16 by default), saved with the tokenizer to merged/;
  2. python <llama.cpp>/convert_hf_to_gguf.py merged --outtype f16 --outfile <f16.gguf>
  3. <llama.cpp>/build/bin/llama-quantize <f16.gguf> <q4.gguf> Q4_K_M
     (or --quantizer python: llama_cpp.llama_model_quantize from llama-cpp-python, the
     same library the service uses; no llama.cpp build needed);
  4. verify the GGUF: it loads in llama-cpp-python, carries a chat template, and that
     template renders the empty think block with enable_thinking=False (the serving
     contract in agentpal/analyzers/llm.py);
  5. EXPORT_MANIFEST.json: SHA-256 and size of every produced file, the adapter's
     COMPLETE.json digest, base revision, llama.cpp commit, library versions, and the
     operator command that pins the GGUF: python -m agentpal pin-model llamacpp <q4.gguf>.

Verified in this repository ONLY as a pipeline smoke test on CPU with the tiny random
Qwen3 test model (merge, convert, python quantize, load, template check). The real 1.7B
export (GPU memory, ~4 GB merged, ~3.4 GB f16, ~1.1 GB Q4_K_M) is UNVERIFIED here.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .train_lora import EMPTY_THINK, library_versions, sha256_file, verify_final

GGUF_STEM = "agentpal-qwen3-1.7b"


def merge(adapter: Path, out: Path, dtype_name: str = "float16") -> Path:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    marker = verify_final(adapter)
    dtype = getattr(torch, dtype_name)
    base = AutoModelForCausalLM.from_pretrained(marker["base_model"],
                                                revision=marker["base_revision"], dtype=dtype)
    merged = PeftModel.from_pretrained(base, str(adapter)).merge_and_unload()
    tmp = out.with_name(out.name + f".tmp-{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    merged.save_pretrained(str(tmp), safe_serialization=True)
    AutoTokenizer.from_pretrained(str(adapter)).save_pretrained(str(tmp))
    if out.exists():
        shutil.rmtree(out)
    os.replace(tmp, out)
    return out


def run(cmd: list[str], log: list[dict]) -> None:
    print("+ " + shlex.join(cmd), flush=True)
    t = datetime.now(timezone.utc).isoformat(timespec="seconds")
    subprocess.run(cmd, check=True)
    log.append({"at": t, "cmd": shlex.join(cmd)})


def quantize_python(src: Path, dst: Path, log: list[dict]) -> None:
    import llama_cpp
    params = llama_cpp.llama_model_quantize_default_params()
    params.ftype = llama_cpp.LLAMA_FTYPE_MOSTLY_Q4_K_M
    params.nthread = os.cpu_count() or 1
    rc = llama_cpp.llama_model_quantize(str(src).encode(), str(dst).encode(), params)
    log.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "cmd": f"llama_cpp.llama_model_quantize({src.name}, {dst.name}, Q4_K_M) "
                       f"[llama-cpp-python {llama_cpp.__version__}] -> {rc}"})
    if rc != 0:
        raise RuntimeError(f"llama_model_quantize failed with code {rc}")


def verify_gguf(path: Path) -> dict:
    """Load the GGUF (vocab only: no weights in memory) and check the serving contract."""
    from llama_cpp import Llama

    from agentpal.analyzers.llm import CHAT_TEMPLATE_METADATA_KEY, no_think_chat_handler
    llm = Llama(model_path=str(path), vocab_only=True, verbose=False)
    template = llm.metadata.get(CHAT_TEMPLATE_METADATA_KEY)
    if not template:
        raise ValueError(f"{path.name} has no chat template in its metadata")
    seen: dict = {}

    class Stop(Exception):
        pass

    class Probe:
        def tokenize(self, text, add_bos=True, special=False):
            seen["prompt"] = text.decode() if isinstance(text, bytes) else text
            raise Stop

    handler = no_think_chat_handler(template, eos_token="<|im_end|>", bos_token="")
    try:
        handler(llama=Probe(), messages=[{"role": "system", "content": "s"},
                                         {"role": "user", "content": "u"}])
    except Stop:
        pass
    ok = seen.get("prompt", "").endswith("<|im_start|>assistant\n" + EMPTY_THINK)
    if not ok:
        raise ValueError("the GGUF chat template does not render the empty think block "
                         "with enable_thinking=False; serving would not match training")
    return {"architecture": llm.metadata.get("general.architecture"),
            "chat_template_sha256": __import__("hashlib").sha256(template.encode()).hexdigest(),
            "no_think_prompt_ok": ok, "n_vocab": llm.n_vocab()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--adapter", required=True, help="final/ directory of a training run")
    p.add_argument("--out", required=True)
    p.add_argument("--llama-cpp", required=True, help="llama.cpp checkout (converter)")
    p.add_argument("--convert-python", default=sys.executable,
                   help="python with llama.cpp's converter requirements")
    p.add_argument("--quantizer", choices=["binary", "python"], default="python")
    p.add_argument("--merge-dtype", default="float16", choices=["float16", "bfloat16",
                                                                 "float32"])
    p.add_argument("--keep-intermediate", action="store_true",
                   help="keep merged/ and the f16 GGUF (default: delete after hashing)")
    p.add_argument("--allow-smoke", action="store_true")
    a = p.parse_args(argv)
    adapter, out, lc = Path(a.adapter), Path(a.out), Path(a.llama_cpp)
    marker = verify_final(adapter)
    if marker.get("run_kind") != "training" and not a.allow_smoke:
        print(f"refusing: {adapter} is a {marker.get('run_kind')} run", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    merged = merge(adapter, out / "merged", a.merge_dtype)
    log.append({"step": "merge", "dtype": a.merge_dtype, "out": str(merged)})
    f16, q4 = out / f"{GGUF_STEM}-f16.gguf", out / f"{GGUF_STEM}-q4_k_m.gguf"
    run([a.convert_python, str(lc / "convert_hf_to_gguf.py"), str(merged), "--outtype",
         "f16", "--outfile", str(f16)], log)
    if a.quantizer == "binary":
        run([str(lc / "build" / "bin" / "llama-quantize"), str(f16), str(q4), "Q4_K_M"], log)
    else:
        quantize_python(f16, q4, log)
    check = verify_gguf(q4)
    files = {q4.name: {"sha256": sha256_file(q4), "bytes": q4.stat().st_size}}
    if a.keep_intermediate:
        files[f16.name] = {"sha256": sha256_file(f16), "bytes": f16.stat().st_size}
    else:
        f16.unlink()
        shutil.rmtree(merged)
    try:
        lc_commit = subprocess.run(["git", "-C", str(lc), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip() or None
    except OSError:
        lc_commit = None
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_kind": marker.get("run_kind"), "notice": marker.get("notice"),
        "adapter_complete_json_sha256": sha256_file(adapter / "COMPLETE.json"),
        "base_model": marker["base_model"], "base_revision": marker["base_revision"],
        "llama_cpp_commit": lc_commit, "libraries": library_versions(),
        "files": files, "gguf_check": check, "steps": log,
        "inference_contract": marker.get("inference_contract"),
        "pin_command": f"python -m agentpal pin-model llamacpp {q4} --source "
                       f"'training.export {files[q4.name]['sha256'][:12]} from "
                       f"{marker['base_model']}@{marker['base_revision'][:12]}'",
        "serving_settings": "llamacpp analyzer with include_rule_findings=False and "
                            "disable_thinking=True (see docs/ML_AUDIT.md proposals)",
    }
    (out / "EXPORT_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"files": files, "gguf_check": check}, indent=2))
    print(f"manifest: {out / 'EXPORT_MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
