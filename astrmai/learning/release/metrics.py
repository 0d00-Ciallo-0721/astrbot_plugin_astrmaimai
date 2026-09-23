from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class BaselineMetrics:
    reply_loss_rate: float
    dialog_p95_latency_sec: float
    queue_wait_p95_sec: float
    provider_timeout_rate: float
    window: str = "previous-7d-same-utc-hour"


@dataclass(frozen=True, slots=True)
class MetricWindow:
    window_id: str
    eligible_turns: int
    confirmed_dialog_reply_loss_rate: float
    dialog_p95_latency_sec: float
    learning_caused_starvation: int
    queue_wait_p95_sec: float
    provider_timeout_rate: float
    prompt_visible: int
    prompt_selected: int
    attribution_correct: int
    attribution_total: int
    unsafe_asset_count: int
    vector_mismatch_count: int
    cursor_regression_count: int
    old_revision_overwrite_count: int
    duplicate_visible_send_count: int
    repeated_injection_count: int
    known_outcomes: int
    unknown_outcomes: int
    window_start: str | None = None
    window_end: str | None = None


@dataclass(frozen=True, slots=True)
class StopEvaluation:
    status: str
    stop: bool
    reasons: tuple[str, ...]
    semantic_kpi_status: str
    denominator_sufficient: bool
    consecutive_windows: int


class StopEvaluator:
    def __init__(self, baseline: BaselineMetrics, *, minimum_prompt_denominator: int = 50):
        self.baseline = baseline
        self.minimum_prompt_denominator = minimum_prompt_denominator
        self._degraded_streak = 0
        self._baseline_error = self._validate_baseline(baseline)

    @staticmethod
    def _finite_non_negative(value: object) -> bool:
        return (
            type(value) in {int, float}
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0.0
        )

    @classmethod
    def _validate_baseline(cls, baseline: BaselineMetrics) -> str:
        for name in (
            "reply_loss_rate",
            "dialog_p95_latency_sec",
            "queue_wait_p95_sec",
            "provider_timeout_rate",
        ):
            if not cls._finite_non_negative(getattr(baseline, name)):
                return f"invalid_baseline:{name}"
        if baseline.reply_loss_rate > 1.0 or baseline.provider_timeout_rate > 1.0:
            return "invalid_baseline:rate_out_of_range"
        return ""

    @classmethod
    def _validate_window(cls, window: MetricWindow) -> str:
        for name in (
            "confirmed_dialog_reply_loss_rate",
            "dialog_p95_latency_sec",
            "queue_wait_p95_sec",
            "provider_timeout_rate",
        ):
            if not cls._finite_non_negative(getattr(window, name)):
                return f"invalid_metric:{name}"
        if (
            window.confirmed_dialog_reply_loss_rate > 1.0
            or window.provider_timeout_rate > 1.0
        ):
            return "invalid_metric:rate_out_of_range"
        counts = (
            "eligible_turns",
            "learning_caused_starvation",
            "prompt_visible",
            "prompt_selected",
            "attribution_correct",
            "attribution_total",
            "unsafe_asset_count",
            "vector_mismatch_count",
            "cursor_regression_count",
            "old_revision_overwrite_count",
            "duplicate_visible_send_count",
            "repeated_injection_count",
            "known_outcomes",
            "unknown_outcomes",
        )
        for name in counts:
            value = getattr(window, name)
            if type(value) is not int or value < 0:
                return f"invalid_metric:{name}"
        if window.prompt_visible > window.prompt_selected:
            return "invalid_metric:prompt_visible_exceeds_selected"
        if window.attribution_correct > window.attribution_total:
            return "invalid_metric:attribution_correct_exceeds_total"
        if window.known_outcomes + window.unknown_outcomes > window.eligible_turns:
            return "invalid_metric:outcomes_exceed_eligible_turns"
        return ""

    def evaluate(self, window: MetricWindow) -> StopEvaluation:
        invalid = self._baseline_error or self._validate_window(window)
        if invalid:
            self._degraded_streak = 0
            return StopEvaluation(
                "blocked",
                True,
                (invalid,),
                "unavailable",
                False,
                0,
            )
        reasons: list[str] = []
        immediate = {
            "unsafe_asset_active": window.unsafe_asset_count > 0,
            "vector_identity_generation_mismatch": window.vector_mismatch_count > 0,
            "cursor_regression": window.cursor_regression_count > 0,
            "old_revision_overwrite": window.old_revision_overwrite_count > 0,
            "duplicate_visible_send": window.duplicate_visible_send_count > 0,
            "repeated_injection": window.repeated_injection_count > 0,
            "learning_caused_starvation": window.learning_caused_starvation > 0,
            "confirmed_dialog_reply_loss": window.confirmed_dialog_reply_loss_rate > self.baseline.reply_loss_rate + 0.005,
        }
        reasons.extend(name for name, value in immediate.items() if value)
        denominator_sufficient = window.prompt_selected >= self.minimum_prompt_denominator
        if not denominator_sufficient:
            reasons.append("prompt_visibility_denominator_insufficient")
        degraded = False
        if denominator_sufficient and window.prompt_visible / window.prompt_selected < 0.95:
            degraded = True
            reasons.append("prompt_visibility_below_threshold")
        if window.attribution_total and window.attribution_correct / window.attribution_total < 0.90:
            degraded = True
            reasons.append("attribution_below_threshold")
        if window.dialog_p95_latency_sec >= max(self.baseline.dialog_p95_latency_sec * 1.20, self.baseline.dialog_p95_latency_sec + 2.0):
            degraded = True
            reasons.append("dialog_p95_latency_threshold")
        if window.queue_wait_p95_sec >= max(self.baseline.queue_wait_p95_sec * 1.20, self.baseline.queue_wait_p95_sec + 5.0):
            degraded = True
            reasons.append("queue_wait_p95_threshold")
        if window.provider_timeout_rate > self.baseline.provider_timeout_rate + 0.05:
            degraded = True
            reasons.append("provider_timeout_threshold")
        self._degraded_streak = self._degraded_streak + 1 if degraded else 0
        consecutive = self._degraded_streak
        if immediate.keys() and any(immediate.values()):
            return StopEvaluation("stopped", True, tuple(dict.fromkeys(reasons)), "unavailable", denominator_sufficient, consecutive)
        unknown_total = window.known_outcomes + window.unknown_outcomes
        if unknown_total and window.unknown_outcomes / unknown_total > 0.30:
            self._degraded_streak = 0
            return StopEvaluation("partial", False, tuple(dict.fromkeys(reasons + ["unknown_outcome_above_threshold"])), "unavailable", denominator_sufficient, 0)
        if consecutive >= 3:
            return StopEvaluation("stopped", True, tuple(dict.fromkeys(reasons)), "unavailable", denominator_sufficient, consecutive)
        if not denominator_sufficient:
            return StopEvaluation("insufficient_observation", False, tuple(dict.fromkeys(reasons)), "unavailable", False, consecutive)
        return StopEvaluation("ok", False, tuple(dict.fromkeys(reasons)), "unavailable", True, consecutive)
