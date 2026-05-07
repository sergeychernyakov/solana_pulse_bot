# tests/pulse_bot/test_shadow.py
"""Shadow logging — env-gated, exception-tolerant."""

from __future__ import annotations

from unittest.mock import patch

from pulse_bot.ml import shadow


def test_t30_disabled_when_env_unset(monkeypatch) -> None:
    """No env var → record_t30_shadow is a noop and never inserts."""
    # Force re-eval via reload-style monkeypatch on internal flag.
    monkeypatch.setattr(shadow, "_T30_SHADOW_ENABLED", False)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_t30_shadow(
            mint="M",
            scored_at=1700.0,
            proba=0.7,
            action="BUY",
            model_hash="abc",
        )
        mock_ins.assert_not_called()


def test_t30_inserts_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_T30_SHADOW_ENABLED", True)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_t30_shadow(
            mint="M",
            scored_at=1700.0,
            proba=0.95,
            action="BUY",
            model_hash="abc",
        )
        mock_ins.assert_called_once()
        kwargs = mock_ins.call_args.kwargs
        assert kwargs["model_name"] == "entry_model_t30"
        assert kwargs["snapshot_t"] == 30.0
        assert kwargs["prediction"] == {"proba": 0.95, "action": "BUY"}
        # confidence = abs(proba - 0.5) * 2  → 0.9
        assert abs(kwargs["confidence"] - 0.9) < 1e-9


def test_t30_confidence_at_indecisive() -> None:
    """proba=0.5 → confidence=0 (perfectly indecisive)."""
    with patch.object(shadow, "_T30_SHADOW_ENABLED", True), patch.object(
        shadow, "_insert"
    ) as mock_ins:
        shadow.record_t30_shadow(
            mint="M",
            scored_at=1700.0,
            proba=0.5,
            action="DEFER",
            model_hash="abc",
        )
        assert mock_ins.call_args.kwargs["confidence"] == 0.0


def test_quantile_disabled_when_env_unset(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_QUANTILE_SHADOW_ENABLED", False)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_quantile_shadow(
            mint="M",
            scored_at=1700.0,
            snapshot_t=15.0,
            q25_pred=-12.0,
            q75_pred=20.0,
            actual_pnl_pct=2.5,
        )
        mock_ins.assert_not_called()


def test_quantile_records_pair_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_QUANTILE_SHADOW_ENABLED", True)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_quantile_shadow(
            mint="M",
            scored_at=1700.0,
            snapshot_t=15.0,
            q25_pred=-12.0,
            q75_pred=20.0,
            actual_pnl_pct=2.5,
            model_hash_sl="sl1",
            model_hash_tp="tp1",
        )
        kw = mock_ins.call_args.kwargs
        assert kw["model_name"] == "exit_quantile"
        # spread = q75 - q25 = 32, used as confidence proxy
        assert kw["confidence"] == 32.0
        assert kw["prediction"]["q25_pred"] == -12.0
        assert kw["prediction"]["q75_pred"] == 20.0
        assert kw["prediction"]["actual_pnl_pct"] == 2.5
        assert kw["model_hash"] == "sl=sl1|tp=tp1"


def test_timing_disabled_when_env_unset(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_TIMING_SHADOW_ENABLED", False)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_timing_shadow(
            mint="M",
            scored_at=1700.0,
            snapshot_t=15.0,
            action="BUY",
            proba=0.85,
        )
        mock_ins.assert_not_called()


def test_timing_records_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_TIMING_SHADOW_ENABLED", True)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_timing_shadow(
            mint="M",
            scored_at=1700.0,
            snapshot_t=15.0,
            action="BUY",
            proba=0.85,
        )
        kw = mock_ins.call_args.kwargs
        assert kw["model_name"] == "entry_timing_model"
        assert kw["snapshot_t"] == 15.0
        assert kw["prediction"] == {"action": "BUY", "proba": 0.85}
        assert kw["confidence"] == 0.85


def test_survival_disabled_when_env_unset(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_SURVIVAL_SHADOW_ENABLED", False)
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_survival_shadow(
            mint="M",
            scored_at=1700.0,
            snapshot_t=30.0,
            remaining_life_seconds=120.0,
            confidence=0.7,
        )
        mock_ins.assert_not_called()


def test_survival_records_with_hazard_curve_truncated(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "_SURVIVAL_SHADOW_ENABLED", True)
    long_curve = [0.01] * 100
    with patch.object(shadow, "_insert") as mock_ins:
        shadow.record_survival_shadow(
            mint="M",
            scored_at=1700.0,
            snapshot_t=30.0,
            remaining_life_seconds=120.0,
            confidence=0.7,
            hazard_curve=long_curve,
        )
        kw = mock_ins.call_args.kwargs
        assert kw["model_name"] == "survival_model"
        assert kw["prediction"]["remaining_life_seconds"] == 120.0
        # truncated to 64 buckets to keep JSONB rows compact
        assert len(kw["prediction"]["hazard_curve"]) == 64
        assert kw["confidence"] == 0.7


def test_insert_swallows_exceptions() -> None:
    """A failing DB connection MUST NOT raise — shadow is best-effort."""
    with patch("pulse_bot.ml.shadow.psycopg2.connect", side_effect=Exception("boom")):
        # Should silently return, no exception propagates.
        shadow._insert(
            mint="M",
            model_name="entry_model_t30",
            scored_at=1700.0,
            snapshot_t=30.0,
            prediction={"x": 1},
            confidence=0.5,
            model_hash="abc",
            schema_version="v1",
        )
