# Model artifact integrity (P0.7d, owner decision 7)

## What it guarantees, and what it doesn't

Before a model artifact is loaded, its SHA-256 is compared (in constant time) with a digest **pinned by the operator** in `config/model_manifest.json`.

* **A match proves only one thing:** the file is byte-identical to the file whose digest was pinned. It says nothing about whether the model is safe, accurate, unbiased, or published by who you think.
* **Where the digest comes from matters.** For the Qwen GGUF, the pinned digest was compared with the SHA-256 that Hugging Face publishes for `Qwen/Qwen2.5-1.5B-Instruct-GGUF` (LFS metadata) and matched on 2026-10-28.
* **The ML artifact is local.** Its digest is that of the Phase 1 artifact `lr-20261028-C0.3`, which was not retrained during P0.

## Behaviour

| Situation | `require_model_integrity=true` (default) | `false` (explicit opt-out) |
|---|---|---|
| Digest pinned and matches | Backend loads | Backend loads |
| Digest pinned, mismatch | Backend **unavailable** (never loaded) → next backend → rules | Same (a pinned digest is always enforced) |
| No digest pinned / manifest missing | Backend **unavailable** → rules | Loads unverified |

* Failing closed affects only that backend. The deterministic rules and hard limits always run. `/model-info` shows `available`, `unavailable_reason`, `integrity_required` and `pinned_sha256`.
* **JSON ML artifact:** the code parses exactly the bytes it hashed, so there is no time-of-check/time-of-use window.
* **GGUF (llama.cpp): controlled loading (owner decision 7).** llama.cpp loads by path, so loading the source file directly would leave a window between hashing and loading. Instead:
  1. **Read-only storage is required** (`AGENTPAL_REQUIRE_READ_ONLY_MODEL_STORAGE=true`, the default). If the model file or its directory has any write bit set, the backend is unavailable with a `read-only` reason, and the rules apply.
  2. **Hash-while-copy staging.** The verified bytes are streamed into `AGENTPAL_MODEL_STAGING_DIR` (default `<tmp>/agentpal-models-<uid>`) while being hashed.
     * The staging directory must be owned by the service user, mode 0700, and not a symlink; otherwise loading is refused.
     * Only if the streamed bytes match the pinned digest is the copy published, atomically (`os.replace`), as `<sha256>.gguf` with mode 0400.
     * llama.cpp loads **only that private copy**. A source change after verification cannot change the loaded bytes, and a mismatching stream is discarded and never loaded.
     * An existing staged copy is reused only after its own hash matches.
  3. **Warm-up.** At API start-up (`RiskService.warm_up`), each backend is verified, staged and loaded *before* requests are served. On a cold page cache, the first hash of the 1.1 GB file took 117 s in this environment (3.5 s warm). Without warm-up, the first requests would time out and be degraded.

  Limitations:
  * **These checks do not protect against a privileged (root) process.** Root ignores permission bits, and anything running as root or as the service user can still modify the source or the staging directory.
  * The read-only check inspects permission bits only. It cannot detect a process running as root (which ignores them), nor other writers with the same uid.
  * A read-only bind mount or image layer is the recommended deployment.
  * The default staging location is under the system temp directory. Production should set a dedicated path on a filesystem only the service user can write.
  * Verification of the source is cached per process by (path, size, mtime, inode).
* **Ollama:** Ollama manages its own model files, so the service can only compare the digest the server **reports** (`/api/tags`) with the pinned one. That detects accidental model changes, but not a malicious or compromised Ollama server.

## Setup and rotation

```bash
# 1. Obtain the artifact and, where possible, the publisher's digest
#    (e.g. Hugging Face file metadata).
# 2. Compute and pin it (writes config/model_manifest.json):
python3 -m agentpal pin-model llamacpp artifacts/models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
    --source "publisher sha256 <value> verified on <date>"
python3 -m agentpal pin-model ml artifacts/baseline_classifier.json --source "trained <date>"
# 3. Compare the printed digest with the publisher's value before committing.
# 4. Commit the manifest change through review (it is trusted configuration).
# 5. Restart the service; check /model-info.
```

* **Rotation:** pin the new artifact (step 2), review it, deploy the new artifact *and* the new manifest together, then remove the old artifact. A retrained ML model always needs re-pinning, because its JSON includes a training-date version.
* **Configuration:**
  * `AGENTPAL_MODEL_MANIFEST_PATH` (default `config/model_manifest.json`)
  * `AGENTPAL_REQUIRE_MODEL_INTEGRITY` (default `true`)
  * `AGENTPAL_REQUIRE_READ_ONLY_MODEL_STORAGE` (default `true`)
  * `AGENTPAL_MODEL_STAGING_DIR` (default `<tmp>/agentpal-models-<uid>`)
* **Offline evaluation (`python -m agentpal evaluate`)** evaluates whatever artifact it is given and does not enforce the manifest. Instead it records the artifact's SHA-256 in the report for provenance.
