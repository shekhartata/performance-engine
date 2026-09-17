"""Static matplotlib plots for HTML/Markdown reports."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

COLOR = {"GREEN": "#2e7d32", "AMBER": "#ed6c02", "RED": "#c62828"}


def _save(fig, path: Path | None) -> str:
    buffer = BytesIO()
    fig.tight_layout()
    fig.savefig(buffer, format="png", dpi=120)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, format="png", dpi=120)
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("ascii")


def plot_p95_vs(frame: pd.DataFrame, x: str, path: Path | None = None) -> str | None:
    if x not in frame.columns or "p95_ms" not in frame.columns:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    if "model_id" in frame.columns:
        for model, subset in frame.groupby("model_id"):
            grouped = subset.groupby(x)["p95_ms"].mean()
            ax.plot(grouped.index, grouped.values, marker="o", label=str(model))
        ax.legend()
    else:
        grouped = frame.groupby(x)["p95_ms"].mean()
        ax.plot(grouped.index, grouped.values, marker="o")
    ax.set_xlabel(x)
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title(f"p95 vs {x}")
    ax.grid(True, alpha=0.3)
    return _save(fig, path)


def plot_envelope(envelope: list[dict], path: Path | None = None) -> str | None:
    if not envelope:
        return None
    xs = [row.get("dataset_size") for row in envelope if "dataset_size" in row]
    ys = [row.get("concurrency") for row in envelope if "concurrency" in row]
    if not xs or not ys:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    for cls, color in COLOR.items():
        pts = [row for row in envelope if row.get("class") == cls]
        if not pts:
            continue
        ax.scatter(
            [p["dataset_size"] for p in pts],
            [p.get("concurrency", 1) for p in pts],
            c=color,
            label=cls,
            s=80,
        )
    ax.set_xlabel("dataset size")
    ax.set_ylabel("concurrency")
    ax.set_title("GREEN / AMBER / RED envelope")
    ax.legend()
    ax.grid(True, alpha=0.3)
    return _save(fig, path)


def plot_p95_curves_by_scale(
    frame: pd.DataFrame, path: Path | None = None, x: str = "concurrency"
) -> str | None:
    """One line per dataset size: p95 vs concurrency (or selectivity)."""
    if "dataset_size" not in frame.columns or "p95_ms" not in frame.columns:
        return None
    if x not in frame.columns:
        x = "selectivity" if "selectivity" in frame.columns else ""
        if not x:
            return None
    sizes = sorted(frame["dataset_size"].dropna().unique().tolist())
    if len(sizes) < 1:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    for size, subset in frame.groupby("dataset_size"):
        grouped = subset.groupby(x)["p95_ms"].mean()
        ax.plot(grouped.index, grouped.values, marker="o", label=f"N={int(size)}")
    ax.set_xlabel(x)
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title(f"p95 vs {x} by dataset size")
    ax.legend(title="dataset size")
    ax.grid(True, alpha=0.3)
    return _save(fig, path)


def plot_observed_vs_predicted(predictions: list[dict], path: Path | None = None) -> str | None:
    if not predictions:
        return None
    actual = [p["p95_ms"] for p in predictions if "p95_ms" in p and "predicted_p95_ms" in p]
    pred = [p["predicted_p95_ms"] for p in predictions if "p95_ms" in p and "predicted_p95_ms" in p]
    if not actual:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.scatter(actual, pred, alpha=0.7)
    lo = min(min(actual), min(pred))
    hi = max(max(actual), max(pred))
    ax.plot([lo, hi], [lo, hi], linestyle="--", color="gray")
    ax.set_xlabel("observed p95 (ms)")
    ax.set_ylabel("predicted p95 (ms)")
    ax.set_title("Observed vs predicted latency")
    ax.grid(True, alpha=0.3)
    return _save(fig, path)
