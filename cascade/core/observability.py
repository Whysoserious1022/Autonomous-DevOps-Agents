"""
cascade/core/observability.py
──────────────────────────────
Prometheus telemetry metrics and telemetry helpers for Cascade.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

# Optional Prometheus Client import
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

if TYPE_CHECKING:
    from cascade.core.state import RunState, StepState

# ── Metrics Definition ────────────────────────────────────────────────────────

if PROMETHEUS_AVAILABLE:
    # Costs
    RUN_COST_GAUGE = Gauge(
        "cascade_run_cost_cents",
        "Total cost in cents accumulated for the pipeline run",
        ["flow_name", "run_id"]
    )
    
    # Token usage metrics
    TOKEN_USAGE_COUNTER = Counter(
        "cascade_tokens_total",
        "Total input/output LLM tokens processed",
        ["agent_name", "model_name", "token_type"] # token_type: prompt, completion, total
    )

    # Durations
    RUN_DURATION_HISTOGRAM = Histogram(
        "cascade_run_duration_seconds",
        "Duration of the flow run in seconds",
        ["flow_name"]
    )
    
    STEP_DURATION_HISTOGRAM = Histogram(
        "cascade_step_duration_seconds",
        "Duration of the step execution in seconds",
        ["step_name", "status"]
    )
else:
    RUN_COST_GAUGE = None
    TOKEN_USAGE_COUNTER = None
    RUN_DURATION_HISTOGRAM = None
    STEP_DURATION_HISTOGRAM = None


# ── Instrumentation Interface ──────────────────────────────────────────────────

class CascadeTelemetry:
    """Interface to record telemetry events and export to Prometheus."""

    _server_started = False

    @classmethod
    def start_exporter(cls, port: int = 9090) -> None:
        """Start local Prometheus HTTP exporter server."""
        if not PROMETHEUS_AVAILABLE:
            return
        if not cls._server_started:
            try:
                start_http_server(port)
                cls._server_started = True
            except Exception:
                pass # Silently proceed if address already in use

    @classmethod
    def record_run(cls, run_state: RunState) -> None:
        """Record final metrics for a completed workflow run."""
        if not PROMETHEUS_AVAILABLE:
            return
        flow_name = run_state.flow_name
        run_id = str(run_state.id)

        # Record accumulated cost
        if RUN_COST_GAUGE:
            RUN_COST_GAUGE.labels(flow_name=flow_name, run_id=run_id).set(run_state.total_cost_cents)

        # Record total duration
        if RUN_DURATION_HISTOGRAM and run_state.duration_seconds:
            RUN_DURATION_HISTOGRAM.labels(flow_name=flow_name).observe(run_state.duration_seconds)

    @classmethod
    def record_step(cls, step_name: str, step_state: StepState) -> None:
        """Record step-specific execution metrics."""
        if not PROMETHEUS_AVAILABLE:
            return
        
        # Record step execution duration
        if STEP_DURATION_HISTOGRAM and step_state.duration_seconds:
            STEP_DURATION_HISTOGRAM.labels(
                step_name=step_name,
                status=step_state.status.value
            ).observe(step_state.duration_seconds)

        # Record LLM tokens used during step
        if TOKEN_USAGE_COUNTER and step_state.total_tokens > 0:
            agent_name = step_state.name
            # Fallback model lookup from step tags or default settings
            model_name = step_state.tags.get("model", os.environ.get("CASCADE_LLM_MODEL", "unknown"))
            
            TOKEN_USAGE_COUNTER.labels(
                agent_name=agent_name,
                model_name=model_name,
                token_type="prompt"
            ).inc(step_state.prompt_tokens)
            
            TOKEN_USAGE_COUNTER.labels(
                agent_name=agent_name,
                model_name=model_name,
                token_type="completion"
            ).inc(step_state.completion_tokens)

            TOKEN_USAGE_COUNTER.labels(
                agent_name=agent_name,
                model_name=model_name,
                token_type="total"
            ).inc(step_state.total_tokens)
