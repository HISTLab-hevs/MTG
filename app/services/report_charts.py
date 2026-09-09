"""Inline SVG charts for the course report, built from the stored metrics.

Two charts, both pure functions of the deterministic metrics dict
(report_metrics), computing no data of their own: a radar over the improvement
areas, showing how well the course does on each axis, and a severity bar of the
failures.

Returned as self-contained SVG strings so they render the same in the web page
and, rasterised, in the PDF, with no system charting dependency.
"""

import math

from app.models import RuleCategory


_AXES = ["outcomes", "assessment", "ai_readiness", "content", "clarity"]

_AXIS_LABEL = {axis: RuleCategory.short_label(axis) for axis in _AXES}

_PAD_X = 56


def _point(cx: float, cy: float, r: float, angle: float) -> tuple[float, float]:
    """Cartesian coordinates of a polar point around a centre."""
    return cx + r * math.cos(angle), cy + r * math.sin(angle)


def radar_svg(metrics: dict, size: int = 260) -> str | None:
    """Radar of the pass percentage per improvement area.

    Args:
        metrics: Stored metrics dict, read for its categories breakdown.
        size: Height of the chart in user units; the viewBox is wider, to leave
            room for the axis labels.

    Returns:
        The SVG, or None when fewer than three areas were checked, since a
        radar needs at least a triangle to read as one.
    """
    by_cat = {c["category"]: c for c in metrics.get("categories", [])}
    axes = [a for a in _AXES if a in by_cat]
    if len(axes) < 3:
        return None
    cy = size / 2
    cx = size / 2 + _PAD_X  # shift right; the viewBox is widened by 2 * _PAD_X
    r = size / 2 - 50  # leave room for the labels outside the grid
    n = len(axes)
    grid = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(
            f"{x:.1f},{y:.1f}"
            for i in range(n)
            for x, y in [_point(cx, cy, r * frac, -math.pi / 2 + 2 * math.pi * i / n)]
        )
        grid.append(
            f'<polygon points="{pts}" fill="none" stroke="#e2e8f0" stroke-width="1"/>'
        )
    shape_pts = []
    labels = []
    for i, axis in enumerate(axes):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        pct = by_cat[axis]["pass_pct"] / 100
        x, y = _point(cx, cy, r * pct, angle)
        shape_pts.append(f"{x:.1f},{y:.1f}")
        lx, ly = _point(cx, cy, r + 14, angle)
        # Snap near-vertical labels (top/bottom axes) to centred; side labels
        # anchor away from the chart so long names grow outward into the pad.
        if abs(lx - cx) < 2:
            anchor = "middle"
        else:
            anchor = "start" if lx > cx else "end"
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" font-size="10" fill="#475569">'
            f'{_AXIS_LABEL[axis]}</text>'
        )
    shape = (
        f'<polygon points="{" ".join(shape_pts)}" fill="#6366f1" '
        f'fill-opacity="0.25" stroke="#6366f1" stroke-width="2"/>'
    )
    vb_w = size + 2 * _PAD_X
    return (
        f'<svg viewBox="0 0 {vb_w} {size}" width="{vb_w}" height="{size}" '
        f'role="img" aria-label="Pass rate by improvement area" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'{"".join(grid)}{shape}{"".join(labels)}</svg>'
    )


def severity_bar_svg(metrics: dict, width: int = 260) -> str | None:
    """Horizontal bars of the failure counts by severity.

    Args:
        metrics: Stored metrics dict, read for its severity breakdown.
        width: Width of the chart in user units.

    Returns:
        The SVG, or None when nothing failed.
    """
    counts = _failure_counts(metrics)
    total = sum(counts.values())
    if not total:
        return None
    rows = [("high", "#ef4444"), ("medium", "#f59e0b"), ("low", "#94a3b8")]
    bar_max = width - 90
    peak = max(counts.values()) or 1
    parts = []
    y = 8
    for name, color in rows:
        c = counts[name]
        w = (bar_max * c / peak) if c else 0
        parts.append(
            f'<text x="0" y="{y + 11}" font-size="10" fill="#475569">'
            f'{name.title()}</text>'
            f'<rect x="56" y="{y}" width="{w:.1f}" height="14" rx="3" fill="{color}"/>'
            f'<text x="{56 + w + 6:.1f}" y="{y + 11}" font-size="10" '
            f'fill="#475569">{c}</text>'
        )
        y += 22
    height = y + 2
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Failures by severity" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )


def _failure_counts(metrics: dict) -> dict[str, int]:
    """Failure counts by severity, straight from the stored breakdown."""
    fs = metrics.get("failures_by_severity") or {}
    return {k: int(fs.get(k, 0)) for k in ("high", "medium", "low")}
