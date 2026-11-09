"""Modal runner (training/modal_train.py): importable without credentials, and its
expected SFT hashes are exactly what the deterministic builder produces."""

from __future__ import annotations

import json

import pytest

modal = pytest.importorskip("modal")


def test_expected_sft_constants_match_the_builder(tmp_path, monkeypatch):
    from training import sft_data
    from training.modal_train import EXPECTED_SFT

    m = sft_data.build(tmp_path / "sft")
    got = {s: (f["rows"], f["sha256"][:12]) for s, f in m["files"].items()}
    assert got == EXPECTED_SFT
    assert json.loads((tmp_path / "sft" / "manifest.json").read_text())[
        "retired_test_split_read"] is False


def test_stages_and_gpu_are_declared():
    from training import modal_train

    assert list(modal_train.STAGES) == ["check", "train", "evaluate", "export"]
    assert modal_train.GPU[0] == "L4" and set(modal_train.GPU) <= {"L4", "A10", "L40S"}
    assert modal_train.OUT.startswith("/vol/")
