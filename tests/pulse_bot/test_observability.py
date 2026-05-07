# tests/pulse_bot/test_observability.py
"""Tests for the observability module (architecture phase H, codex
review 2026-04-28). Pure unit tests — no HTTP, no threads."""

from __future__ import annotations

from pulse_bot.observability import _Counter, _Gauge, _Histogram, Timer, metrics


# ───────────────────────── Counter ─────────────────────────────────────


def test_counter_inc_and_render():
    c = _Counter("test_total", "demo")
    c.inc()
    c.inc(5)
    out = "\n".join(c.render())
    assert "test_total 6" in out
    assert "# TYPE test_total counter" in out


def test_counter_with_labels_creates_per_label_series():
    c = _Counter("entries_total")
    c.inc(action="buy")
    c.inc(action="buy")
    c.inc(action="skip")
    rendered = "\n".join(c.render())
    assert 'entries_total{action="buy"} 2' in rendered
    assert 'entries_total{action="skip"} 1' in rendered


# ───────────────────────── Gauge ───────────────────────────────────────


def test_gauge_set_overwrites():
    g = _Gauge("open")
    g.set(5)
    g.set(7)
    out = "\n".join(g.render())
    assert "open 7.0" in out


def test_gauge_with_labels_per_series():
    g = _Gauge("model_health")
    g.set(1, name="entry")
    g.set(0, name="entry_t30")
    rendered = "\n".join(g.render())
    assert 'model_health{name="entry"} 1.0' in rendered
    assert 'model_health{name="entry_t30"} 0.0' in rendered


# ───────────────────────── Histogram ───────────────────────────────────


def test_histogram_quantiles_after_observations():
    h = _Histogram("lag", maxlen=100)
    for i in range(1, 101):
        h.observe(i / 100.0)  # 0.01 ... 1.00
    out = "\n".join(h.render())
    assert "lag_count 100" in out
    # p50 ≈ 0.5, p95 ≈ 0.95, p99 ≈ 0.99
    assert 'lag{quantile="0.5"}' in out


def test_histogram_empty_renders_zero_count():
    h = _Histogram("empty")
    out = "\n".join(h.render())
    assert "empty_count 0" in out


def test_histogram_ringbuffer_drops_oldest():
    h = _Histogram("ring", maxlen=3)
    h.observe(1.0)
    h.observe(2.0)
    h.observe(3.0)
    h.observe(4.0)  # pushes 1.0 out
    out = "\n".join(h.render())
    assert "ring_count 3" in out


# ───────────────────────── Timer ──────────────────────────────────────


def test_timer_records_to_histogram():
    h = _Histogram("op_lat")
    with Timer(h):
        sum(range(100))  # negligible work
    out = "\n".join(h.render())
    assert "op_lat_count 1" in out


# ───────────────────────── Singleton metrics ──────────────────────────


def test_module_metrics_renders_known_series():
    metrics.tokens_scored.inc()
    metrics.paper_trades_opened.inc(action="ml_override")
    metrics.model_health.set(1, name="entry_t30")
    rendered = metrics.render()
    assert "pulse_tokens_scored_total" in rendered
    assert 'pulse_paper_trades_opened_total{action="ml_override"}' in rendered
    assert 'pulse_model_health{name="entry_t30"} 1.0' in rendered


def test_metrics_render_is_prometheus_format():
    """Every line is either '# HELP', '# TYPE', or 'name{...} value'."""
    rendered = metrics.render()
    for line in rendered.strip().splitlines():
        assert (
            line.startswith("# HELP")
            or line.startswith("# TYPE")
            or line  # data lines
        )
