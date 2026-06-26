#!/usr/bin/env python3
"""Token Usage Review — analyze Claude Code session logs.

Scans ~/.claude/projects/**/*.jsonl, filters by a time window, and reports:
  - aggregate token usage (input/output/cache_creation/cache_read)
  - estimated cost (USD) using approximate Anthropic pricing
  - distribution by model, project, hour-of-day
  - top-N sessions with heuristic task-type fingerprint
  - patterns and concrete optimization suggestions

Outputs markdown to stdout by default. Use --json for machine output,
or --html PATH to dump a self-contained HTML report (renderer is a sibling
script).

Pricing constants below are approximations of Anthropic's public list
prices and may be out of date — they're only used to give a rough sense
of cost share between models. Treat the $ figures as comparative, not
authoritative.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
STATS_FILE = CLAUDE_HOME / ".session-stats.json"

# Approximate USD per 1M tokens. Cache write defaults to 5m ephemeral;
# 1h ephemeral is priced separately because Anthropic charges ~2x for it.
#
# Only Anthropic families have priced entries here. Anything else (the
# upstream-provider case — cc-minimax, cc-switch routes to gpt/gemini/
# glm/kimi/etc.) is detected dynamically from the model id and is given
# zero Anthropic cost via `ZERO_PRICING`, since the real billing happens
# at the upstream provider. We still count token volume so the user can
# see where work is actually happening.
PRICING = {
    "opus": {
        "input": 15.0, "output": 75.0,
        "cache_read": 1.5, "cache_write_5m": 18.75, "cache_write_1h": 30.0,
    },
    "sonnet": {
        "input": 3.0, "output": 15.0,
        "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0,
    },
    "haiku": {
        "input": 1.0, "output": 5.0,
        "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0,
    },
}
ZERO_PRICING = {
    "input": 0.0, "output": 0.0,
    "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0,
}
ANTHROPIC_FAMILIES = frozenset(PRICING.keys())
DEFAULT_FAMILY = "unknown"  # used when a message has no model id at all


def is_upstream_family(family: str) -> bool:
    """True if this family is served by a non-Anthropic upstream provider
    (cc-minimax, cc-switch route, etc.). Used to flag rows that don't
    contribute to the Anthropic bill."""
    return family not in ANTHROPIC_FAMILIES


# ----- utilities -------------------------------------------------------

def parse_duration(spec: str) -> timedelta:
    """Parse '24h', '7d', '2w', '30m', '90s'. Bare integer → hours."""
    s = (spec or "").strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhdw])?$", s)
    if not m:
        raise ValueError(f"invalid time spec: {spec!r}")
    n = float(m.group(1))
    unit = m.group(2) or "h"
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]


def parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def detect_family(model_id: str | None) -> str:
    """Classify a model id into a family label.

    Anthropic models keep their canonical family (opus/sonnet/haiku) so
    pricing applies cleanly. For anything else — cc-minimax, cc-switch
    routes to gemini/glm/kimi/gpt/etc. — we don't hardcode a list. We
    take the leading provider-ish segment of the model id and use that
    as the family. Pricing for unknown families falls through to
    ZERO_PRICING in `cost_for`, so a brand-new upstream provider gets
    correctly tracked as zero-Anthropic-cost without code changes.

    Examples of dynamic extraction:
      MiniMax-M2.7-highspeed → minimax
      gpt-5-4                → gpt
      gemini-2.5-flash       → gemini
      glm-4.6                → glm
      kimi-k2-instruct       → kimi
    """
    if not model_id:
        return DEFAULT_FAMILY
    m = model_id.lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    # Strip a leading "claude-" if present (some routers report
    # "claude-glm-4.6" style ids) so we don't all-bucket them as
    # "claude". Take the first segment after that.
    if m.startswith("claude-"):
        m = m[len("claude-"):]
    head = re.split(r"[-_/\s.]", m, maxsplit=1)[0]
    return head or DEFAULT_FAMILY


def cost_for(family: str, *, input_t: int, output_t: int,
             cache_read: int, cache_5m: int, cache_1h: int) -> float:
    p = PRICING.get(family, ZERO_PRICING)
    return (
        input_t * p["input"] +
        output_t * p["output"] +
        cache_read * p["cache_read"] +
        cache_5m * p["cache_write_5m"] +
        cache_1h * p["cache_write_1h"]
    ) / 1_000_000.0


def short_session(sid: str) -> str:
    return sid[:8] if sid else "????????"


def project_label(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    # Use last 2 path segments for a recognizable but compact label.
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else cwd


def humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


# ----- first-user-message extraction ----------------------------------

# Lines/blocks that should be stripped from user content because they're
# system reminders, hook injections, slash-command metadata, or pasted
# tool output rather than the user's actual prompt.
_NOISE_PATTERNS = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<command-message>.*?</command-message>", re.DOTALL),
    re.compile(r"<command-name>.*?</command-name>", re.DOTALL),
    re.compile(r"<command-args>.*?</command-args>", re.DOTALL),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL),
    re.compile(r"<user-prompt-submit-hook>.*?</user-prompt-submit-hook>",
               re.DOTALL),
    re.compile(r"<bash-input>.*?</bash-input>", re.DOTALL),
    re.compile(r"<bash-stdout>.*?</bash-stdout>", re.DOTALL),
    re.compile(r"<bash-stderr>.*?</bash-stderr>", re.DOTALL),
]


def clean_user_text(text: str) -> str:
    if not text:
        return ""
    for pat in _NOISE_PATTERNS:
        text = pat.sub("", text)
    # Drop tag-only lines and excess whitespace.
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<") and stripped.endswith(">"):
            continue
        if stripped.startswith("Caveat:"):
            continue
        lines.append(stripped)
    return " ".join(lines).strip()


def extract_user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return clean_user_text(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return clean_user_text(" ".join(parts))
    return ""


# ----- model recommendation heuristics --------------------------------

# Strong-signal keyword lists for inferring task type from the first
# cleaned user prompt. Bilingual EN+ZH because users prompt in either
# language. We aim for *strong* signals only — false positives turn
# the recommendation into noise.
_ARCH_SIGNAL_RE = re.compile(
    r"architect|design|refactor|concurren|security|audit|"
    r"deep\s+(?:dive|debug)|root\s+cause|performance|optimiz|"
    r"investigate|profile|memory\s+leak|race\s+condition|"
    r"deadlock|crash|"
    r"架构|设计|重构|并发|安全|审查|深入|调试|性能|排查|优化|崩溃|死锁|内存泄漏",
    re.IGNORECASE,
)
_MECHANICAL_SIGNAL_RE = re.compile(
    r"format|rename|translate|typo|boilerplate|stub|lint|"
    r"reformat|prettify|docstring|spelling|"
    r"格式化|重命名|翻译|样板|拼写|样式|文档化",
    re.IGNORECASE,
)


def recommend_model(session: "SessionAgg") -> dict:
    """Pick a more-suitable model family for a session.

    Heuristic only — looks at the primary model, the first user
    message (cleaned of hook/reminder noise), turn count, total
    tokens, and the input/output ratio. Upstream (non-Anthropic)
    sessions are left alone because the Anthropic-side cost is
    already $0; routing them elsewhere wouldn't reduce it.

    Returns a dict with:
      verdict: "keep" | "downgrade" | "upgrade" | "review"
      model:   recommended family
      reason:  one-line explanation suitable for direct display
    """
    current = session.primary_family
    msg = session.first_user_msg or ""
    turns = session.assistant_turns
    tokens = session.total_tokens
    input_t = session.input_t
    output_t = session.output_t

    if is_upstream_family(current):
        return {
            "verdict": "keep",
            "model": current,
            "reason": ("Already on upstream provider — Anthropic-side "
                       "cost is $0, no model change needed."),
        }

    arch_signal = bool(_ARCH_SIGNAL_RE.search(msg))
    mech_signal = bool(_MECHANICAL_SIGNAL_RE.search(msg))
    very_short = turns <= 3
    heavy_read = output_t and input_t / max(output_t, 1) > 50

    if current == "opus":
        if mech_signal and not arch_signal:
            return {
                "verdict": "downgrade", "model": "haiku",
                "reason": ("Task looks mechanical (format / rename / "
                           "translate / docs). Haiku is ~15× cheaper "
                           "and accurate enough."),
            }
        if very_short and tokens < 500_000:
            return {
                "verdict": "downgrade", "model": "sonnet",
                "reason": (f"Only {turns} turns, "
                           f"{humanize_tokens(tokens)} tokens — scope "
                           "doesn't justify Opus; Sonnet is ~5× cheaper."),
            }
        if arch_signal:
            return {
                "verdict": "keep", "model": "opus",
                "reason": ("Architecture / deep-debug signal in task "
                           "— Opus is appropriate."),
            }
        if tokens < 1_000_000 and not heavy_read:
            return {
                "verdict": "downgrade", "model": "sonnet",
                "reason": ("No architecture / debugging signal "
                           "detected; Sonnet matches normal "
                           "implementation work at ~1/5 the cost."),
            }
        return {
            "verdict": "review", "model": "opus",
            "reason": ("Long Opus session — recheck the task type; "
                       "if any phase was mechanical, split it into a "
                       "Sonnet/Haiku sub-session."),
        }

    if current == "sonnet":
        if arch_signal and tokens > 500_000:
            return {
                "verdict": "upgrade", "model": "opus",
                "reason": ("Architecture / debugging signal on a "
                           "token-heavy session — Opus often gives "
                           "better answers per token here."),
            }
        if mech_signal and very_short:
            return {
                "verdict": "downgrade", "model": "haiku",
                "reason": ("Short mechanical task — Haiku is ~3× "
                           "cheaper for this shape."),
            }
        return {
            "verdict": "keep", "model": "sonnet",
            "reason": "Sonnet matches the workload shape.",
        }

    if current == "haiku":
        if arch_signal or tokens > 500_000:
            return {
                "verdict": "upgrade", "model": "sonnet",
                "reason": ("Token-heavy or design-heavy session — "
                           "Sonnet typically delivers materially "
                           "better results."),
            }
        return {
            "verdict": "keep", "model": "haiku",
            "reason": "Lightweight task — Haiku is a good fit.",
        }

    return {
        "verdict": "keep", "model": current,
        "reason": "No clear signal — leave model as-is.",
    }


# ----- session aggregation --------------------------------------------

@dataclass
class SessionAgg:
    session_id: str
    project_path: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    models: Counter = field(default_factory=Counter)  # family -> turns
    input_t: int = 0
    output_t: int = 0
    cache_read: int = 0
    cache_5m: int = 0
    cache_1h: int = 0
    assistant_turns: int = 0
    first_user_msg: str = ""
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (self.input_t + self.output_t +
                self.cache_read + self.cache_5m + self.cache_1h)

    @property
    def primary_family(self) -> str:
        if not self.models:
            return DEFAULT_FAMILY
        return self.models.most_common(1)[0][0]

    @property
    def duration_min(self) -> float:
        if not (self.started_at and self.ended_at):
            return 0.0
        return (self.ended_at - self.started_at).total_seconds() / 60.0


def scan_jsonl(path: Path, window_start: datetime,
               window_end: datetime) -> SessionAgg | None:
    """Stream-read a session file, return SessionAgg if any message is in
    the window. Returns None if nothing in the file falls inside."""
    agg: SessionAgg | None = None
    # Deduplicate same API response recorded multiple times in jsonl.
    # Claude Code may write the same (messageId, requestId) more than once
    # (retries, streaming chunks, etc.); ccusage skips these. Without this
    # guard we double-count and over-report by ~2x.
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_ts(rec.get("timestamp", ""))
                if ts is None:
                    continue
                rec_type = rec.get("type")
                # session_id can live at top level or inside the record
                sid = rec.get("sessionId") or path.stem

                if rec_type == "user":
                    if agg is None:
                        # We may not yet have any in-window data; capture
                        # cwd/first-msg lazily once we hit an in-window
                        # assistant turn.
                        pending_first = agg
                    if not (agg and agg.first_user_msg):
                        msg = rec.get("message") or {}
                        text = extract_user_text(msg)
                        if text:
                            if agg is None:
                                # stash pending first msg until we open an agg
                                _pending = (sid, rec.get("cwd"), text)
                                # use a small closure-like: store on function
                                # via attribute? Simpler: set later when agg
                                # is created.
                                scan_jsonl._pending = _pending  # type: ignore[attr-defined]
                            else:
                                if not agg.first_user_msg:
                                    agg.first_user_msg = text[:240]
                    continue

                if rec_type != "assistant":
                    continue

                msg = rec.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue

                if ts < window_start or ts > window_end:
                    continue

                if agg is None:
                    agg = SessionAgg(session_id=sid)
                    agg.project_path = rec.get("cwd") or ""
                    pending = getattr(scan_jsonl, "_pending", None)
                    if pending and pending[0] == sid:
                        if not agg.project_path:
                            agg.project_path = pending[1] or ""
                        agg.first_user_msg = (pending[2] or "")[:240]
                    if hasattr(scan_jsonl, "_pending"):
                        delattr(scan_jsonl, "_pending")

                # Skip duplicate API responses (same messageId + requestId).
                # Records without either key still pass through (e.g. upstream
                # providers that don't emit requestId).
                _mid = msg.get("id")
                _rid = rec.get("requestId")
                if _mid is not None and _rid is not None:
                    _key = f"{_mid}:{_rid}"
                    if _key in seen:
                        continue
                    seen.add(_key)

                agg.assistant_turns += 1
                family = detect_family(msg.get("model"))
                agg.models[family] += 1

                in_t = int(usage.get("input_tokens") or 0)
                out_t = int(usage.get("output_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                cw_total = int(usage.get("cache_creation_input_tokens") or 0)
                cw_detail = usage.get("cache_creation") or {}
                cw_5m = int(cw_detail.get("ephemeral_5m_input_tokens") or 0)
                cw_1h = int(cw_detail.get("ephemeral_1h_input_tokens") or 0)
                # Fall back to total if the breakdown isn't present.
                if cw_5m == 0 and cw_1h == 0 and cw_total:
                    cw_5m = cw_total

                agg.input_t += in_t
                agg.output_t += out_t
                agg.cache_read += cr
                agg.cache_5m += cw_5m
                agg.cache_1h += cw_1h
                agg.cost_usd += cost_for(
                    family,
                    input_t=in_t, output_t=out_t,
                    cache_read=cr, cache_5m=cw_5m, cache_1h=cw_1h,
                )

                if agg.started_at is None or ts < agg.started_at:
                    agg.started_at = ts
                if agg.ended_at is None or ts > agg.ended_at:
                    agg.ended_at = ts
    except (OSError, PermissionError):
        return None
    finally:
        if hasattr(scan_jsonl, "_pending"):
            delattr(scan_jsonl, "_pending")
    return agg


def collect_sessions(window_start: datetime, window_end: datetime,
                     project_filter: str | None) -> list[SessionAgg]:
    """Walk every project dir; collect sessions that have activity in the
    window. Uses mtime as a cheap prefilter."""
    if not PROJECTS_DIR.exists():
        return []

    # mtime is naive local; convert window_start to a comparable epoch
    win_start_epoch = window_start.timestamp()
    sessions: list[SessionAgg] = []

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                st = jsonl.stat()
            except OSError:
                continue
            # If the file was last modified before the window even started,
            # it can't contain in-window data. Skip.
            if st.st_mtime < win_start_epoch:
                continue
            agg = scan_jsonl(jsonl, window_start, window_end)
            if agg is None:
                continue
            if agg.assistant_turns == 0:
                continue
            if project_filter:
                if not (agg.project_path or "").startswith(project_filter):
                    continue
            sessions.append(agg)
    return sessions


# ----- enrichment: tool counts from .session-stats.json ----------------

def load_tool_counts() -> dict[str, dict]:
    if not STATS_FILE.exists():
        return {}
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return (data or {}).get("sessions") or {}


def top_tools(stats: dict, limit: int = 3) -> list[tuple[str, int]]:
    counts = (stats or {}).get("tool_counts") or {}
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


# ----- reporting -------------------------------------------------------

def format_task_type(s: SessionAgg, tool_stats: dict | None) -> str:
    proj = project_label(s.project_path)
    msg = (s.first_user_msg or "").replace("\n", " ").strip()
    if len(msg) > 110:
        msg = msg[:107] + "..."
    if not msg:
        msg = "(no user prompt captured)"
    bits = [f"[{proj}] {msg}"]
    tools = top_tools(tool_stats or {})
    if tools:
        bits.append("tools: " + ", ".join(f"{t}×{c}" for t, c in tools))
    return " · ".join(bits)


def aggregate(sessions: list[SessionAgg], window_start: datetime,
              window_end: datetime, top_n: int,
              tool_counts_by_session: dict[str, dict]) -> dict:
    sessions = sorted(sessions, key=lambda s: s.input_t + s.cache_5m + s.cache_1h, reverse=True)

    total = SessionAgg(session_id="*")
    by_model: dict[str, dict] = defaultdict(
        lambda: {
            "tokens": 0, "cost": 0.0, "sessions": 0,
            "input_t": 0, "output_t": 0,
            "cache_read": 0, "cache_write": 0,
        })
    by_project: dict[str, dict] = defaultdict(
        lambda: {"tokens": 0, "cost": 0.0, "sessions": 0})
    by_hour: list[int] = [0] * 24  # tokens
    # upstream_dispatch[family][project] = {sessions, tokens}
    # Tracks non-Anthropic (cc-minimax, etc.) sub-agent invocations and
    # which project's cwd they were dispatched from. Lets us spot
    # patterns like "my-project dispatched 18 minimax sessions".
    upstream_dispatch: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"sessions": 0, "tokens": 0}))

    for s in sessions:
        total.input_t += s.input_t
        total.output_t += s.output_t
        total.cache_read += s.cache_read
        total.cache_5m += s.cache_5m
        total.cache_1h += s.cache_1h
        total.cost_usd += s.cost_usd
        total.assistant_turns += s.assistant_turns

        fam = s.primary_family
        by_model[fam]["tokens"] += s.total_tokens
        by_model[fam]["cost"] += s.cost_usd
        by_model[fam]["sessions"] += 1
        by_model[fam]["input_t"] += s.input_t
        by_model[fam]["output_t"] += s.output_t
        by_model[fam]["cache_read"] += s.cache_read
        by_model[fam]["cache_write"] += s.cache_5m + s.cache_1h

        proj = project_label(s.project_path)
        by_project[proj]["tokens"] += s.total_tokens
        by_project[proj]["cost"] += s.cost_usd
        by_project[proj]["sessions"] += 1

        if is_upstream_family(fam):
            upstream_dispatch[fam][proj]["sessions"] += 1
            upstream_dispatch[fam][proj]["tokens"] += s.total_tokens

        if s.started_at:
            by_hour[s.started_at.astimezone().hour] += s.total_tokens

    top = sessions[:top_n]
    total_tok_for_share = total.total_tokens or 1
    total_cost_for_share = total.cost_usd or 1.0
    top_share_tokens = sum(s.total_tokens for s in top)
    top_share_cost = sum(s.cost_usd for s in top)
    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "sessions_count": len(sessions),
        "total": {
            "input_tokens": total.input_t,
            "output_tokens": total.output_t,
            "cache_read_tokens": total.cache_read,
            "cache_write_5m_tokens": total.cache_5m,
            "cache_write_1h_tokens": total.cache_1h,
            "total_tokens": total.total_tokens,
            "assistant_turns": total.assistant_turns,
            "estimated_cost_usd": round(total.cost_usd, 2),
        },
        "by_model": {
            k: {**v, "cost": round(v["cost"], 2),
                "is_upstream": is_upstream_family(k)}
            for k, v in sorted(by_model.items(),
                               key=lambda kv: kv[1]["tokens"], reverse=True)
        },
        "by_project": {
            k: {**v, "cost": round(v["cost"], 2)}
            for k, v in sorted(by_project.items(),
                               key=lambda kv: kv[1]["tokens"],
                               reverse=True)[:15]
        },
        "by_hour": by_hour,
        "upstream_dispatch": {
            fam: {
                "total_sessions": sum(v["sessions"] for v in projs.values()),
                "total_tokens": sum(v["tokens"] for v in projs.values()),
                "by_project": dict(sorted(
                    projs.items(),
                    key=lambda kv: kv[1]["sessions"], reverse=True)),
            }
            for fam, projs in sorted(
                upstream_dispatch.items(),
                key=lambda kv: sum(v["sessions"] for v in kv[1].values()),
                reverse=True)
        },
        "top_share": {
            "top_n": len(top),
            "tokens": top_share_tokens,
            "cost_usd": round(top_share_cost, 2),
            "tokens_share_pct": round(
                top_share_tokens / total_tok_for_share * 100, 1),
            "cost_share_pct": round(
                top_share_cost / total_cost_for_share * 100, 1),
        },
        "top_sessions": [
            {
                "rank": i + 1,
                "session_id": s.session_id,
                "session_short": short_session(s.session_id),
                "project": project_label(s.project_path),
                "project_path": s.project_path,
                "primary_model": s.primary_family,
                "is_upstream": is_upstream_family(s.primary_family),
                "models": dict(s.models),
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "duration_min": round(s.duration_min, 1),
                "assistant_turns": s.assistant_turns,
                "tokens": s.total_tokens,
                "tokens_share_pct": round(
                    s.total_tokens / total_tok_for_share * 100, 1),
                "cost_share_pct": round(
                    s.cost_usd / total_cost_for_share * 100, 1),
                "input_tokens": s.input_t,
                "output_tokens": s.output_t,
                "cache_read_tokens": s.cache_read,
                "cache_write_tokens": s.cache_5m + s.cache_1h,
                "cost_usd": round(s.cost_usd, 2),
                "task_type": format_task_type(
                    s, tool_counts_by_session.get(s.session_id)),
                "first_user_msg": s.first_user_msg,
                "top_tools": top_tools(
                    tool_counts_by_session.get(s.session_id) or {}),
                "recommendation": recommend_model(s),
            }
            for i, s in enumerate(top)
        ],
    }


# ----- insights & optimization suggestions ----------------------------

def compute_insights(report: dict, sessions: list[SessionAgg]) -> dict:
    total = report["total"]
    sum_input_like = (total["input_tokens"] +
                      total["cache_read_tokens"] +
                      total["cache_write_5m_tokens"] +
                      total["cache_write_1h_tokens"])
    cache_eff = (total["cache_read_tokens"] / sum_input_like * 100
                 if sum_input_like else 0.0)
    cache_write_share = ((total["cache_write_5m_tokens"] +
                          total["cache_write_1h_tokens"]) /
                         sum_input_like * 100 if sum_input_like else 0.0)
    opus_tokens = report["by_model"].get("opus", {}).get("tokens", 0)
    total_tok = total["total_tokens"] or 1
    opus_share = opus_tokens / total_tok * 100

    # peak hour
    by_hour = report["by_hour"]
    peak_hour = max(range(24), key=lambda h: by_hour[h]) if any(by_hour) else None

    # session-shape heuristics
    short_high = []  # short sessions burning lots of tokens
    long_running = []  # long sessions, candidates for /compact
    for s in sessions:
        if s.assistant_turns <= 3 and s.total_tokens > 200_000:
            short_high.append(s)
        if s.duration_min > 60 and s.total_tokens > 1_000_000:
            long_running.append(s)

    return {
        "cache_effective_pct": round(cache_eff, 1),
        "cache_write_share_pct": round(cache_write_share, 1),
        "opus_token_share_pct": round(opus_share, 1),
        "peak_hour": peak_hour,
        "short_high_token_sessions": len(short_high),
        "long_running_sessions": len(long_running),
    }


def build_suggestions(report: dict, insights: dict,
                      sessions: list[SessionAgg]) -> list[str]:
    out: list[str] = []
    eff = insights["cache_effective_pct"]
    cw = insights["cache_write_share_pct"]
    opus = insights["opus_token_share_pct"]

    if eff < 50 and report["total"]["total_tokens"] > 100_000:
        out.append(
            f"Cache effective rate is {eff}% — under 50% means most "
            "input tokens are being re-billed at full price. Common "
            "causes: frequent `/clear`, switching cwd often, or context "
            "edits that invalidate the prefix. Try staying in one "
            "session longer and avoid mid-session file rewrites that "
            "shift the cached prefix.")
    if cw > 25:
        out.append(
            f"{cw}% of input-side tokens are cache writes — you're paying "
            "premium for caches that may not be reused. If sessions are "
            "short, the 1h cache rarely pays back; consider whether the "
            "ephemeral_1h tier is worth it for these workloads.")
    if opus > 70:
        out.append(
            f"Opus drove {opus}% of token volume. For mechanical or "
            "well-scoped tasks (refactors, formatting, tests, doc edits) "
            "Sonnet or Haiku are typically 5–15× cheaper and good enough. "
            "Reserve Opus for architecture, deep debugging, security.")
    if insights["short_high_token_sessions"] >= 3:
        out.append(
            f"{insights['short_high_token_sessions']} sessions burned >200k "
            "tokens in ≤3 assistant turns — likely large file pastes or "
            "broad reads up front. Try targeted reads (`grep`/`rg`, "
            "line-range Reads) instead of dumping whole files.")
    if insights["long_running_sessions"] >= 2:
        out.append(
            f"{insights['long_running_sessions']} sessions ran >1h with >1M "
            "tokens — context-window pressure builds in long sessions. "
            "Consider `/compact` checkpoints or splitting milestones into "
            "fresh sessions to keep the cached prefix small.")

    # Per-top-session targeted notes
    for ts in report["top_sessions"]:
        notes = []
        tok = ts["tokens"] or 1
        cr_share = ts["cache_read_tokens"] / tok * 100
        cw_share = ts["cache_write_tokens"] / tok * 100
        if cr_share < 30 and tok > 200_000:
            notes.append(
                f"only {cr_share:.0f}% of tokens were cache reads → "
                "context was not reused well")
        if cw_share > 40:
            notes.append(
                f"{cw_share:.0f}% cache writes → context kept being "
                "invalidated mid-session")
        if ts["output_tokens"] and ts["input_tokens"] > 0:
            ratio = ts["input_tokens"] / max(ts["output_tokens"], 1)
            if ratio > 50:
                notes.append(
                    f"input/output ratio {ratio:.0f}× — heavy reading, "
                    "little producing; prune attached context")
        if notes:
            out.append(
                f"#{ts['rank']} {ts['session_short']} "
                f"({ts['project']}, {humanize_tokens(tok)} tokens): " +
                "; ".join(notes))
    return out


# ----- rendering -------------------------------------------------------

def hour_bar_chart(by_hour: list[int], width: int = 40) -> list[str]:
    mx = max(by_hour) or 1
    out = []
    for h, v in enumerate(by_hour):
        bar = "█" * int(round(v / mx * width)) if v else ""
        out.append(f"  {h:02d}h │ {bar} {humanize_tokens(v)}")
    return out


def render_markdown(report: dict, insights: dict,
                    suggestions: list[str]) -> str:
    total = report["total"]
    win = report["window"]
    start = parse_ts(win["start"])
    end = parse_ts(win["end"])
    span_str = (
        f"{start.astimezone().strftime('%Y-%m-%d %H:%M')} → "
        f"{end.astimezone().strftime('%Y-%m-%d %H:%M')}"
        if start and end else f"{win['start']} → {win['end']}"
    )
    lines: list[str] = []
    add = lines.append

    add(f"# Token Usage Review")
    add(f"**Window**: {span_str}  ")
    add(f"**Sessions active**: {report['sessions_count']}  ")
    add(f"**Assistant turns**: {total['assistant_turns']:,}  ")
    add(f"**Total tokens**: {humanize_tokens(total['total_tokens'])}  ")
    add(f"  · input {humanize_tokens(total['input_tokens'])}"
        f"  · output {humanize_tokens(total['output_tokens'])}"
        f"  · cache_read {humanize_tokens(total['cache_read_tokens'])}"
        f"  · cache_write {humanize_tokens(total['cache_write_5m_tokens'] + total['cache_write_1h_tokens'])}")
    add(f"**Estimated cost**: ~${total['estimated_cost_usd']:.2f} USD "
        "*(approximate)*")
    add("")

    add("## Distribution by model")
    if not report["by_model"]:
        add("_no data_")
    else:
        for fam, info in report["by_model"].items():
            share = (info["tokens"] / total["total_tokens"] * 100
                     if total["total_tokens"] else 0)
            tag = (" *(upstream — billed by provider, not Anthropic)*"
                   if info.get("is_upstream") else "")
            model_total = (info["input_t"] + info["output_t"] +
                           info["cache_read"] + info["cache_write"]) or 1
            in_pct = info["input_t"] / model_total * 100
            out_pct = info["output_t"] / model_total * 100
            cr_pct = info["cache_read"] / model_total * 100
            cw_pct = info["cache_write"] / model_total * 100
            add(f"- **{fam}** — {humanize_tokens(info['tokens'])} tokens "
                f"({share:.0f}%) · ~${info['cost']:.2f} · "
                f"{info['sessions']} sessions{tag}")
            add(f"  · in {humanize_tokens(info['input_t'])} ({in_pct:.0f}%)"
                f" · out {humanize_tokens(info['output_t'])} ({out_pct:.0f}%)"
                f" · cache_read {humanize_tokens(info['cache_read'])} ({cr_pct:.0f}%)"
                f" · cache_write {humanize_tokens(info['cache_write'])} ({cw_pct:.0f}%)")
    add("")

    add("## Distribution by project (top 15)")
    if not report["by_project"]:
        add("_no data_")
    else:
        for proj, info in report["by_project"].items():
            share = (info["tokens"] / total["total_tokens"] * 100
                     if total["total_tokens"] else 0)
            add(f"- `{proj}` — {humanize_tokens(info['tokens'])} "
                f"({share:.0f}%) · ~${info['cost']:.2f} · "
                f"{info['sessions']} sessions")
    add("")

    add("## Token volume by hour-of-day (local time)")
    add("```")
    for row in hour_bar_chart(report["by_hour"]):
        add(row)
    add("```")
    add("")

    dispatch = report.get("upstream_dispatch") or {}
    if dispatch:
        add("## Upstream-provider dispatch")
        add("_Sub-agent sessions on non-Anthropic providers (cc-minimax, "
            "cc-switch, etc.). These are billed by the upstream provider, "
            "not Anthropic — token volume tracked, cost shown as $0._")
        for fam, info in dispatch.items():
            add(f"- **{fam}** — {info['total_sessions']} sessions · "
                f"{humanize_tokens(info['total_tokens'])} tokens "
                "*(billed upstream)*")
            proj_bits = [
                f"{proj} ×{v['sessions']}"
                for proj, v in list(info["by_project"].items())[:6]
            ]
            if proj_bits:
                add(f"  · dispatched from: {', '.join(proj_bits)}")
        add("")

    top_share = report.get("top_share") or {}
    if top_share.get("top_n"):
        add(f"## Top sessions (top {top_share['top_n']})")
        add(f"_Top {top_share['top_n']} = "
            f"**{top_share['tokens_share_pct']}%** of token volume · "
            f"**{top_share['cost_share_pct']}%** of estimated cost "
            f"({humanize_tokens(top_share['tokens'])} tokens · "
            f"~${top_share['cost_usd']:.2f})_")
    else:
        add("## Top sessions")
    if not report["top_sessions"]:
        add("_no sessions in window_")
    else:
        for ts in report["top_sessions"]:
            started = ts["started_at"]
            try:
                started_h = (parse_ts(started).astimezone()
                             .strftime("%m-%d %H:%M")) if started else "?"
            except Exception:
                started_h = "?"
            up_tag = " · upstream" if ts.get("is_upstream") else ""
            share_str = (f" · {ts['tokens_share_pct']}% of window"
                         if ts.get("tokens_share_pct") else "")
            add(
                f"### #{ts['rank']} · `{ts['session_short']}` · "
                f"{humanize_tokens(ts['tokens'])} tokens · "
                f"~${ts['cost_usd']:.2f}{share_str}{up_tag}")
            add(f"- **Project**: `{ts['project_path'] or ts['project']}`")
            model_note = (" *(upstream — not billed by Anthropic)*"
                          if ts.get("is_upstream") else "")
            add(f"- **Model**: {ts['primary_model']}{model_note} "
                f"(turns={ts['assistant_turns']}, "
                f"dur={ts['duration_min']:.0f}min, started {started_h})")
            add(f"- **Breakdown**: "
                f"input {humanize_tokens(ts['input_tokens'])} · "
                f"output {humanize_tokens(ts['output_tokens'])} · "
                f"cache_read {humanize_tokens(ts['cache_read_tokens'])} · "
                f"cache_write {humanize_tokens(ts['cache_write_tokens'])}")
            tools_str = (", ".join(f"{t}×{c}" for t, c in ts["top_tools"])
                         if ts["top_tools"] else "(no tool stats)")
            add(f"- **Top tools**: {tools_str}")
            msg = ts["first_user_msg"] or "(no user prompt captured)"
            if len(msg) > 200:
                msg = msg[:197] + "..."
            add(f"- **Task**: {msg}")
            rec = ts.get("recommendation") or {}
            if rec:
                verdict = rec.get("verdict", "keep")
                rec_model = rec.get("model", ts["primary_model"])
                reason = rec.get("reason", "")
                if verdict == "keep":
                    rec_head = f"keep `{rec_model}`"
                else:
                    rec_head = (
                        f"`{rec_model}` ({verdict} from "
                        f"`{ts['primary_model']}`)")
                add(f"- **Recommended model**: {rec_head} — {reason}")
            add("")

    add("## Patterns & insights")
    add(f"- Cache effective rate: **{insights['cache_effective_pct']}%** "
        "(cache_read / all input-side tokens)")
    add(f"- Cache-write share: **{insights['cache_write_share_pct']}%** "
        "(paid for caching the prefix)")
    add(f"- Opus token share: **{insights['opus_token_share_pct']}%**")
    if insights["peak_hour"] is not None:
        add(f"- Peak hour: **{insights['peak_hour']:02d}:00** local time")
    add(f"- Short high-token sessions: "
        f"{insights['short_high_token_sessions']}")
    add(f"- Long-running token-heavy sessions: "
        f"{insights['long_running_sessions']}")
    add("")

    add("## Optimization suggestions")
    if not suggestions:
        add("- _Nothing flagged. Your usage looks well-shaped for this "
            "window._")
    else:
        for s in suggestions:
            add(f"- {s}")
    add("")
    add("> Pricing constants are approximate; treat cost figures as "
        "comparative, not authoritative.")

    return "\n".join(lines)


# ----- main ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review Claude Code session token usage.")
    parser.add_argument(
        "window", nargs="?", default="24h",
        help="Time window ending now. Examples: 24h, 7d, 2w, 90m. Default 24h.")
    parser.add_argument(
        "--project", default=None,
        help="Filter to sessions whose cwd starts with this path. "
             "Pass 'current' to use $PWD.")
    parser.add_argument(
        "--top", type=int, default=100,
        help="Number of top sessions to list (default 100).")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON only — suppresses markdown and HTML.")
    parser.add_argument(
        "--html", metavar="PATH", default=None,
        help="Override the HTML output path. By default the HTML is "
             "written to /tmp/token-review-<window>.html and opened "
             "in the browser alongside the markdown report.")
    parser.add_argument(
        "--no-html", action="store_true",
        help="Skip writing/opening the HTML report (markdown-only).")
    parser.add_argument(
        "--no-open", action="store_true",
        help="Write the HTML report but do not open it in the browser. "
             "Useful for headless environments.")
    args = parser.parse_args(argv)

    try:
        delta = parse_duration(args.window)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    window_end = datetime.now(timezone.utc)
    window_start = window_end - delta

    project_filter = args.project
    if project_filter == "current":
        project_filter = os.getcwd()

    sessions = collect_sessions(window_start, window_end, project_filter)
    tool_counts = load_tool_counts()
    report = aggregate(sessions, window_start, window_end,
                       top_n=args.top,
                       tool_counts_by_session=tool_counts)
    insights = compute_insights(report, sessions)
    report["insights"] = insights
    suggestions = build_suggestions(report, insights, sessions)
    report["suggestions"] = suggestions

    if args.json:
        # JSON mode is exclusive — caller wants raw data, not the rendered
        # views, and definitely not a browser popup.
        print(json.dumps(report, indent=2, default=str))
        return 0

    # Markdown always goes to stdout — the in-chat default. HTML is also
    # written and opened by default, so the user gets the rich view
    # without needing a separate command. `--no-html` skips HTML
    # entirely; `--no-open` writes the file but won't open the browser
    # (useful for headless / remote sessions).
    print(render_markdown(report, insights, suggestions))

    if not args.no_html:
        # Defer rendering to sibling script for separation of concerns.
        from render_html import render_html  # type: ignore
        if args.html:
            out_path = Path(args.html).expanduser().resolve()
        else:
            out_path = Path(
                f"/tmp/token-review-{args.window}.html").resolve()
        out_path.write_text(render_html(report), encoding="utf-8")
        # Use stderr so the path note doesn't get tangled with the
        # markdown body when callers pipe stdout into a viewer.
        print(f"\n_HTML report: {out_path}_", file=sys.stderr)
        if not args.no_open:
            import subprocess
            try:
                # macOS `open` — fine if it fails on non-darwin; the file
                # is already on disk and the user can open it manually.
                subprocess.run(["open", str(out_path)], check=False,
                               capture_output=True, timeout=5)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
