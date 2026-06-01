"""
Time-series trend prediction for ecommerce intelligence.

Model stack (in priority order):
  1. Chronos (Amazon, MIT-licensed) — foundation model, best predictions
  2. Prophet (Meta) — robust seasonality-aware forecasting
  3. numpy linear/polynomial regression — always available, honest fallback

When real historical data is unavailable, synthetic category-benchmark
sequences are used and the output is clearly labelled as projected estimates.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Literal

import numpy as np

logger = logging.getLogger(__name__)

# ── Availability flags (resolved once at import) ───────────────────────────────

def _try_chronos():
    try:
        import torch  # noqa: F401
        from chronos import ChronosPipeline  # noqa: F401
        return True
    except ImportError:
        return False

def _try_prophet():
    try:
        from prophet import Prophet  # noqa: F401
        return True
    except ImportError:
        return False

_CHRONOS_OK = _try_chronos()
_PROPHET_OK  = _try_prophet()

logger.info(
    "TrendPredictor backends: chronos=%s  prophet=%s  numpy=✓",
    "✓" if _CHRONOS_OK else "✗",
    "✓" if _PROPHET_OK else "✗",
)

# Human-readable label for the active backend
def _active_backend_label() -> str:
    if _CHRONOS_OK:
        return "AI-Powered Forecast (Chronos)"
    if _PROPHET_OK:
        return "AI-Powered Forecast (Prophet)"
    return "Statistical Projection"

# ── Category benchmark sequences (used when real data is unavailable) ──────────
# Each value is a rough weekly review-count index for the category.
# Scaled per product when synthetic data is generated.

_CATEGORY_BENCHMARKS: dict[str, list[float]] = {
    "fashion":     [18, 22, 20, 24, 27, 25, 30, 28, 32, 35, 33, 38],
    "skincare":    [12, 14, 13, 16, 15, 18, 20, 19, 22, 24, 23, 26],
    "electronics": [30, 28, 32, 35, 33, 38, 40, 37, 42, 44, 41, 46],
    "fitness":     [10, 11, 12, 14, 13, 15, 17, 16, 18, 20, 19, 22],
    "food":        [8,  9,  8, 10, 11, 10, 12, 11, 13, 14, 13, 15],
    "ecommerce":   [15, 16, 17, 18, 17, 19, 20, 21, 22, 21, 23, 24],
}

_PRICE_BENCHMARKS: dict[str, list[float]] = {
    "fashion":     [1499, 1499, 1549, 1549, 1599, 1649, 1649, 1699, 1699, 1749, 1749, 1799],
    "skincare":    [799,  799,  799,  849,  849,  849,  899,  899,  899,  949,  949,  999],
    "electronics": [4999, 4999, 4799, 4799, 4699, 4699, 4599, 4599, 4499, 4499, 4399, 4399],
    "fitness":     [2499, 2499, 2599, 2599, 2699, 2699, 2799, 2799, 2899, 2899, 2999, 2999],
    "food":        [499,  499,  529,  529,  549,  549,  579,  579,  599,  599,  629,  629],
    "ecommerce":   [999,  999,  1049, 1049, 1099, 1099, 1149, 1149, 1199, 1199, 1249, 1249],
}


# ── Internal forecasting helpers ───────────────────────────────────────────────

def _numpy_forecast(series: list[float], horizon: int) -> list[float]:
    """Polynomial (degree-2) regression forecast — always available."""
    x = np.arange(len(series), dtype=float)
    y = np.array(series, dtype=float)
    coeffs = np.polyfit(x, y, deg=min(2, len(series) - 1))
    poly = np.poly1d(coeffs)
    future_x = np.arange(len(series), len(series) + horizon, dtype=float)
    return [max(0.0, float(poly(xi))) for xi in future_x]


def _prophet_forecast(series: list[float], horizon: int) -> list[float]:
    """Prophet forecast — handles seasonality and trend changes."""
    import pandas as pd
    from prophet import Prophet

    # Prophet needs at least 2 data points and a datetime index
    base = pd.Timestamp("2024-01-01")
    ds = [base + pd.Timedelta(weeks=i) for i in range(len(series))]
    df = pd.DataFrame({"ds": ds, "y": series})

    m = Prophet(weekly_seasonality=False, daily_seasonality=False, yearly_seasonality=False)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m.fit(df)

    future = m.make_future_dataframe(periods=horizon, freq="W")
    fc = m.predict(future)
    return [max(0.0, float(v)) for v in fc["yhat"].tail(horizon).values]


def _chronos_forecast(series: list[float], horizon: int) -> list[float]:
    """Chronos foundation model forecast."""
    import torch
    from chronos import ChronosPipeline

    pipeline = ChronosPipeline.from_pretrained(
        "amazon/chronos-t5-tiny",   # smallest model: ~10MB, good for ≤12-step ahead
        device_map="cpu",
        torch_dtype=torch.float32,
    )
    context = torch.tensor(series, dtype=torch.float32).unsqueeze(0)
    forecast = pipeline.predict(context, prediction_length=horizon, num_samples=20)
    # Median over samples
    median = forecast[0].median(dim=0).values
    return [max(0.0, float(v)) for v in median.tolist()]


def _forecast(series: list[float], horizon: int) -> tuple[list[float], str]:
    """Run the best available model. Returns (predictions, backend_name)."""
    if _CHRONOS_OK:
        try:
            return _chronos_forecast(series, horizon), "Chronos (Amazon)"
        except Exception as exc:
            logger.warning("Chronos forecast failed, falling back: %s", exc)
    if _PROPHET_OK:
        try:
            return _prophet_forecast(series, horizon), "Prophet (Meta)"
        except Exception as exc:
            logger.warning("Prophet forecast failed, falling back: %s", exc)
    return _numpy_forecast(series, horizon), "polynomial regression"


def _trend_label(series: list[float], predicted: list[float]) -> str:
    """Classify the overall trend direction."""
    if not series or not predicted:
        return "stable"
    recent_mean = np.mean(series[-4:]) if len(series) >= 4 else np.mean(series)
    future_mean = np.mean(predicted)
    pct_change  = (future_mean - recent_mean) / (abs(recent_mean) + 1e-9)
    if pct_change > 0.12:
        return "accelerating"
    if pct_change < -0.12:
        return "declining"
    return "stable"


def _build_synthetic(
    category: str,
    benchmark_map: dict[str, list[float]],
    scale: float = 1.0,
) -> tuple[list[float], bool]:
    """Return (series, is_synthetic). Series is scaled from category benchmarks."""
    base = benchmark_map.get(category) or benchmark_map["ecommerce"]
    arr  = [max(0.0, v * scale) for v in base]
    return arr, True


# ── Public API ─────────────────────────────────────────────────────────────────

class TrendPredictor:
    """
    Ecommerce trend prediction using Chronos → Prophet → numpy (in that order).

    When real historical data is unavailable, synthetic category-benchmark
    sequences are used and all outputs are labelled as projected estimates.
    """

    # ── Review velocity ────────────────────────────────────────────────────────

    def predict_review_velocity(
        self,
        review_counts: list[int] | None = None,
        horizon: int = 4,
        category: str = "ecommerce",
        current_review_count: int = 0,
    ) -> dict:
        """
        Given weekly review counts for the past 8–12 weeks, predict the next
        `horizon` weeks. Uses category benchmarks when real data is unavailable.

        Returns
        -------
        {
          predicted_counts   : list[int],
          trend              : "accelerating" | "stable" | "declining",
          confidence         : float (0-1),
          signal             : "high_demand" | "cooling" | "stable",
          weekly_sparkline   : list[float],   # historical + predicted normalised 0-100
          backend            : str,           # which model was used
          is_projected       : bool,          # True if synthetic data was used
          note               : str | None,    # "projected from category averages" when synthetic
        }
        """
        is_projected = False
        note: str | None = None

        if review_counts and len(review_counts) >= 4:
            series = [float(v) for v in review_counts]
        else:
            # Scale benchmarks so the endpoint matches current_review_count
            scale = max(current_review_count / 20.0, 0.5) if current_review_count else 1.0
            series, is_projected = _build_synthetic(category, _CATEGORY_BENCHMARKS, scale)
            note = "Projected from category averages — connect weekly monitoring for real trends."

        predicted, backend = _forecast(series, horizon)
        trend = _trend_label(series, predicted)

        # Signal
        if trend == "accelerating":
            signal = "high_demand"
        elif trend == "declining":
            signal = "cooling"
        else:
            signal = "stable"

        # Confidence: higher with more data; lower for synthetic
        raw_conf = min(0.92, 0.55 + len(series) * 0.025) if not is_projected else 0.45
        confidence = round(raw_conf, 2)

        # Normalise full series (historical + predicted) to 0-100 for sparkline
        combined = series + predicted
        mn, mx   = min(combined), max(combined) + 1e-9
        sparkline = [round((v - mn) / (mx - mn) * 100) for v in combined]

        return {
            "predicted_counts":   [max(0, round(v)) for v in predicted],
            "trend":              trend,
            "confidence":         confidence,
            "signal":             signal,
            "weekly_sparkline":   sparkline,
            "backend":            backend,
            "is_projected":       is_projected,
            "note":               note,
        }

    # ── Price trajectory ───────────────────────────────────────────────────────

    def predict_price_trajectory(
        self,
        price_history: list[float] | None = None,
        horizon: int = 4,
        category: str = "ecommerce",
        current_price: float | None = None,
    ) -> dict:
        """
        Predict competitor price direction over the next `horizon` weeks.

        Returns
        -------
        {
          predicted_prices   : list[float],
          direction          : "increasing" | "stable" | "decreasing",
          price_war_risk     : bool,
          risk_level         : "high" | "medium" | "low",
          recommendation     : str,
          pct_change_30d     : float,   # expected % change
          backend            : str,
          is_projected       : bool,
          note               : str | None,
        }
        """
        is_projected = False
        note: str | None = None

        if price_history and len(price_history) >= 4:
            series = [float(v) for v in price_history]
        else:
            scale  = (current_price / 1000.0) if current_price else 1.0
            series, is_projected = _build_synthetic(category, _PRICE_BENCHMARKS, scale)
            note   = "Projected from category price benchmarks — connect price monitoring for real data."

        predicted, backend = _forecast(series, horizon)
        direction = _trend_label(series, predicted)
        # Rename for price context
        if direction == "accelerating":
            direction = "increasing"
        elif direction == "declining":
            direction = "decreasing"

        # 30-day expected % change
        ref_price = np.mean(series[-4:]) if len(series) >= 4 else np.mean(series)
        final_price = np.mean(predicted) if predicted else ref_price
        pct_change  = round((final_price - ref_price) / (ref_price + 1e-9) * 100, 1)

        # Price war risk: rapid downward movement > 8%
        price_war_risk = (direction == "decreasing" and abs(pct_change) > 8)
        risk_level = "high" if price_war_risk else ("medium" if abs(pct_change) > 4 else "low")

        if direction == "increasing":
            recommendation = "Lock in pricing now — competitor increases expected. Consider a short-term promo."
        elif price_war_risk:
            recommendation = "Price war risk detected. Defend position with value-add bundles, not pure discounting."
        elif direction == "decreasing":
            recommendation = "Monitor weekly — hold price and emphasise quality differentiation."
        else:
            recommendation = "Market pricing stable. Maintain current strategy and monitor for signals."

        return {
            "predicted_prices":  [round(v, 2) for v in predicted],
            "direction":         direction,
            "price_war_risk":    price_war_risk,
            "risk_level":        risk_level,
            "recommendation":    recommendation,
            "pct_change_30d":    pct_change,
            "backend":           backend,
            "is_projected":      is_projected,
            "note":              note,
        }

    # ── Virality trajectory ────────────────────────────────────────────────────

    def predict_virality_trajectory(
        self,
        engagement_signals: dict,
    ) -> dict:
        """
        Given early engagement signals, predict the 7-day virality trajectory.
        Works without historical data — derives a plausible day-1 view series
        from the engagement proxy score and category benchmarks.

        Parameters
        ----------
        engagement_signals : {
          review_count        : int,
          rating              : float,
          description_length  : int,
          has_images          : bool,
          category            : str  (optional),
          virality_score      : int  (optional, 0-100),
        }

        Returns
        -------
        {
          predicted_7day_reach : int,
          viral_probability    : float (0-1),
          peak_day             : int,
          trajectory           : "exponential" | "linear" | "declining",
          action               : "boost_now" | "let_it_run" | "reconsider",
          day_by_day           : list[int],   # estimated daily reach D1-D7
          backend              : str,
          note                 : str,
        }
        """
        category  = engagement_signals.get("category", "ecommerce")
        v_score   = int(engagement_signals.get("virality_score", 50))
        rating    = float(engagement_signals.get("rating", 4.0) or 4.0)
        reviews   = int(engagement_signals.get("review_count", 0) or 0)
        has_imgs  = bool(engagement_signals.get("has_images", True))
        desc_len  = int(engagement_signals.get("description_length", 200) or 200)

        # ── Build a proxy engagement series from signals ───────────────────────
        # Base daily reach scaled by virality score and quality signals
        base_reach = 200 + v_score * 18           # 200 (cold) → 2000 (viral machine)
        quality_mult = (
            (rating / 5.0) *
            (1.15 if has_imgs else 0.85) *
            min(1.2, 0.9 + desc_len / 2000)
        )
        # Initial 7-day seed series with natural early growth pattern
        day_factors = [1.0, 1.4, 2.1, 3.0, 3.8, 4.3, 4.5]
        if v_score >= 70:
            day_factors = [1.0, 2.2, 4.5, 7.8, 9.5, 10.2, 9.8]  # exponential burst
        elif v_score <= 35:
            day_factors = [1.0, 0.9, 0.8, 0.75, 0.7, 0.65, 0.6]  # declining

        seed_series = [base_reach * quality_mult * f for f in day_factors]

        # ── Run the model ──────────────────────────────────────────────────────
        predicted, backend = _forecast(seed_series, horizon=7)
        # Use predicted values as D1-D7 (model projects from seed pattern)
        day_by_day = [max(1, round(v)) for v in predicted]

        total_reach = sum(day_by_day)
        peak_day    = int(np.argmax(day_by_day)) + 1

        # Trajectory classification
        first_half = np.mean(day_by_day[:3])
        second_half = np.mean(day_by_day[4:])
        ratio = second_half / (first_half + 1e-9)
        if ratio > 1.5:
            trajectory: Literal["exponential", "linear", "declining"] = "exponential"
        elif ratio > 0.85:
            trajectory = "linear"
        else:
            trajectory = "declining"

        # Viral probability: normalise v_score + trajectory signal
        trajectory_bonus = {"exponential": 0.15, "linear": 0.0, "declining": -0.2}
        base_prob  = v_score / 100.0
        viral_prob = max(0.02, min(0.97, base_prob + trajectory_bonus[trajectory]))

        # Action
        if viral_prob >= 0.65:
            action = "boost_now"
        elif viral_prob >= 0.40:
            action = "let_it_run"
        else:
            action = "reconsider"

        return {
            "predicted_7day_reach": total_reach,
            "viral_probability":    round(viral_prob, 2),
            "peak_day":             peak_day,
            "trajectory":           trajectory,
            "action":               action,
            "day_by_day":           day_by_day,
            "backend":              backend,
            "note":                 "Projected from engagement proxy signals.",
        }


# ── Module-level singleton (lazy-loaded in agents) ─────────────────────────────

_predictor: TrendPredictor | None = None


def get_predictor() -> TrendPredictor:
    global _predictor
    if _predictor is None:
        _predictor = TrendPredictor()
    return _predictor
