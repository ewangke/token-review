#!/usr/bin/env node
"use strict";

// Installer for the token-review Claude Code skill.
//
// Copies the skill payload (SKILL.md, scripts/, README.md, LICENSE) into the
// canonical skill location: ~/.claude/skills/token-review. Idempotent — run it
// again to update an existing install. Works both when launched via
// `npx github:<owner>/<repo>` (npm clones the repo to a temp dir) and from a
// locally installed package, because the source is resolved relative to this
// script.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const PKG_ROOT = path.resolve(__dirname, "..");
const TARGET = path.join(os.homedir(), ".claude", "skills", "token-review");
const PAYLOAD = ["SKILL.md", "scripts", "README.md", "LICENSE"];

function fail(msg) {
  console.error(`token-review: ${msg}`);
  process.exit(1);
}

function copyPayload() {
  // Required pieces — abort if the package is incomplete.
  for (const required of ["SKILL.md", "scripts"]) {
    if (!fs.existsSync(path.join(PKG_ROOT, required))) {
      fail(`package payload incomplete (missing ${required}), aborting.`);
    }
  }

  // If the package already sits at the canonical location (e.g. the repo was
  // cloned straight there), there's nothing to copy — copying a dir onto
  // itself throws. Treat it as already-installed.
  if (path.resolve(PKG_ROOT) === path.resolve(TARGET)) {
    return;
  }

  fs.mkdirSync(TARGET, { recursive: true });
  for (const item of PAYLOAD) {
    const src = path.join(PKG_ROOT, item);
    if (!fs.existsSync(src)) continue;
    fs.cpSync(src, path.join(TARGET, item), { recursive: true, force: true });
  }
}

function checkPython() {
  for (const bin of ["python3", "python"]) {
    const r = spawnSync(bin, ["--version"], { encoding: "utf8" });
    if (r.status === 0) {
      return (r.stdout || r.stderr || "").trim();
    }
  }
  return null;
}

function main() {
  copyPayload();

  console.log(`token-review installed to ${TARGET}`);

  const py = checkPython();
  if (py) {
    console.log(`Found ${py}.`);
  } else {
    console.log(
      "WARNING: no python3 found on PATH. The skill needs Python 3.8+ to run."
    );
  }

  console.log("");
  console.log("Next steps:");
  console.log("  1. Restart Claude Code (or start a new session).");
  console.log('  2. Ask: "/token-review" or "review my recent token usage".');
  console.log("");
  console.log("Or run the analyzer directly:");
  console.log(
    "  python3 ~/.claude/skills/token-review/scripts/analyze.py 7d"
  );
}

main();
