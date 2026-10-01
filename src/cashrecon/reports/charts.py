"""Tiny inline-SVG charts (no JavaScript, render in browsers and mail clients)."""

from __future__ import annotations

from html import escape

POS = "#1a7f37"
NEG = "#cf222e"
LINE = "#0a4da2"
GRID = "#d0d7de"
TEXT = "#57606a"


def _scale(values: list[float]) -> tuple[float, float]:
    lo, hi = min(values + [0]), max(values + [0])
    if lo == hi:
        hi = lo + 1
    return lo, hi


def bar_chart(labels: list[str], values: list[int], *, width: int = 640, height: int = 180,
              title: str = "") -> str:
    """Signed bars (cents). Positive green, negative red, zero line."""
    if not values:
        return ""
    yuan = [v / 100 for v in values]
    lo, hi = _scale(yuan)
    top, bottom, left = 16, 22, 4
    plot_h = height - top - bottom
    step = (width - left * 2) / len(yuan)
    bar_w = max(2.0, step * 0.7)

    def y(v: float) -> float:
        return top + (hi - v) / (hi - lo) * plot_h

    zero = y(0)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{escape(title)}" '
             f'xmlns="http://www.w3.org/2000/svg">',
             f'<line x1="0" x2="{width}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="{GRID}"/>']
    every = max(1, len(labels) // 10)
    for i, v in enumerate(yuan):
        x = left + i * step + (step - bar_w) / 2
        y0, y1 = sorted((y(v), zero))
        color = POS if v >= 0 else NEG
        parts.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bar_w:.1f}" height="{max(0.5, y1 - y0):.1f}" '
                     f'fill="{color}"><title>{escape(labels[i])}：{v:,.2f}</title></rect>')
        if i % every == 0 or i == len(yuan) - 1:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 6}" font-size="10" fill="{TEXT}" '
                         f'text-anchor="middle">{escape(labels[i])}</text>')
    parts.append(f'<text x="{width - 2}" y="11" font-size="10" fill="{TEXT}" text-anchor="end">'
                 f'最高 {hi:,.0f} / 最低 {lo:,.0f}</text></svg>')
    return "".join(parts)


def line_chart(labels: list[str], values: list[int | None], *, width: int = 640, height: int = 160,
               title: str = "") -> str:
    points = [(i, v / 100) for i, v in enumerate(values) if v is not None]
    if len(points) < 2:
        return ""
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    top, bottom, left = 14, 22, 4
    plot_h = height - top - bottom
    step = (width - left * 2) / max(1, len(values) - 1)
    coords = [(left + i * step, top + (hi - v) / (hi - lo) * plot_h) for i, v in points]
    path = " ".join(f"{'M' if n == 0 else 'L'}{x:.1f},{y:.1f}" for n, (x, y) in enumerate(coords))
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{escape(title)}" '
             f'xmlns="http://www.w3.org/2000/svg">',
             f'<path d="{path}" fill="none" stroke="{LINE}" stroke-width="2"/>']
    for (i, v), (x, yy) in zip(points, coords, strict=True):
        parts.append(f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="2.5" fill="{LINE}"><title>{escape(labels[i])}：'
                     f'{v:,.2f}</title></circle>')
    every = max(1, len(labels) // 8)
    for i, label in enumerate(labels):
        if i % every == 0 or i == len(labels) - 1:
            parts.append(f'<text x="{left + i * step:.1f}" y="{height - 6}" font-size="10" fill="{TEXT}" '
                         f'text-anchor="middle">{escape(label)}</text>')
    parts.append(f'<text x="{width - 2}" y="11" font-size="10" fill="{TEXT}" text-anchor="end">'
                 f'最高 {hi:,.0f} / 最低 {lo:,.0f}</text></svg>')
    return "".join(parts)


def share_bar(value: int, total: int, color: str = LINE) -> str:
    pct = 0 if not total else max(0.0, min(100.0, abs(value) / abs(total) * 100))
    return (f'<span class="share"><span style="width:{pct:.1f}%;background:{color}"></span></span>')
