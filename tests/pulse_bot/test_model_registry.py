# tests/pulse_bot/test_model_registry.py
"""Tests for ModelRegistry — the artifact + health resolver
(architecture phase E, codex review 2026-04-28).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse_bot.ml.model_registry import DEFAULT_NAMES, ModelRegistry, ModelSpec


# ───────────────────────── Path resolution ─────────────────────────────


def test_default_names_cover_all_known_models():
    """If a new model is added, the registry mapping must include it.
    This test will fail loudly when train.py or build_dataset.py
    creates a new artifact and someone forgets to register it."""
    expected = {
        "entry", "entry_t30", "entry_reg", "entry_timing",
        "exit_quantile_sl", "exit_quantile_tp", "exit_quantile_max_hold",
        "survival",
    }
    assert set(DEFAULT_NAMES.keys()) == expected


def test_path_for_returns_data_ml_path(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    p = reg.path_for("entry")
    assert p == tmp_path / "entry_model.ubj"
    m = reg.meta_path_for("entry")
    assert m == tmp_path / "entry_model.meta.json"


def test_path_for_unknown_model_raises(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    with pytest.raises(KeyError):
        reg.path_for("not_a_real_model")


# ───────────────────────── Spec resolution ─────────────────────────────


def test_spec_for_missing_artifact_marks_not_exists(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    spec = reg.get("entry")
    assert spec.exists is False
    assert spec.healthy is False  # missing → unhealthy
    assert spec.status == "missing"
    assert "MISSING" in spec.summary()


def test_spec_reads_meta_fields(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    # Create both files so exists=True
    (tmp_path / "entry_model.ubj").write_bytes(b"fake_xgb_dump")
    (tmp_path / "entry_model.meta.json").write_text(json.dumps({
        "schema_version": "entry_v20_test",
        "auc": 0.91,
        "precision_top10": 0.097,
        "model_health": {"status": "ok"},
    }))
    spec = reg.get("entry")
    assert spec.exists
    assert spec.schema_version == "entry_v20_test"
    assert spec.auc == pytest.approx(0.91)
    assert spec.p_at_top10 == pytest.approx(0.097)
    assert spec.healthy is True
    assert spec.status == "ok"


def test_spec_with_degenerate_health_is_unhealthy(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    (tmp_path / "entry_model.ubj").write_bytes(b"x")
    (tmp_path / "entry_model.meta.json").write_text(json.dumps({
        "schema_version": "v20",
        "auc": 0.83,
        "model_health": {
            "status": "degenerate",
            "notes": ["floor>=ceiling overlap"],
        },
    }))
    spec = reg.get("entry")
    assert spec.exists is True
    assert spec.healthy is False
    assert spec.status == "degenerate"


def test_spec_legacy_meta_without_health_assumed_ok(tmp_path):
    """Pre-2026-04-28 meta.json files have no model_health field. They
    must be treated as healthy (gate didn't exist back then)."""
    reg = ModelRegistry(data_dir=tmp_path)
    (tmp_path / "entry_model.ubj").write_bytes(b"x")
    (tmp_path / "entry_model.meta.json").write_text(json.dumps({
        "schema_version": "v17",
        "auc": 0.92,
    }))
    spec = reg.get("entry")
    assert spec.healthy is True
    assert spec.status == "ok"


def test_quantile_spec_reads_spearman(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    (tmp_path / "exit_quantile_sl.ubj").write_bytes(b"x")
    (tmp_path / "exit_quantile_sl.meta.json").write_text(json.dumps({
        "objective": "reg:quantileerror",
        "quantile": 0.25,
        "spearman_rho": 0.099,
    }))
    spec = reg.get("exit_quantile_sl")
    assert spec.spearman_rho == pytest.approx(0.099)


# ───────────────────────── Listing / aggregation ───────────────────────


def test_list_all_returns_full_set(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    specs = reg.list_all()
    assert len(specs) == len(DEFAULT_NAMES)
    assert {s.name for s in specs} == set(DEFAULT_NAMES.keys())


def test_healthy_only_filters_out_missing_and_degenerate(tmp_path):
    reg = ModelRegistry(data_dir=tmp_path)
    # Only entry_t30 exists and healthy
    (tmp_path / "entry_model_t30.ubj").write_bytes(b"x")
    (tmp_path / "entry_model_t30.meta.json").write_text(json.dumps({
        "schema_version": "v20",
        "auc": 0.91,
        "model_health": {"status": "ok"},
    }))
    healthy = reg.healthy_only()
    assert [s.name for s in healthy] == ["entry_t30"]


def test_warn_if_unhealthy_logs_only_when_unhealthy(tmp_path, caplog):
    """warn_if_unhealthy must NOT spam logs when model is fine."""
    import logging
    caplog.set_level(logging.WARNING)
    reg = ModelRegistry(data_dir=tmp_path)
    (tmp_path / "entry_model.ubj").write_bytes(b"x")
    (tmp_path / "entry_model.meta.json").write_text(json.dumps({
        "schema_version": "v20",
        "model_health": {"status": "ok"},
    }))
    reg.warn_if_unhealthy("entry")
    assert all("MODEL entry status=" not in rec.getMessage() for rec in caplog.records)


def test_warn_if_unhealthy_flags_problems(tmp_path, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    reg = ModelRegistry(data_dir=tmp_path)
    (tmp_path / "entry_model.ubj").write_bytes(b"x")
    (tmp_path / "entry_model.meta.json").write_text(json.dumps({
        "schema_version": "v20",
        "model_health": {
            "status": "degenerate",
            "notes": ["overlap"],
        },
    }))
    reg.warn_if_unhealthy("entry")
    assert any("MODEL entry status=degenerate" in rec.getMessage()
               for rec in caplog.records)


def test_log_boot_summary_dumps_one_line_per_model(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    reg = ModelRegistry(data_dir=tmp_path)
    reg.log_boot_summary()
    msgs = [rec.getMessage() for rec in caplog.records]
    # Header + footer lines + one per model
    assert any("MODEL REGISTRY" in m for m in msgs)
    for name in DEFAULT_NAMES:
        assert any(name in m for m in msgs)
