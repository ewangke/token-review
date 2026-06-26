# token-review

A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill that reviews
your token consumption across recent sessions — totals, distribution (by model /
project / hour), top-N sessions with task fingerprints, and concrete
optimization suggestions.

It reads your **local** session logs from `~/.claude/projects/**/*.jsonl`, so no
data leaves your machine. The analyzer is pure Python stdlib (no dependencies).

## What you get

- **Summary** — total tokens, assistant turns, estimated cost (USD), cache
  effective rate, Opus share.
- **Distribution** — by model (with input/output/cache breakdown), by project,
  and by hour-of-day.
- **Top sessions** — the heaviest sessions with a task fingerprint, tool usage,
  and a per-session model recommendation (keep / downgrade / upgrade / review).
- **Upstream-provider dispatch** — sub-agent sessions routed to non-Anthropic
  providers (cc-minimax, cc-switch, etc.), tracked separately at $0 Anthropic
  cost.
- **Optimization suggestions** — actionable notes on cache hit rate, model
  choice, oversized short sessions, and long-running sessions.

Output is both **markdown** (printed in chat) and a self-contained **HTML**
report (written to `/tmp/` and opened in your browser).

## Requirements

- macOS or Linux
- Python 3.8+ (stdlib only — nothing to `pip install`)
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed, with
  session logs under `~/.claude/projects/`

## Install

The skill expects to live at `~/.claude/skills/token-review`. Clone it straight
there:

```bash
git clone <REPO_URL> ~/.claude/skills/token-review
```

If you cloned elsewhere, either move it or symlink it into place:

```bash
ln -s /path/to/token-review ~/.claude/skills/token-review
```

Restart Claude Code (or start a new session) so it picks up the skill.

## Usage

### As a skill (recommended)

Just ask in natural language — the skill auto-triggers on phrases like:

- `/token-review`
- "看看我最近 token 消耗" / "过去 24 小时哪些 session 最烧 token"
- "review my recent token usage" / "show top consuming sessions"
- "我的 cache 命中率怎么样" / "analyze claude code cost"

Default window is **24h**. You can pass any window: `7d`, `2w`, `90m`, etc.

### Direct CLI

```bash
python3 ~/.claude/skills/token-review/scripts/analyze.py 7d
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--project current` | Restrict to sessions whose `cwd` is under `$PWD`. |
| `--project /path/to/project` | Restrict to a specific path prefix. |
| `--top N` | Size of the top-sessions list. |
| `--html PATH` | Override the HTML output path. |
| `--no-html` | Markdown only — skip writing/opening HTML. |
| `--no-open` | Write the HTML but don't open the browser (headless). |
| `--json` | Emit raw JSON only (for follow-up scripting). |

## Notes

- **Pricing is approximate.** Cost figures use Anthropic public list prices and
  are meant for *relative* comparison between sessions/models/projects — not for
  billing reconciliation. Check the Anthropic console for authoritative numbers.
- **Local only.** Sessions run on another machine or via the API directly won't
  appear here.
- **Forked sessions / sidechains** are not specially deduplicated; each
  `sessionId` is one unit.

## Layout

```
token-review/
├── SKILL.md              # skill manifest + usage contract for Claude Code
├── scripts/
│   ├── analyze.py        # the analyzer (stdlib only)
│   └── render_html.py    # HTML renderer invoked by analyze.py
├── README.md
└── LICENSE
```

## License

MIT — see [LICENSE](LICENSE).
