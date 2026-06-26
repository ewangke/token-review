#!/usr/bin/env python3
"""Render the token-review report dict as a self-contained HTML page.

No external assets, no JS framework — just inline SVG bars and clean
CSS so the file works offline and is safe to email or save.
"""

from __future__ import annotations

import html
import json
from datetime import datetime

CSS = """
:root {
  --bg: #0f1115; --panel: #161a22; --ink: #e7eaf0;
  --muted: #8b94a7; --accent: #7aa2f7; --warn: #e0af68;
  --good: #9ece6a; --bad: #f7768e; --line: #232838;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px;
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
        Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--ink);
}
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 28px 0 12px; color: var(--muted);
     text-transform: uppercase; letter-spacing: 0.08em; }
h3 { font-size: 14px; margin: 0 0 8px; font-weight: 600; }
.sub { color: var(--muted); margin-bottom: 24px; }
.grid { display: grid; gap: 16px; }
.cards { grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); }
.card { background: var(--panel); border: 1px solid var(--line);
        border-radius: 10px; padding: 14px 16px; }
.card .k { color: var(--muted); font-size: 12px; }
.card .v { font-size: 22px; font-weight: 600; margin-top: 4px; }
.card .v.small { font-size: 16px; font-weight: 500; }
.row { display: flex; align-items: center; gap: 10px;
       padding: 6px 0; border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: none; }
.row .name { flex: 1; font-family: ui-monospace, SFMono-Regular,
             Menlo, monospace; font-size: 13px; }
.row .bar { flex: 2; height: 8px; background: var(--line);
            border-radius: 4px; overflow: hidden; }
.row .bar > i { display:block; height:100%; background: var(--accent); }
.row .val { width: 110px; text-align: right; color: var(--muted);
            font-variant-numeric: tabular-nums; font-size: 13px; }
.row .val.big { color: var(--ink); }
.hours { display: grid; grid-template-columns: 32px 1fr 70px; gap: 8px;
         align-items: center; }
.hours > .label { color: var(--muted);
                  font-family: ui-monospace, monospace; }
.session { background: var(--panel); border: 1px solid var(--line);
           border-radius: 10px; padding: 14px 16px; margin-bottom: 10px;}
.session .head { display: flex; gap: 12px; align-items: baseline;
                 flex-wrap: wrap; margin-bottom: 6px; }
.session .rank { color: var(--warn); font-weight: 700;
                 font-variant-numeric: tabular-nums; }
.session .sid { font-family: ui-monospace, monospace;
                color: var(--accent); }
.session .meta { color: var(--muted); font-size: 12px; }
.session .task { color: var(--ink); margin-top: 6px;
                 white-space: pre-wrap; word-break: break-word; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.tag { background: var(--line); color: var(--muted);
       padding: 2px 8px; border-radius: 999px; font-size: 11px; }
.tag.opus { background: #3b2f4a; color: #c9a7f5; }
.tag.sonnet { background: #1f3a4d; color: #8fbcd9; }
.tag.haiku { background: #2c3a2c; color: #a7d39f; }
.tag.share { background: #2a2f3d; color: #c0c7d6;
             font-variant-numeric: tabular-nums; }
.rec { margin-top: 8px; padding: 8px 10px; border-radius: 8px;
       background: #1b2230; border-left: 3px solid var(--accent);
       font-size: 13px; color: var(--ink); }
.rec.keep { border-left-color: var(--good); }
.rec.downgrade { border-left-color: var(--good); }
.rec.upgrade { border-left-color: var(--warn); }
.rec.review { border-left-color: var(--warn); }
.rec b { color: var(--accent); }
.rec.upgrade b, .rec.review b { color: var(--warn); }
.rec.downgrade b, .rec.keep b { color: var(--good); }
.section-note { color: var(--muted); margin: -8px 0 12px;
                font-size: 13px; }
ul.sugg { padding-left: 18px; }
ul.sugg li { margin: 6px 0; }
.disclaimer { color: var(--muted); font-size: 12px; margin-top: 24px; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 760px) { .split { grid-template-columns: 1fr; } }
"""


