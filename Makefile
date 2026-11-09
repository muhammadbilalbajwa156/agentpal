PY ?= python3
POSTMAN_MODE ?= legacy
MODEL_URL = https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf

.PHONY: install install-llm model data train pin-ml evaluate evaluate-llm evaluate-retired-test test serve demo lint data-audit sft train-dry-run bench-v2-leakage bench-v2-smoke

install:
	$(PY) -m pip install -r requirements-dev.txt

install-llm:            ## optional: build llama.cpp bindings + download Qwen2.5-1.5B-Instruct (Apache-2.0, ~1.1 GB)
	$(PY) -m pip install llama-cpp-python
	$(MAKE) model

model:
	mkdir -p artifacts/models
	curl -L --fail -o artifacts/models/qwen2.5-1.5b-instruct-q4_k_m.gguf $(MODEL_URL)

data:
	$(PY) -m agentpal data

train:
	$(PY) -m agentpal train
	@echo "Retrained artifact is NOT trusted until pinned: review it, then run 'make pin-ml'"

pin-ml:                 ## operator action: record the ML artifact digest in config/model_manifest.json
	$(PY) -m agentpal pin-model ml artifacts/baseline_classifier.json --source "local training"

evaluate:               ## validation split; the test split is RETIRED for any selection or tuning
	$(PY) -m agentpal evaluate --split validation

evaluate-llm:           ## slow on CPU (~10-15 s per example)
	$(PY) -m agentpal evaluate --split validation --llm-sample 60

evaluate-retired-test:  ## regression check only; never use for selection (report is marked retired)
	$(PY) -m agentpal evaluate --split test

test:
	$(PY) -m pytest

serve:
	$(PY) -m agentpal serve

demo:
	AGENTPAL_ANALYZER_CHAIN=ml,heuristic $(PY) -m agentpal assess examples/requests/0*.json examples/requests/1*.json

lint:
	$(PY) -m ruff check agentpal training tests

# ---- risk-model pipeline (see docs/TRAINING_HANDOFF.md); CPU-safe targets only ----
data-audit:             ## reproducible audit of the synthetic data (needs `make data`)
	$(PY) -m training.audit --report reports/data_audit.json

sft:                    ## build SFT data (fails on overlap with the benchmarks)
	$(PY) -m training.sft_data --out build/sft

train-dry-run: sft      ## validate data + config without a GPU or model download
	$(PY) -m training.train_lora --data build/sft --dry-run

bench-v2-leakage:
	$(PY) benchmarks/agentpal-bench-v2/leakage_check.py

bench-v2-smoke:         ## self-labelled DEV smoke run (not an independent benchmark)
	$(PY) benchmarks/agentpal-bench-v2/score.py --labels proposed --out-dir reports/bench_v2 --name dev_smoke

.PHONY: postman-generate postman-check postman-newman postman-sync openapi-check
postman-generate:
	$(PY) -m postman.generate

postman-check:
	$(PY) -m postman.generate --check

postman-newman:
	$(PY) -m postman.run_newman --mode $(POSTMAN_MODE)

postman-sync:          ## dry-run only; requires POSTMAN_API_KEY
	$(PY) -m postman.sync

openapi-check:
	$(PY) -m agentpal.platform.openapi --check
