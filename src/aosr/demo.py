"""A self-contained HTML page showing how one image grew: the stream, the library, the kernel, the results."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from aosr.evaluate import load_arm, summarize
from aosr.store import Image, Store

CSS = """
:root{color-scheme:light} html{background:#fff} body{font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:32px auto;max-width:1080px;color:#1d1d1f;background:#fff;padding:0 16px}
h1{font-size:26px;margin:0 0 4px} h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.sub{color:#666} .k{display:inline-block;margin:8px 24px 8px 0} .k b{font-size:22px;display:block}
table{border-collapse:collapse;width:100%;font-size:13px} td,th{border-bottom:1px solid #eee;padding:4px 6px;text-align:left;vertical-align:top}
code,pre{font:12px/1.4 ui-monospace,Menlo,monospace} pre{background:#f6f6f6;padding:8px;overflow:auto;max-height:280px}
.ok{color:#1a7f37} .no{color:#b42318} details{margin:6px 0} svg text{font:11px -apple-system,Helvetica,sans-serif}
"""


def _line_chart(series: Mapping[str, Sequence[tuple[int, float]]], width: int = 1040, height: int = 220) -> str:
    colors = ["#2563eb", "#16a34a", "#d97706", "#9333ea"]
    xs = [x for pts in series.values() for x, _ in pts] or [0, 1]
    ys = [y for pts in series.values() for _, y in pts] or [0, 1]
    x0, x1, y1 = min(xs), max(xs) or 1, max(ys) or 1
    pad = 36

    def px(x: float) -> float:
        return pad + (x - x0) / max(x1 - x0, 1) * (width - 2 * pad)

    def py(y: float) -> float:
        return height - pad - y / y1 * (height - 2 * pad)

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%">']
    out.append(f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="#999"/>')
    out.append(f'<text x="{width - pad}" y="{height - 8}" text-anchor="end">stream tasks seen</text>')
    out.append(f'<text x="4" y="{pad - 8}">{y1:g}</text>')
    for i, (name, pts) in enumerate(series.items()):
        c = colors[i % len(colors)]
        path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
        out.append(f'<polyline fill="none" stroke="{c}" stroke-width="2" points="{path}"/>')
        out.append(f'<text x="{pad + 8 + i * 170}" y="{pad - 8}" fill="{c}">■ {html.escape(name)}</text>')
    out.append("</svg>")
    return "".join(out)


def _graph(image: Image) -> str:
    """Capabilities laid out in columns by static depth; edges go from caller to callee."""
    depths = image.depths()
    if not depths:
        return "<p class='sub'>No capabilities.</p>"
    cols: dict[int, list[str]] = {}
    for n, d in depths.items():
        cols.setdefault(d, []).append(n)
    rows = max(len(v) for v in cols.values())
    w, rh = 1040, 18
    h = 30 + rows * rh
    colw = w / (max(cols) + 1)
    pos = {n: (20 + (d - 1) * colw, 24 + i * rh) for d, names in cols.items() for i, n in enumerate(sorted(names))}
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%">']
    for d in sorted(cols):
        out.append(f'<text x="{20 + (d - 1) * colw}" y="12" fill="#666">depth {d}</text>')
    for n in depths:
        for dep in image.cap(n)["deps"]:
            if dep in pos:
                (x1, y1), (x2, y2) = pos[n], pos[dep]
                out.append(f'<line x1="{x1}" y1="{y1 - 4}" x2="{x2 + 6}" y2="{y2 - 4}" stroke="#bbb"/>')
    for n, (x, y) in pos.items():
        uses = image.head.caps[n].uses
        out.append(f'<circle cx="{x}" cy="{y - 4}" r="{3 + min(uses, 12) / 2}" fill="#2563eb"/>')
        out.append(f'<text x="{x + 9}" y="{y}">{html.escape(n)} ({uses})</text>')
    out.append("</svg>")
    return "".join(out)


def render(store: Store, run: str, grow_log: Path, evals: list[Path], title: str = "") -> str:
    final = store.resolve(f"{run}/latest")
    image = Image(store, store.head(final))
    recs = [json.loads(x) for x in grow_log.read_text().splitlines() if x.strip()]
    tasks = [r for r in recs if "position" in r]
    epochs = [r for r in recs if "epoch" in r]
    # growth over the stream, read back from the image lineage
    lineage = [store.get(d) for d in reversed(store.lineage(final))]
    caps_at, progs_at = [], []
    seen = 0
    for h in lineage[1:]:
        note = h.get("note", "")
        if "after" in note:
            seen = int(note.split("after ")[1].split()[0])
        caps_at.append((seen, len(h["caps"])))
        progs_at.append((seen, len(h["programs"])))
    solved, cum = [], 0
    for r in tasks:
        cum += r["correct"]
        solved.append((r["position"] + 1, cum))
    s = image.summary()
    parts = [f"<!doctype html><meta charset=utf-8><title>{html.escape(title or run)}</title><style>{CSS}</style>"]
    parts.append(f"<h1>{html.escape(title or run)}</h1>")
    parts.append(
        f"<div class=sub>image <code>{final[:16]}</code> · grown from the empty image over {len(tasks)} stream tasks</div>"
    )
    parts.append(
        "".join(
            f"<span class=k><b>{v}</b>{k}</span>"
            for k, v in [
                ("stream tasks solved", sum(r["correct"] for r in tasks)),
                ("capabilities", s["capabilities"]),
                ("programs remembered", s["programs"]),
                ("deepest capability", s["max_depth"]),
                ("kernel edits accepted", sum(e["accepted"] for e in epochs)),
            ]
        )
    )
    parts.append("<h2>Growth along the stream</h2>")
    parts.append(_line_chart({"tasks solved": solved, "capabilities": caps_at, "programs": progs_at}))
    parts.append(
        "<h2>The library it grew</h2><p class=sub>Columns are depth (a capability at depth k calls one at depth k-1); the number is how many later solved tasks executed it.</p>"
    )
    parts.append(_graph(image))
    rows = []
    depths = image.depths()
    for n, e in sorted(image.head.caps.items(), key=lambda kv: (-kv[1].uses, kv[1].admitted)):
        c = image.cap(n)
        src = html.escape(c["src"])
        rows.append(
            f"<tr><td><code>{html.escape(c['sig'])}</code><details><summary>source</summary><pre>{src}</pre></details></td>"
            f"<td>{html.escape(c['doc'])}</td><td>{e.uses}</td><td>{depths[n]}</td><td>{e.admitted}</td></tr>"
        )
    parts.append(
        "<table><tr><th>capability</th><th>what it does</th><th>reused</th><th>depth</th><th>born at</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    parts.append("<h2>Kernel evolution</h2>")
    if not epochs:
        parts.append("<p class=sub>No kernel epochs in this run.</p>")
    for e in epochs:
        cls, word = ("ok", "accepted") if e["accepted"] else ("no", "rejected")
        parts.append(
            f"<details><summary>after {e['epoch']} tasks: <b class={cls}>{word}</b> — "
            f"{e['old']} → {e['new']} of {len(e['window'])} recent tasks solved</summary>"
            f"<pre>{html.escape(e['notes'])}</pre>"
            + (f"<pre>policy {html.escape(e.get('policy', ''))}</pre>" if e.get("policy") else "")
            + "</details>"
        )
    notes = image.slot_source("notes")
    if notes:
        parts.append(f"<p>Operating notes in the final image:</p><pre>{html.escape(notes)}</pre>")
    if evals:
        parts.append(
            "<h2>Held-out results</h2><table><tr><th>arm</th><th>tasks</th><th>budget</th><th>solved</th><th>rate</th><th>model calls</th><th>USD</th><th>solved using the library</th><th>solved with no model call</th></tr>"
        )
        for p in evals:
            h, rws = load_arm(p)
            m = summarize(h, rws)
            parts.append(
                f"<tr><td>{html.escape(m['arm'])}</td><td>{m['n']}</td><td>{m['budget']}</td><td>{m['solved']}</td>"
                f"<td>{m['rate']:.1%}</td><td>{m['calls']}</td><td>{m['usd']:.2f}</td><td>{m['solved_with_library']}</td><td>{m['presolved']}</td></tr>"
            )
        parts.append("</table>")
    return "".join(parts)


def write(store: Store, run: str, grow_log: Path, evals: list[Path], out: Path, title: str = "") -> Path:
    out.write_text(render(store, run, grow_log, evals, title))
    return out
