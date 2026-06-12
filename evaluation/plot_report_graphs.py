"""
plot_report_graphs.py

Generates the three performance figures for the sqlite-reconcile evaluation
chapter. Reads the following CSV files from evaluation/results:

    main_benchmark_results.csv  - end-to-end merge benchmark
                                  (workload x transactions_per_branch)
    microbenchmark_results.csv  - per-phase microbenchmarks
                                  (case x rows)

Outputs PNG files under evaluation/figures:

    graph1_runtime.png               - merge runtime vs transactions
    graph_probe_refinement.png       - probe refinement time vs N
    graph_pair_check_pipeline.png    - pair-check pipeline time vs N

Usage:
    python evaluation/plot_report_graphs.py
"""

import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter, ScalarFormatter

# ---------------------------------------------------------------------------
# Shared style
# ---------------------------------------------------------------------------

plt.rcParams.update({
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.labelsize": 12,
    "legend.frameon": True,
    "legend.framealpha": 0.9,
    "figure.dpi": 120,
})

FIGSIZE = (8, 5.5)
EVALUATION_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVALUATION_DIR / "results"
FIGURES_DIR = EVALUATION_DIR / "figures"

# Colour-blind-friendly palette (Okabe-Ito / ColorBrewer derived)
WORKLOAD_COLORS = {
    "no_overlap":    "#0072B2",
    "overlap_10":    "#009E73",
    "overlap_30":    "#E69F00",
    "overlap_70":    "#D55E00",
    "dense_overlap": "#B2182B",
}
WORKLOAD_LABELS = {
    "no_overlap":    "No overlap",
    "overlap_10":    "Overlap 10",
    "overlap_30":    "Overlap 30",
    "overlap_70":    "Overlap 70",
    "dense_overlap": "Dense overlap",
}

CASE_COLORS = {
    "simple_write_write":   "#0072B2",
    "aggregate_write_read": "#009E73",
    "cte_write_read":       "#D55E00",
}
CASE_LABELS = {
    "simple_write_write":   "Simple (write\u2013write)",
    "aggregate_write_read": "Aggregate (write\u2013read)",
    "cte_write_read":       "CTE (write\u2013read)",
}


def log_tick_label(value, _pos):
    """Format a tick as 10^e, or 5x10^e for half-decade ticks."""
    exponent = math.floor(math.log10(value) + 1e-9)
    mantissa = value / 10 ** exponent
    if abs(mantissa - 1) < 0.05:
        return rf"$10^{{{exponent}}}$"
    if abs(mantissa - 5) < 0.05:
        return rf"$5\times10^{{{exponent}}}$"
    return f"{value:g}"


def save(fig, stem):
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / f"{stem}.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Graph 1: merge runtime vs transactions (linear x, log y, +/- 1 s.d. bands)
# ---------------------------------------------------------------------------

def plot_merge_runtime(df):
    xticks = sorted(df["transactions_per_branch"].unique())

    fig, ax = plt.subplots(figsize=FIGSIZE)
    for workload, color in WORKLOAD_COLORS.items():
        d = df[df["workload"] == workload].sort_values("transactions_per_branch")
        x = d["transactions_per_branch"].to_numpy()
        y = d["elapsed_seconds"].to_numpy()
        sd = d["elapsed_stdev_seconds"].to_numpy()

        ax.plot(x, y, marker="o", markersize=5, linewidth=2,
                color=color, label=WORKLOAD_LABELS[workload], zorder=3)
        # Shaded +/- 1 s.d. band; clip the lower edge so log scale stays valid.
        ax.fill_between(x, np.clip(y - sd, 1e-9, None), y + sd,
                        color=color, alpha=0.18, linewidth=0, zorder=1)

    ax.set_yscale("log")
    ax.set_title("Merge runtime vs transactions (mean \u00b1 1 s.d.)")
    ax.set_xlabel("Transactions per branch")
    ax.set_ylabel("Elapsed time (seconds)")

    # Linear x starting at the origin; y extended so the 10^1 gridline shows.
    ax.set_xlim(0, 104)
    ax.set_xticks([0] + xticks)
    ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.set_ylim(0.0045, 13)
    ax.yaxis.set_major_locator(FixedLocator([0.005, 0.01, 0.1, 1, 10]))
    ax.yaxis.set_major_formatter(FuncFormatter(log_tick_label))

    ax.grid(True, alpha=0.3)
    ax.legend(title="Workload", loc="upper left")
    save(fig, "graph1_runtime")


# ---------------------------------------------------------------------------
# Graphs 2 & 3: microbenchmark phases vs scale parameter N (log-log, no bands)
# ---------------------------------------------------------------------------

def plot_microbenchmark(df, metric, title, ylabel, ylim, yticks, stem):
    xticks = sorted(df["rows"].unique())

    fig, ax = plt.subplots(figsize=FIGSIZE)
    for case, color in CASE_COLORS.items():
        d = df[df["case"] == case].sort_values("rows")
        ax.plot(d["rows"], d[f"{metric}_mean_ms"], marker="o", markersize=5,
                linewidth=2, color=color, label=CASE_LABELS[case], zorder=3)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_title(title)
    ax.set_xlabel("Scale parameter N")
    ax.set_ylabel(ylabel)

    # x: powers-of-10 labels at the measured row counts.
    ax.set_xlim(7, 140_000)
    ax.set_xticks(xticks)
    ax.xaxis.set_major_formatter(FuncFormatter(log_tick_label))
    ax.xaxis.set_minor_formatter(NullFormatter())

    # y: compact range bracketing the data, with half-decade ticks where needed.
    ax.set_ylim(*ylim)
    ax.yaxis.set_major_locator(FixedLocator(yticks))
    ax.yaxis.set_major_formatter(FuncFormatter(log_tick_label))
    ax.yaxis.set_minor_formatter(NullFormatter())

    ax.grid(True, which="major", alpha=0.3)
    ax.grid(True, which="minor", axis="y", alpha=0.12)
    ax.legend(title="Case", loc="upper left")
    save(fig, stem)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    performance = pd.read_csv(RESULTS_DIR / "main_benchmark_results.csv")
    microbench = pd.read_csv(RESULTS_DIR / "microbenchmark_results.csv")

    plot_merge_runtime(performance)

    plot_microbenchmark(
        microbench,
        metric="probe_refinement",
        title="Probe-Based Refinement Time vs Scale Parameter",
        ylabel="Probe refinement time (ms)",
        ylim=(0.09, 250),
        yticks=[0.1, 1, 10, 100],
        stem="graph_probe_refinement",
    )

    plot_microbenchmark(
        microbench,
        metric="pair_check_pipeline",
        title="Pair-Check Pipeline Time vs Scale Parameter",
        ylabel="Pair-check pipeline time (ms)",
        ylim=(0.45, 600),
        yticks=[0.5, 1, 10, 100, 500],
        stem="graph_pair_check_pipeline",
    )

    print(
        "Wrote graph1_runtime, graph_probe_refinement, "
        f"graph_pair_check_pipeline (.png) to {FIGURES_DIR}"
    )


if __name__ == "__main__":
    main()
