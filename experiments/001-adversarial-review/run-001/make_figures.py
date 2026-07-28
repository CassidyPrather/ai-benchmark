"""Confidence-forward figures for Experiment 001 Run 001.

Reads the committed ``run-001-results.json`` and renders three figures that
foreground *uncertainty* rather than point estimates, in the spirit of the
write-up:

1. ``forest`` -- paired difference in regression rate per contrast, with 95%
   bootstrap CIs and a zero line (the headline "how confident are we" chart).
2. ``rates`` -- per-condition regression rate (with Wilson CIs) beside the flat
   resolution guardrail (arms solved equally -> the gaps aren't a do-nothing
   artifact).
3. ``discordant`` -- the McNemar evidence base: how few tasks actually disagreed
   between arms (the inference rests on 11-15 of 65 pairs).

Run (matplotlib is not a repo dep):
    uv run --with matplotlib python experiments/001-adversarial-review/run-001/make_figures.py

Outputs SVG (vector, for the blog) + PNG (preview) under ``figures/``. Palette and
mark specs follow the bundled dataviz skill's validated reference palette.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib as mpl  # ty: ignore[unresolved-import]

mpl.use("Agg")
import matplotlib.pyplot as plt  # ty: ignore[unresolved-import]

# --- palette (dataviz skill reference instance; both modes are *selected*,
# each stepped for its own surface -- references/palette.md) ------------------
# The light values also define the module-level names (linters resolve them);
# _apply_theme() overwrites them per render pass (light, then dark).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASE = "#c3c2b7"
BLUE = "#2a78d6"  # categorical slot 1 / diverging cool pole
ORANGE = "#eb6834"  # categorical slot 2
RED = "#e34948"  # diverging warm pole

_KEYS = ("SURFACE", "INK", "INK2", "MUTED", "GRID", "BASE", "BLUE", "ORANGE", "RED")
_LIGHT = {k: globals()[k] for k in _KEYS}
_DARK = {
    "SURFACE": "#1a1a19",
    "INK": "#ffffff",
    "INK2": "#c3c2b7",
    "MUTED": "#898781",
    "GRID": "#2c2c2a",
    "BASE": "#383835",
    "BLUE": "#3987e5",
    "ORANGE": "#d95926",
    "RED": "#e66767",
}
_STATE = {"suffix": ""}

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "run-001-results.json"
OUT = HERE / "figures"

_BASE_RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans", "Arial"],
    "font.size": 10.5,
    "axes.linewidth": 1.0,
    "xtick.labelsize": 9.5,
    "ytick.labelsize": 10.5,
    "axes.titlesize": 13,
    "svg.fonttype": "none",
}


def _apply_theme(palette: dict, suffix: str) -> None:
    """Point the module palette + color rcParams at one theme for a render pass."""
    globals().update(palette)
    _STATE["suffix"] = suffix
    plt.rcParams.update(_BASE_RC)
    plt.rcParams.update({
        "figure.facecolor": palette["SURFACE"],
        "axes.facecolor": palette["SURFACE"],
        "savefig.facecolor": palette["SURFACE"],
        "text.color": palette["INK2"],
        "axes.labelcolor": palette["INK2"],
        "axes.edgecolor": palette["BASE"],
        "xtick.color": palette["MUTED"],
        "ytick.color": palette["MUTED"],
    })


def wilson(k: int, n: int, z: float = 1.959963985) -> tuple[float, float, float]:
    """95% Wilson score interval for a binomial proportion -> (lo, mid, hi)."""
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, p, center + half)


def _spines(ax, *, left: bool = True, bottom: bool = True) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(left)
    ax.spines["bottom"].set_visible(bottom)


def _save(fig, name: str) -> None:
    OUT.mkdir(exist_ok=True)
    for ext in ("svg", "png"):
        fig.savefig(OUT / f"{name}{_STATE['suffix']}.{ext}", dpi=200)
    plt.close(fig)


def _rows(data: dict) -> list[tuple[str, str, dict, bool]]:
    return [
        ("Adversarial − Self-review", "primary · H1", data["primary"], True),
        (
            "Adversarial − Control",
            "secondary",
            data["secondary"]["adversarial_vs_control"],
            False,
        ),
        (
            "Self-review − Control",
            "secondary",
            data["secondary"]["self_review_vs_control"],
            False,
        ),
    ]


def fig_forest(data: dict) -> None:
    """Forest plot: paired Delta regression rate + 95% bootstrap CI per contrast."""
    fig, ax = plt.subplots(figsize=(8.8, 4.2))
    ys = [2, 1, 0]
    ann_x = 19.5
    for y, (label, kind, c, primary) in zip(ys, _rows(data), strict=True):
        rd = c["rate_diff"]
        est, lo, hi = rd["estimate"] * 100, rd["ci_lower"] * 100, rd["ci_upper"] * 100
        p, ndisc = c["mcnemar"]["p_value"], c["mcnemar"]["n_discordant"]
        ax.plot([lo, hi], [y, y], color=BLUE, lw=2.0, solid_capstyle="round", zorder=3)
        for xend in (lo, hi):
            ax.plot([xend, xend], [y - 0.12, y + 0.12], color=BLUE, lw=2.0, zorder=3)
        ax.plot([est], [y], "o", ms=9, color=BLUE, mec=SURFACE, mew=1.6, zorder=4)
        weight = "bold" if primary else "normal"
        ax.text(
            -24.5,
            y + 0.22,
            label,
            ha="left",
            va="center",
            color=INK,
            fontsize=11,
            fontweight=weight,
        )
        ax.text(
            -24.5, y - 0.24, kind, ha="left", va="center", color=MUTED, fontsize=8.5
        )
        ax.text(
            ann_x,
            y + 0.22,
            f"Δ {est:+.1f} pp",
            ha="left",
            va="center",
            color=INK,
            fontsize=10,
            fontweight=weight,
        )
        ax.text(
            ann_x,
            y - 0.24,
            f"p = {p:.2f}  ·  {ndisc} discordant",
            ha="left",
            va="center",
            color=MUTED,
            fontsize=8.5,
        )
    ax.axvline(0, color=BASE, lw=1.4, zorder=1)
    ax.text(
        0, 2.6, "no difference", ha="center", va="bottom", color=MUTED, fontsize=8.5
    )
    ax.annotate(
        "",
        xy=(-15, -0.7),
        xytext=(-3, -0.7),
        arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 1.0},
    )
    ax.annotate(
        "",
        xy=(15, -0.7),
        xytext=(3, -0.7),
        arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 1.0},
    )
    ax.text(
        -9,
        -0.92,
        "review regressed less",
        ha="center",
        va="top",
        color=INK2,
        fontsize=8.5,
    )
    ax.text(
        9,
        -0.92,
        "review regressed more",
        ha="center",
        va="top",
        color=INK2,
        fontsize=8.5,
    )
    ax.set_xlim(-25, 33)
    ax.set_ylim(-1.3, 2.85)
    ax.set_xticks([-20, -10, 0, 10])
    ax.set_xticklabels(["−20", "−10", "0", "+10"])
    ax.set_yticks([])
    ax.xaxis.grid(visible=True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    _spines(ax, left=False, bottom=True)
    ax.set_xlabel(
        "Difference in PASS_TO_PASS regression rate (percentage points)",
        color=INK2,
        fontsize=10,
        labelpad=6,
    )
    ax.set_title(
        "Did review reduce regressions?  Paired difference vs. baseline, 95% CI",
        color=INK,
        loc="left",
        pad=14,
        fontweight="bold",
    )
    fig.subplots_adjust(left=0.02, right=0.99, top=0.86, bottom=0.19)
    fig.text(
        0.02,
        0.03,
        "n = 65 paired tasks.  Negative = the review arm introduced fewer regressions than its baseline.  "
        "The primary interval spans zero.",
        color=MUTED,
        fontsize=8.4,
        ha="left",
    )
    _save(fig, "fig1-forest-ci")


def fig_rates(data: dict) -> None:
    """Per-condition regression rate (Wilson CI) beside the flat resolution guardrail."""
    order = [
        ("control", "Control"),
        ("self_review", "Self-review"),
        ("adversarial", "Adversarial"),
    ]
    fig, ax = plt.subplots(figsize=(7.4, 4.3))
    xs = [0, 1, 2]
    dx = 0.12
    for color, key_k, off, lbl in [
        (BLUE, "n_regressed", -dx, "Regression rate"),
        (ORANGE, "n_f2p_resolved", +dx, "Resolution (F2P)"),
    ]:
        for x, (cond, _name) in zip(xs, order, strict=True):
            pc = data["per_condition"][cond]
            lo, mid, hi = wilson(pc[key_k], pc["n"])
            ax.plot(
                [x + off, x + off],
                [lo * 100, hi * 100],
                color=color,
                lw=2.0,
                solid_capstyle="round",
                zorder=3,
            )
            ax.plot(
                [x + off],
                [mid * 100],
                "o",
                ms=8.5,
                color=color,
                mec=SURFACE,
                mew=1.5,
                zorder=4,
                label=lbl if x == 0 else None,
            )
            ax.text(
                x + off,
                hi * 100 + 1.8,
                f"{mid * 100:.0f}%",
                ha="center",
                va="bottom",
                color=INK,
                fontsize=9,
                fontweight="bold",
            )
    ax.set_xticks(xs)
    ax.set_xticklabels([n for _, n in order], color=INK, fontsize=11)
    ax.set_xlim(-0.5, 2.5)
    ax.set_ylim(0, 86)
    ax.set_yticks([0, 20, 40, 60])
    ax.set_yticklabels(["0", "20", "40", "60%"])
    ax.yaxis.grid(visible=True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    _spines(ax, left=True, bottom=True)
    ax.set_ylabel("Rate over 65 tasks", color=INK2, fontsize=10)
    ax.set_title(
        "Regression rate moves a little; resolution is flat",
        color=INK,
        loc="left",
        pad=14,
        fontweight="bold",
    )
    ax.legend(
        loc="upper left",
        frameon=False,
        fontsize=9.5,
        handletextpad=0.4,
        labelcolor=INK2,
    )
    fig.subplots_adjust(left=0.09, right=0.98, top=0.86, bottom=0.16)
    fig.text(
        0.02,
        0.03,
        "Rates over 65 tasks, 95% Wilson CIs.  Resolution is flat across arms — they solved equally often.",
        color=MUTED,
        fontsize=8.4,
        ha="left",
    )
    _save(fig, "fig2-rates-guardrail")


def fig_discordant(data: dict) -> None:
    """Diverging discordant-pair counts: the McNemar evidence base is thin."""
    fig, ax = plt.subplots(figsize=(8.8, 4.0))
    ys = [2.6, 1.3, 0.0]
    for y, (label, kind, c, primary) in zip(ys, _rows(data), strict=True):
        b = c["mcnemar"][
            "b_baseline_only"
        ]  # only baseline regressed -> favors treatment
        cc = c["mcnemar"][
            "c_treatment_only"
        ]  # only treatment regressed -> favors baseline
        concord = c["n_tasks"] - b - cc
        ax.barh(y, -b, height=0.42, color=BLUE, zorder=3)
        ax.barh(y, cc, height=0.42, color=RED, zorder=3)
        if b:
            ax.text(
                -b - 0.25,
                y,
                str(b),
                ha="right",
                va="center",
                color=INK,
                fontsize=10,
                fontweight="bold",
            )
        if cc:
            ax.text(
                cc + 0.25,
                y,
                str(cc),
                ha="left",
                va="center",
                color=INK,
                fontsize=10,
                fontweight="bold",
            )
        weight = "bold" if primary else "normal"
        ax.text(
            -10.7,
            y + 0.44,
            label,
            ha="left",
            va="bottom",
            color=INK,
            fontsize=10.5,
            fontweight=weight,
        )
        ax.text(
            10.7,
            y + 0.44,
            f"{kind}  ·  {concord} / 65 tasks agreed",
            ha="right",
            va="bottom",
            color=MUTED,
            fontsize=8.5,
        )
    ax.axvline(0, color=BASE, lw=1.4, zorder=1)
    ax.set_xlim(-11, 11)
    ax.set_ylim(-0.55, 3.75)
    ax.set_xticks([-8, -6, -4, -2, 0, 2, 4, 6, 8])
    ax.set_xticklabels([8, 6, 4, 2, 0, 2, 4, 6, 8])
    ax.set_yticks([])
    ax.xaxis.grid(visible=True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    _spines(ax, left=False, bottom=True)
    ax.text(
        -5,
        3.35,
        "tasks where review helped",
        ha="center",
        va="bottom",
        color=BLUE,
        fontsize=9,
    )
    ax.text(
        5,
        3.35,
        "tasks where review hurt",
        ha="center",
        va="bottom",
        color=RED,
        fontsize=9,
    )
    ax.set_xlabel(
        "Number of disagreeing (discordant) tasks", color=INK2, fontsize=10, labelpad=6
    )
    ax.set_title(
        "Every verdict rests on a handful of disagreeing tasks",
        color=INK,
        loc="left",
        pad=14,
        fontweight="bold",
    )
    fig.subplots_adjust(left=0.02, right=0.99, top=0.85, bottom=0.19)
    fig.text(
        0.02,
        0.03,
        "Only the disagreeing tasks inform the McNemar test.  Left (blue) = only the baseline regressed;  "
        "right (red) = only the review arm.",
        color=MUTED,
        fontsize=8.4,
        ha="left",
    )
    _save(fig, "fig3-discordant-pairs")


def main() -> None:
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    for palette, suffix in ((_LIGHT, ""), (_DARK, "-dark")):
        _apply_theme(palette, suffix)
        fig_forest(data)
        fig_rates(data)
        fig_discordant(data)
    print(f"wrote light + dark figures to {OUT}")


if __name__ == "__main__":
    main()