def _ht(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return datetime.fromisoformat(iso).astimezone().strftime(
            "%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _bar_row(label: str, value: int, total: int, value_str: str) -> str:
    pct = (value / total * 100) if total else 0
    return (
        f'<div class="row"><div class="name">{_ht(label)}</div>'
        f'<div class="bar"><i style="width:{pct:.1f}%"></i></div>'
        f'<div class="val big">{_ht(value_str)}</div></div>'
    )


def render_html(report: dict) -> str:
    total = report["total"]
    win = report["window"]
    insights = report.get("insights") or {}
    suggestions = report.get("suggestions") or []

    title = (f"Token Review · {_fmt_time(win['start'])} → "
             f"{_fmt_time(win['end'])}")

    # Summary cards
    cards = []
    cards.append(
        ("Total tokens", _humanize_tokens(total["total_tokens"]), ""))
    cards.append(
        ("Sessions in window", str(report["sessions_count"]), ""))
    cards.append(
        ("Assistant turns", f"{total['assistant_turns']:,}", ""))
    cards.append(
        ("Estimated cost",
         f"${total['estimated_cost_usd']:.2f}", "approx, USD"))
    cards.append(
        ("Cache effective",
         f"{insights.get('cache_effective_pct','?')}%",
         "cache_read share of input-side"))
    cards.append(
        ("Opus share",
         f"{insights.get('opus_token_share_pct','?')}%",
         "of total token volume"))

    cards_html = "".join(
        f'<div class="card"><div class="k">{_ht(k)}</div>'
        f'<div class="v">{_ht(v)}</div>'
        f'{f"<div class=k style=margin-top:4px>{_ht(s)}</div>" if s else ""}'
        f'</div>'
        for k, v, s in cards
    )

    # By model
    total_tok = total["total_tokens"] or 1
    _model_rows = []
    for fam, info in report["by_model"].items():
        model_total = (info["input_t"] + info["output_t"] +
                       info["cache_read"] + info["cache_write"]) or 1
        in_pct  = info["input_t"]    / model_total * 100
        out_pct = info["output_t"]   / model_total * 100
        cr_pct  = info["cache_read"] / model_total * 100
        cw_pct  = info["cache_write"]/ model_total * 100
        label = fam + (" (upstream)" if info.get("is_upstream") else "")
        _model_rows.append(
            _bar_row(
                label, info["tokens"], total_tok,
                f"{_humanize_tokens(info['tokens'])} · ${info['cost']:.2f} · "
                f"{info['sessions']}s",
            ) +
            f'<div class="meta" style="padding:2px 0 8px 0">'
            f'in {_humanize_tokens(info["input_t"])} ({in_pct:.0f}%) · '
            f'out {_humanize_tokens(info["output_t"])} ({out_pct:.0f}%) · '
            f'cache_read {_humanize_tokens(info["cache_read"])} ({cr_pct:.0f}%) · '
            f'cache_write {_humanize_tokens(info["cache_write"])} ({cw_pct:.0f}%)'
            f'</div>'
        )
    model_rows = "".join(_model_rows)

    # Upstream-provider dispatch (sub-agent sessions, non-Anthropic)
    dispatch = report.get("upstream_dispatch") or {}
    upstream_rows = []
    for fam, info in dispatch.items():
        proj_bits = ", ".join(
            f"{_ht(proj)} ×{v['sessions']}"
            for proj, v in list(info["by_project"].items())[:8]
        )
        upstream_rows.append(
            f'<div class="row">'
            f'<div class="name">{_ht(fam)}</div>'
            f'<div class="bar"></div>'
            f'<div class="val big">'
            f'{info["total_sessions"]} sessions · '
            f'{_ht(_humanize_tokens(info["total_tokens"]))}</div>'
            f'</div>'
            f'<div class="meta" style="padding:2px 0 8px 0">'
            f'from {proj_bits}</div>'
        )

    # By project
    proj_rows = "".join(
        _bar_row(
            proj, info["tokens"], total_tok,
            f"{_humanize_tokens(info['tokens'])} · ${info['cost']:.2f} · "
            f"{info['sessions']}s",
        )
        for proj, info in report["by_project"].items()
    )

    # By hour
    by_hour = report["by_hour"]
    mx_hour = max(by_hour) or 1
    hour_rows = []
    for h, v in enumerate(by_hour):
        pct = v / mx_hour * 100 if v else 0
        hour_rows.append(
            f'<div class="hours">'
            f'<div class="label">{h:02d}h</div>'
            f'<div class="bar"><i style="width:{pct:.1f}%"></i></div>'
            f'<div class="val big">{_ht(_humanize_tokens(v))}</div>'
            f'</div>'
        )

    # Top sessions
    top_html = []
    for ts in report["top_sessions"]:
        models_tags = "".join(
            f'<span class="tag {fam}">{_ht(fam)} ×{_ht(n)}</span>'
            for fam, n in (ts.get("models") or {}).items()
        )
        tools_tags = "".join(
            f'<span class="tag">{_ht(t)} ×{_ht(c)}</span>'
            for t, c in (ts.get("top_tools") or [])
        )
        share_tag = ""
        if ts.get("tokens_share_pct") is not None:
            share_tag = (
                f'<span class="tag share">'
                f'{ts["tokens_share_pct"]}% of window · '
                f'{ts.get("cost_share_pct", 0)}% of cost'
                f'</span>'
            )
        msg = ts.get("first_user_msg") or "(no user prompt captured)"
        if len(msg) > 400:
            msg = msg[:397] + "..."
        breakdown = (
            f"input {_humanize_tokens(ts['input_tokens'])} · "
            f"output {_humanize_tokens(ts['output_tokens'])} · "
            f"cache_read {_humanize_tokens(ts['cache_read_tokens'])} · "
            f"cache_write {_humanize_tokens(ts['cache_write_tokens'])}"
        )
        rec = ts.get("recommendation") or {}
        rec_html = ""
        if rec:
            verdict = rec.get("verdict", "keep")
            rec_model = rec.get("model", ts.get("primary_model", ""))
            if verdict == "keep":
                head = f"keep <b>{_ht(rec_model)}</b>"
            else:
                head = (f"<b>{_ht(rec_model)}</b> "
                        f"({_ht(verdict)} from "
                        f"<i>{_ht(ts['primary_model'])}</i>)")
            rec_html = (
                f'<div class="rec {_ht(verdict)}">'
                f'<b>Recommended model</b> · {head} — '
                f'{_ht(rec.get("reason", ""))}'
                f'</div>'
            )
        top_html.append(
            f'<div class="session">'
            f'<div class="head">'
            f'<span class="rank">#{_ht(ts["rank"])}</span>'
            f'<span class="sid">{_ht(ts["session_short"])}</span>'
            f'<span><b>{_ht(_humanize_tokens(ts["tokens"]))}</b> tokens · '
            f'<b>${ts["cost_usd"]:.2f}</b></span>'
            f'<span class="meta">{_ht(ts["assistant_turns"])} turns · '
            f'{_ht(ts["duration_min"])} min · started '
            f'{_ht(_fmt_time(ts["started_at"]))}</span>'
            f'</div>'
            f'<div class="meta">'
            f'<code>{_ht(ts["project_path"] or ts["project"])}</code></div>'
            f'<div class="tags">{share_tag}{models_tags}{tools_tags}</div>'
            f'<div class="meta" style="margin-top:6px">{_ht(breakdown)}</div>'
            f'<div class="task">{_ht(msg)}</div>'
            f'{rec_html}'
            f'</div>'
        )

    suggestions_html = (
        "<ul class=sugg>" +
        "".join(f"<li>{_ht(s)}</li>" for s in suggestions) +
        "</ul>"
        if suggestions
        else '<p class="sub">Nothing flagged. Usage looks well-shaped.</p>'
    )

    # Header above the Top sessions list — shows what % of the window's
    # tokens and cost the top-N batch accounts for. Precomputed for
    # readability instead of an f-string IIFE.
    top_share = report.get("top_share") or {}
    if top_share.get("top_n"):
        top_sessions_header = (
            f'<h2>Top sessions (top {top_share["top_n"]})</h2>'
            f'<p class="section-note">'
            f'Top {top_share["top_n"]} = '
            f'<b>{top_share["tokens_share_pct"]}%</b> of token volume · '
            f'<b>{top_share["cost_share_pct"]}%</b> of estimated cost '
            f'({_humanize_tokens(top_share["tokens"])} tokens · '
            f'${top_share["cost_usd"]:.2f})'
            f'</p>'
        )
    else:
        top_sessions_header = '<h2>Top sessions</h2>'

    body = f"""
<h1>{_ht(title)}</h1>
<div class="sub">
  Generated {_ht(_fmt_time(datetime.utcnow().isoformat() + "Z"))} ·
  Window {_ht(_fmt_time(win['start']))} → {_ht(_fmt_time(win['end']))}
</div>

<div class="grid cards">{cards_html}</div>

<div class="split">
  <div>
    <h2>By model</h2>
    {model_rows or "<p class=sub>no data</p>"}
  </div>
  <div>
    <h2>By project</h2>
    {proj_rows or "<p class=sub>no data</p>"}
  </div>
</div>

<h2>Token volume by hour (local)</h2>
{''.join(hour_rows)}

{"<h2>Upstream-provider dispatch</h2>"
 + "<p class=sub>Sub-agent sessions on non-Anthropic providers "
   "(cc-minimax, cc-switch). Billed by the upstream provider, not "
   "Anthropic — token volume tracked, cost $0.</p>"
 + "".join(upstream_rows)
 if upstream_rows else ""}

{top_sessions_header}
{''.join(top_html) or "<p class=sub>no sessions in window</p>"}

<h2>Optimization suggestions</h2>
{suggestions_html}

<p class="disclaimer">
  Pricing constants are approximations of Anthropic's public list prices
  and may be out of date. The dollar figures are useful for relative
  comparison between sessions, models, and projects — not for billing
  reconciliation. See the Anthropic console for authoritative numbers.
</p>
"""

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_ht(title)}</title>"
        f"<style>{CSS}</style></head><body>{body}"
        f"<script>window._report = {json.dumps(report, default=str)};</script>"
        "</body></html>"
    )


if __name__ == "__main__":
    import sys
    # Allow standalone use: pipe analyze.py --json into this.
    data = json.loads(sys.stdin.read())
    sys.stdout.write(render_html(data))
