---
name: token-review
description: Review Claude Code token consumption across recent sessions — totals, distribution (by model / project / hour), top-N sessions with task fingerprints, and concrete optimization suggestions. Use whenever the user wants to look back at their token usage, cost, or session shape over a recent time window. Trigger on phrases like "/token-review", "看看我最近 token 消耗", "review my recent token usage", "过去 24 小时哪些 session 最烧 token", "分析下我的 token 分布", "哪些 session 最贵", "我的 cache 命中率怎么样", "show top consuming sessions", "analyze claude code cost". Default window is 24h; user can pass 7d, 2w, 90m, etc. Strongly prefer this skill over hand-rolling a one-off analysis when the question is about historical session token usage.
---

# Token Review

Analyze recent Claude Code session logs from `~/.claude/projects/**/*.jsonl`
to give the user a clear picture of where their tokens go and how to use
fewer of them.

## When to use

Trigger on any question about historical token usage, cost, cache hit
rate, or which sessions/projects were the most expensive. The user may
phrase it many ways — "我的 token 都花哪了", "看看 cache 命中率", "过
去一周哪些任务最烧 token", "give me a usage report" — all of these are
this skill.

## How to invoke

Run the analyzer with the user's time window. Default to `24h` if they
didn't specify one. The window flag accepts `Nh`, `Nd`, `Nw`, `Nm` (e.g.
`24h`, `7d`, `2w`, `90m`).

```bash
python3 ~/.claude/skills/token-review/scripts/analyze.py <window>
```

**Default behavior**: prints the full markdown report to stdout AND
writes a self-contained HTML version to `/tmp/token-review-<window>.html`
and opens it in the browser. The user gets both views at once — the
in-chat markdown for quick reading, the HTML for rich visual scanning.

Useful flags:

- `--project current` — restrict to sessions whose `cwd` starts with the
  current working directory. Use this when the user says "for this
  project" or similar.
- `--project /path/to/project` — restrict to a specific path
  prefix.
- `--top N` — change the size of the top-sessions list (default 10).
- `--html PATH` — override the HTML output path (still written and
  opened by default; this just changes where the file goes).
- `--no-html` — skip writing/opening the HTML (markdown-only). Pass
  this when the user explicitly asks for "just the text" or you're
  running in a headless/CI context.
- `--no-open` — write the HTML report but don't open the browser.
  Useful when the user only wants the saved artifact.
- `--json` — emit raw JSON only; suppresses markdown and HTML.
  Useful for follow-up scripting; don't use by default.

The script is fast (<1s for 24h windows even with thousands of session
files) because it uses file mtime as a cheap pre-filter. Don't try to
re-implement this logic inline.

## How to present results

The script's markdown output is already structured for direct display.
Default behavior: stream it back to the user roughly as-is, then add a
short closing paragraph (2–3 sentences) that highlights the **single
most actionable** finding.

When the report is very large (e.g., user asked for `30d`):

- Show the **Summary**, **Distribution by model**, **Top sessions**,
  and **Optimization suggestions** sections in full.
- Truncate the per-hour bar chart and per-project list if they're long;
  mention that the HTML version (`--html`) has the full breakdown.

The HTML report is generated and opened automatically — no need to
suggest it as a follow-up. If the user wants to share it, just point
them at the path printed on stderr.

## Reading the output

Quick interpretation guide for the user-facing sections, so you can
answer follow-up questions without re-running:

- **Cache effective rate** = `cache_read / (input + cache_read +
  cache_write)`. Healthy is 70%+. Below 50% means the cached prefix
  is being invalidated too often (frequent `/clear`, cwd switches,
  edits to early files).
- **Cache-write share** > 25% with low subsequent reads = paying for
  caches that don't pay back (especially `ephemeral_1h`).
- **Opus token share** is what fraction of token volume Opus consumed.
  Cost-wise Opus is ~5× Sonnet at input and ~5× at output, so even a
  modest share can dominate spend.
- **Top-N concentration** — the line under the *Top sessions* heading
  reports how much of the window's tokens *and* cost the top-N batch
  accounts for. A small N driving a large share (e.g. top 10 = 70%
  of cost) means optimization effort should land on those sessions
  specifically; a flat distribution means the issue is systemic.
- **Per-session `% of window`** appears next to each top session's
  cost — a single session at >15% is unusual and worth inspecting
  before all others.
- **Recommended model** is a per-session heuristic suggestion (keep /
  downgrade / upgrade / review) based on the first user prompt, turn
  count, total tokens, and the input/output ratio. Treat it as a
  prompt to think, not a directive — e.g. "review" means Opus session
  was long enough that some phases may have been mechanical and could
  be split. Upstream (non-Anthropic) sessions are always "keep"
  because their Anthropic-side cost is already $0.
- **Short high-token sessions** = ≤3 assistant turns but >200k tokens.
  Usually means a huge paste or broad file read happened in the first
  prompt — a strong signal to use targeted reads (`rg`, line ranges).
- **Input/output ratio** in a per-session note tells you reading-heavy
  vs producing-heavy sessions. >50× is almost always a sign to prune
  attached context.

## Pricing notes

The dollar figures use approximate Anthropic list prices (Opus 4 ≈
$15/$75/M, Sonnet 4 ≈ $3/$15/M, Haiku 4.5 ≈ $1/$5/M, with cache reads
at ~10% and 5-minute cache writes at ~125% of input). They're meant
for **relative comparison** between sessions/models/projects, not for
billing reconciliation. If the user asks "is this what I'll be
billed?" — clarify that and point at the actual Anthropic console for
the authoritative number.

## What the script does not cover (by design)

- It only reads local session logs. If the user has used Claude on
  another machine or via API directly, those aren't here.
- It doesn't deduplicate forked sessions or sidechains specifically;
  each `sessionId` is treated as a unit.
- Task-type fingerprints come from the first cleaned user prompt; if
  the user opened the session with a slash command and no prose, the
  fingerprint may be terse. That's expected.

## Files

- `scripts/analyze.py` — the analyzer. Stdlib only.
- `scripts/render_html.py` — HTML renderer invoked by `--html`.
