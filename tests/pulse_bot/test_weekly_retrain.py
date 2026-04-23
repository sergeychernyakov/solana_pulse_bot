# tests/pulse_bot/test_weekly_retrain.py
"""Tests for label_noise_floor + weekly_retrain snapshot mechanics."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pulse_bot.ml.label_noise_floor import compute_noise_floor
from pulse_bot.ml.weekly_retrain import snapshot_model

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_noise_floor_low_for_separable_data() -> None:
    # Label perfectly separable by first feature → low noise
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.standard_normal((200, 3)), columns=["a", "b", "c"])
    y = pd.Series((X["a"] > 0).astype(int).values)
    r = compute_noise_floor(X, y, k=5)
    assert "mean_disagreement" in r
    # With clean separation, near-identical feature vectors (k-NN) should
    # mostly share the same label.
    assert r["mean_disagreement"] < 0.20


def test_noise_floor_high_for_random_data() -> None:
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.standard_normal((200, 3)), columns=["a", "b", "c"])
    # Label independent of features
    y = pd.Series((rng.random(200) < 0.4).astype(int))
    r = compute_noise_floor(X, y, k=5)
    # Random label at 40% prevalence → expected disagreement ~ 2p(1-p) = 0.48
    assert r["mean_disagreement"] > 0.30


def test_noise_floor_handles_nan() -> None:
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.standard_normal((50, 3)), columns=["a", "b", "c"])
    X.loc[10:15, "a"] = np.nan
    y = pd.Series((rng.random(50) < 0.3).astype(int))
    r = compute_noise_floor(X, y, k=3)
    # Should not crash on NaN
    assert "mean_disagreement" in r


def test_noise_floor_skipped_on_tiny_sample() -> None:
    X = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    y = pd.Series([0, 1])
    r = compute_noise_floor(X, y, k=5)
    assert r.get("skipped")


def test_snapshot_copies_model_and_meta(tmp_path: Path) -> None:
    model = tmp_path / "entry_model.ubj"
    meta = tmp_path / "entry_model.meta.json"
    model.write_bytes(b"model-bytes")
    meta.write_text('{"features":["a"]}')
    history = tmp_path / "history"
    today = date(2026, 4, 22)
    target = snapshot_model(model, history, today)
    assert target.exists()
    assert target.name == "entry_model_2026-04-22.ubj"
    assert target.read_bytes() == b"model-bytes"
    assert target.with_suffix(".meta.json").exists()


def test_snapshot_no_meta_ok(tmp_path: Path) -> None:
    model = tmp_path / "exit_model.ubj"
    model.write_bytes(b"m")
    target = snapshot_model(model, tmp_path / "h", date(2026, 1, 1))
    assert target.exists()
    # Meta file missing — no error, just no meta snapshot
    assert not target.with_suffix(".meta.json").exists()
