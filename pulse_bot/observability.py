# pulse_bot/observability.py
"""Lightweight operational metrics + counters (architecture phase H,
codex review 2026-04-28).

Why: today the only signal we have for "is the bot OK?" is grep'ing
``logs/bot.log``. Codex review flagged this — operators need:

* queue depth + processing latency
* DB read latency
* holder capture lag
* percent incomplete snapshots
* ML inference failure rate
* parity mismatch rate

Full Prometheus / push-gateway integration is overkill at our scale
(one bot, one dev). This module is the smallest thing that gives us
the same observability surface:

* In-process counters + histograms (deque-based) so any caller can
  bump a counter without touching network.
* HTTP endpoint at PULSE_METRICS_PORT (default 9100) returning text
  in Prometheus exposition format. Pull from `curl localhost:9100/
  metrics` or scrape from a real Prometheus later.
* Snapshot of all metrics dumped to ``logs/metrics_*.log`` once per
  minute by ``MetricsLogger`` (cheap, parseable, persistent).

Concrete invariants we track:
  - ``pulse_tokens_scored_total`` — Counter
  - ``pulse_paper_trades_opened_total`` — Counter
  - ``pulse_paper_trades_closed_total`` — Counter (labeled by reason)
  - ``pulse_ml_override_total{action}`` — Counter
  - ``pulse_ml_inference_failures_total`` — Counter
  - ``pulse_holder_capture_lag_sec`` — Histogram
  - ``pulse_db_read_latency_sec`` — Histogram
  - ``pulse_open_paper_trades`` — Gauge
  - ``pulse_model_health{name}`` — Gauge (1=ok, 0=degenerate)

If/when we move to real Prometheus, swap this module's internals for
``prometheus_client`` and call sites stay unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Iterable

logger = logging.getLogger(__name__)


class _Counter:
    """Thread-safe counter, labels supported."""

    def __init__(self, name: str, help_text: str = "") -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], int] = defaultdict(int)
        self._lock = threading.Lock()

    def inc(self, n: int = 1, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += n

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            for labels, val in self._values.items():
                if labels:
                    label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"
                else:
                    label_str = ""
                out.append(f"{self.name}{label_str} {val}")
        return out


class _Gauge:
    """Mutable instantaneous value."""

    def __init__(self, name: str, help_text: str = "") -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = float(value)

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        with self._lock:
            for labels, val in self._values.items():
                if labels:
                    label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"
                else:
                    label_str = ""
                out.append(f"{self.name}{label_str} {val}")
        return out


class _Histogram:
    """Bounded ring-buffer of recent observations.

    Not a full Prometheus histogram (no buckets) — just rolling p50 /
    p95 / p99. Buffer size 1024 is enough for ~10 min of activity at
    1.5 obs/sec. For burst traffic we lose tail data, which is OK for
    operator dashboards.
    """

    def __init__(self, name: str, help_text: str = "", maxlen: int = 1024) -> None:
        self.name = name
        self.help_text = help_text
        self._buf: deque[float] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._buf.append(float(value))

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} summary"]
        with self._lock:
            if not self._buf:
                out.append(f"{self.name}_count 0")
                return out
            sorted_vals = sorted(self._buf)
            n = len(sorted_vals)
            p50 = sorted_vals[n // 2]
            p95 = sorted_vals[min(n - 1, int(n * 0.95))]
            p99 = sorted_vals[min(n - 1, int(n * 0.99))]
            out.append(f'{self.name}{{quantile="0.5"}} {p50}')
            out.append(f'{self.name}{{quantile="0.95"}} {p95}')
            out.append(f'{self.name}{{quantile="0.99"}} {p99}')
            out.append(f"{self.name}_count {n}")
            out.append(f"{self.name}_sum {sum(sorted_vals)}")
        return out


# ───────────────────────── Public registry ─────────────────────────────


class _Metrics:
    """Singleton-ish global registry. Kept module-level so call sites
    don't need to thread the registry through every constructor."""

    def __init__(self) -> None:
        # Counters
        self.tokens_scored = _Counter(
            "pulse_tokens_scored_total",
            "Tokens that completed scoring (fast or full).",
        )
        self.paper_trades_opened = _Counter(
            "pulse_paper_trades_opened_total",
            "Real paper-trade opens (entry_buyer_number > 0 or entry_type set).",
        )
        self.paper_trades_closed = _Counter(
            "pulse_paper_trades_closed_total",
            "Paper-trade closes labeled by reason.",
        )
        self.ml_override = _Counter(
            "pulse_ml_override_total",
            "ML override decisions, labeled action.",
        )
        self.ml_inference_failures = _Counter(
            "pulse_ml_inference_failures_total",
            "ML predict_proba / decide_with_confidence raised.",
        )
        self.parity_mismatches = _Counter(
            "pulse_parity_mismatches_total",
            "Train/serve feature parity mismatches detected at runtime.",
        )
        # Gauges
        self.open_paper_trades = _Gauge(
            "pulse_open_paper_trades",
            "Open paper-trade positions right now.",
        )
        self.model_health = _Gauge(
            "pulse_model_health",
            "1 = ok, 0 = degenerate / missing. Per-model label.",
        )
        # Histograms
        self.holder_capture_lag = _Histogram(
            "pulse_holder_capture_lag_sec",
            "Helius T+N capture: actual_age - target_age.",
        )
        self.db_read_latency = _Histogram(
            "pulse_db_read_latency_sec",
            "Sync/async DB read timings.",
        )
        self.token_processing_latency = _Histogram(
            "pulse_token_processing_latency_sec",
            "End-to-end time from new-token event to final decision.",
        )

    def render(self) -> str:
        """Return Prometheus exposition-format snapshot."""
        chunks: list[str] = []
        for metric in self._all_metrics():
            chunks.extend(metric.render())
        return "\n".join(chunks) + "\n"

    def _all_metrics(self) -> Iterable:
        return (
            self.tokens_scored,
            self.paper_trades_opened,
            self.paper_trades_closed,
            self.ml_override,
            self.ml_inference_failures,
            self.parity_mismatches,
            self.open_paper_trades,
            self.model_health,
            self.holder_capture_lag,
            self.db_read_latency,
            self.token_processing_latency,
        )


# Module-level singleton.
metrics = _Metrics()


# ───────────────────────── HTTP endpoint ───────────────────────────────


def start_http_server(port: int) -> None:
    """Start a tiny HTTP server in a daemon thread that serves
    ``/metrics``. Ignored if port is 0 or already bound. Failures are
    logged WARNING but never raised — observability must never crash
    the bot."""
    if port <= 0:
        return
    import http.server

    handler_metrics = metrics  # closure capture

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = handler_metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # silence default access log
            pass

    try:
        # Internal /metrics endpoint — host firewall restricts external access.
        srv = http.server.HTTPServer(("0.0.0.0", port), _Handler)  # nosec B104
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logger.info("Observability HTTP server listening on :%d/metrics", port)
    except Exception as exc:
        logger.warning(
            "Observability HTTP server failed to start on :%d: %s", port, exc
        )


# ───────────────────────── Convenience helpers ─────────────────────────


class Timer:
    """Context manager that observes elapsed seconds into a histogram.

    Use::
        with Timer(metrics.db_read_latency):
            rows = db._sync_query(...)
    """

    def __init__(self, hist: _Histogram) -> None:
        self._hist = hist
        self._t0: float = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.time()
        return self

    def __exit__(self, *args) -> None:
        self._hist.observe(time.time() - self._t0)
