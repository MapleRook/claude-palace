#!/usr/bin/env python3
"""
palace_prime.py — Context Priming Hook for Palace
===================================================

Runs on SessionStart. Detects the current project from cwd,
queries the palace for relevant context, and outputs it so
Claude starts each session with project-relevant memories.

Hook input (stdin JSON):
  { session_id, transcript_path, cwd, source, ... }

Output: prints context to stdout (which becomes hook feedback to Claude).

Usage in settings.json hooks:
  SessionStart hook → python palace_prime.py
"""

import json
import os
import sys
from pathlib import Path

# Add scripts dir for palace import
sys.path.insert(0, str(Path(__file__).parent))

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# Wing detection — same mapping as digest hook
WING_MAPPINGS = {
    "small_towns_ai": "small-towns-ai",
    "small-towns": "small-towns-ai",
    "/ai/": "small-towns-ai",
    "c--ai": "small-towns-ai",
    "optimization": "optimization",
    "adhdreminders": "adhd-reminders",
    "adhd": "adhd-reminders",
    "musicmanagement": "music-management",
    "semanticsearch": "semantic-search",
    "sidehustle": "side-hustle",
    "contractordraft": "side-hustle",
    "ticketmachine": "ticket-machine",
    "roadquality": "road-quality",
    "ac7_decoded": "code-ac7-decoded",
    "ac7_asymmetrical": "code-ac7-asymmetrical",
    "ac7_mapfix": "code-ac7-mapfix",
    "ac7_nausicaa": "code-ac7-nausicaa",
    "ac7_nocheatmods": "code-ac7-nocheatmods",
    "ac7": "code-ac7",
    "sw5e": "code-sw5e",
    "aheadofthegame": "aheadofthegame",
    "codelearning": "codelearning",
    "claudemanager": "claude-manager",
}


def detect_wing_from_cwd(cwd: str) -> str:
    cwd_lower = cwd.replace("\\", "/").lower()
    for pattern, wing in WING_MAPPINGS.items():
        if pattern in cwd_lower:
            return wing
    name = Path(cwd).name.lower().replace(" ", "-")
    return name or "unknown"


def check_unknown_project(cwd: str, wing: str, get_collection) -> str:
    """Return a nudge message if cwd has NEITHER a CLAUDE.md NOR any
    palace memories for its detected wing — otherwise None.

    Why: a project with no CLAUDE.md and no palace wing is a blank slate.
    Without intervention, every session re-derives everything (the
    CABINTools-class discovery failure). The global CLAUDE.md says to
    auto-generate a CLAUDE.md in this case — this nudge surfaces the
    condition explicitly so the rule actually fires.
    """
    if not cwd:
        return None
    candidates = [
        os.path.join(cwd, "CLAUDE.md"),
        os.path.join(cwd, ".claude", "CLAUDE.md"),
    ]
    if any(os.path.exists(c) for c in candidates):
        return None
    col = get_collection()
    if not col:
        return None
    try:
        # Match palace's canonicalization.
        from palace import canonicalize_wing
        wing_canon = canonicalize_wing(wing)
        res = col.get(where={"wing": {"$eq": wing_canon}}, limit=1)
        if (res.get("ids") or []):
            return None
    except Exception:
        return None
    return (
        "## Project Discovery — blank slate detected\n"
        f"No CLAUDE.md found at `{cwd}` and no palace memories for wing "
        f"`{wing}`. This project is unknown to your memory system.\n\n"
        "**Action:** before doing substantive work, either (a) auto-generate "
        "a project CLAUDE.md after scanning the codebase per the global rule "
        "(project auto-profiling), or (b) confirm with the user that this dir "
        "is intentional scratch. Don't proceed silently as if context exists."
    )


def get_recent_git_context(cwd: str) -> str:
    """Get recent git log for additional context."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5", "--no-color"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"\nRecent commits:\n{result.stdout.strip()}"
    except Exception:
        pass
    return ""


def run_prime(hook_input: dict):
    """Generate context priming output."""
    from palace import MemoryStack, KnowledgeGraph, search_memories, _get_collection

    cwd = hook_input.get("cwd", "")
    source = hook_input.get("source", "startup")

    # Only prime on fresh starts and resumes, not compacts
    if source not in ("startup", "resume"):
        return

    wing = detect_wing_from_cwd(cwd)

    # Check if palace has any data
    col = _get_collection()
    if not col or col.count() == 0:
        return

    parts = []

    # Blank-slate nudge: emitted FIRST so it can't be missed.
    nudge = check_unknown_project(cwd, wing, _get_collection)
    if nudge:
        parts.append(nudge)

    # L1: Project-specific memories (top 5 most relevant)
    stack = MemoryStack()
    try:
        recall = stack.recall(wing=wing, n_results=5)
        if recall and "No memories found" not in recall and "No palace found" not in recall:
            parts.append(f"## Palace Context [{wing}]")
            parts.append(recall)
    except Exception:
        pass

    # KG: Key facts about the project
    try:
        kg = KnowledgeGraph()
        # Query for the project entity
        project_names = [wing, wing.replace("-", " ").title(), wing.replace("-", "_")]
        for name in project_names:
            facts = kg.query_entity(name, direction="outgoing")
            if facts:
                fact_lines = []
                for f in facts[:8]:
                    if f.get("current"):
                        fact_lines.append(f"  {f['subject']} → {f['predicate']} → {f['object']}")
                if fact_lines:
                    parts.append("\n## Knowledge Graph")
                    parts.append("\n".join(fact_lines))
                break
    except Exception:
        pass

    # KG Timeline: recent facts across all entities for this wing's context
    try:
        timeline = kg.timeline(limit=10)
        if timeline:
            recent_facts = []
            for t in timeline[:5]:
                if t.get("current"):
                    recent_facts.append(f"  [{t.get('valid_from', '?')}] {t['subject']} → {t['predicate']} → {t['object']}")
            if recent_facts:
                parts.append("\n## Recent KG Activity")
                parts.append("\n".join(recent_facts))
    except Exception:
        pass

    # Recent git context
    git_ctx = get_recent_git_context(cwd)
    if git_ctx:
        parts.append(git_ctx)

    if parts:
        output = "\n".join(parts)
        # Cap output to avoid bloating context
        if len(output) > 4000:
            output = output[:3997] + "..."
        print(output)


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        return

    run_prime(hook_input)


if __name__ == "__main__":
    main()
