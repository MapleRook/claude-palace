#!/usr/bin/env python3
"""
palace_custodians.py — Autonomous palace maintenance agents
============================================================

Four custodian types that walk the palace, audit, verify, expand, and link:
  1. Auditor  — finds stale facts, empty rooms, orphan memories (Haiku)
  2. Verifier — cross-references flagged items against codebase (Haiku→Sonnet)
  3. Expander — researches adjacent topics, adds with confidence=0.7 (Sonnet)
  4. Linker   — finds cross-wing entity connections (Haiku)

Respects active_lock — won't run if user has an interactive session.

Usage:
    python palace_custodians.py --wing code-ac7-decoded --verbose
    python palace_custodians.py --wing code-ac7-decoded --only auditor verifier
    python palace_custodians.py --all-wings --budget 0.20
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

CLAUDE_EXE = Path.home() / ".local" / "bin" / "claude.exe"
LOG_FILE = Path.home() / ".claude" / "palace" / "custodian_log.jsonl"
DEFAULT_BUDGET = 0.15  # USD per custodian call
LINKER_BUDGET = 0.25  # Linker needs more headroom for cross-wing searches
BOOTSTRAP_EXPANDER_BUDGET = 0.50  # Higher budget for expander during initial population
LINKER_MAX_FAILURES = 2  # Escalate linker to Sonnet after this many consecutive failures


# ── Custodian Definitions ──────────────────────────────────────────

CUSTODIANS = {
    "auditor": {
        "model": "haiku",
        "label": "Auditor",
        "description": "Walk palace rooms, find gaps and stale facts",
        "prompt_template": """You are a Palace Auditor. Your job is to audit the memory palace for wing "{wing}".

Use these palace MCP tools to inspect the current state:
1. palace_status — get overall stats
2. palace_recall wing="{wing}" — see what's stored
3. palace_kg_stats — check knowledge graph health
4. palace_kg_audit stale_days=30 — find stale/unverified facts
5. palace_search wing="{wing}" — for each topic/room with apparent
   redundancy, search it; results include `id`, `date`, `current`. This
   is how you surface currency candidates with the IDs the Verifier needs.

After inspection, report:
- Empty or sparse rooms (< 3 memories)
- Stale KG triples (never verified or >30 days old)
- Rooms with high duplication (similar memories that should be consolidated)
- Missing KG connections (entities mentioned in memories but not in KG)
- Overall health score (1-10)
- Currency candidates: pairs of CURRENT memories in the SAME wing where
  one looks like a newer/more-complete statement of the same fact the
  other makes (not merely the same topic). For each, give both `id`s,
  a guess at which is newer (use `date` as a weak prior — content
  recency wins over date), and a one-line reason. Skip pairs that are
  genuinely complementary (both still true) — those are NOT candidates.

Output your findings as JSON:
{{
  "wing": "{wing}",
  "health_score": <1-10>,
  "sparse_rooms": [...],
  "stale_triples": [...],
  "consolidation_candidates": [...],
  "missing_kg_links": [...],
  "currency_review": [
    {{"older_id": "<id>", "newer_id": "<id>", "room": "<room>",
      "reason": "<why newer_id supersedes older_id, or 'stale:<why>' if
      older_id should just be retired with no replacement>"}}
  ],
  "recommendations": [...]
}}

Keep the JSON compact — the Verifier consumes it and the handoff is
size-bounded. Cap currency_review at the ~8 highest-signal pairs.""",
    },
    "verifier": {
        "model": "haiku",
        "label": "Verifier",
        "description": "Cross-reference flagged items against current state",
        "prompt_template": """You are a Palace Verifier for wing "{wing}".

The Auditor found these issues:
{audit_findings}

Your job:
1. For each stale KG triple: use palace_kg_query to check if the fact is still accurate.
   - If it looks correct, call palace_kg_verify with the triple_id to update last_verified.
   - If it's outdated, call palace_kg_invalidate with the triple_id and a reason.
2. For sparse rooms: check if there's content that should be there but isn't.
3. For consolidation candidates: use palace_consolidate if appropriate.
4. Prose currency — act on the Auditor's `currency_review` ONLY. For each
   pair, first read both memories (palace_search wing="{wing}" with a
   query from their topic; results carry `id`, `text`, `date`, `current`)
   and confirm the Auditor's read with your own eyes. Then exactly one of:
   - newer_id is a newer / more-complete statement of the SAME fact
     older_id makes → palace_supersede(old_id=older_id, new_id=newer_id).
   - reason is "stale:..." (older_id is obsolete/wrong with NO
     replacement) → palace_retire(memory_id=older_id, reason="<short>").
   - On your own inspection they are actually complementary (both still
     true, different facts) → do nothing; list in currency_left_alone.
   Hard rules:
   - Same wing only. Never supersede across wings (the tool rejects it;
     don't attempt it). Operate strictly within "{wing}".
   - NEVER palace_delete for currency. Retire/supersede are reversible
     (palace_restore); delete is not. If you make a call and immediately
     doubt it, palace_restore and escalate instead.
   - Not confident it's the same fact, or unsure which is newer? Do NOT
     guess — put it in `escalated` for a Sonnet pass. A wrong supersede
     hides a true memory; escalation is the cheap, correct default.
   - Never touch quarantined/low-confidence memories here (they have
     their own lifecycle); the Auditor's candidates are already current.

Output your actions as JSON:
{{
  "verified": [<triple_ids verified>],
  "invalidated": [<triple_ids invalidated with reasons>],
  "consolidated": [<rooms consolidated>],
  "superseded": [{{"old_id": "<id>", "new_id": "<id>", "reason": "<why>"}}],
  "retired": [{{"id": "<id>", "reason": "<why>"}}],
  "currency_left_alone": [{{"pair": ["<id>", "<id>"], "why": "<both true>"}}],
  "escalated": [<items too complex/ambiguous for Haiku, need Sonnet>]
}}""",
    },
    "expander": {
        "model": "sonnet",
        "label": "Expander",
        "description": "Research adjacent topics, add new knowledge",
        "prompt_template": """You are a Palace Expander for wing "{wing}".

Current palace state for this wing:
{palace_summary}

Gaps identified by Auditor:
{audit_findings}

Your job:
1. Look at existing rooms and identify knowledge gaps — what adjacent topics would be valuable?
2. For each gap, use your knowledge to fill it with accurate information.
3. Add new memories via palace_add with appropriate hall/room/drawer.
4. Add KG triples for new entity relationships via palace_kg_add.

IMPORTANT RULES:
- Only add facts you're confident about (don't hallucinate)
- Tag speculative or uncertain additions in the content itself
- Focus on connections and context that make existing knowledge more useful
- Don't duplicate what's already in the palace

Output your additions as JSON:
{{
  "memories_added": [<descriptions of new memories>],
  "kg_triples_added": [<new entity relationships>],
  "gaps_remaining": [<things you couldn't fill>]
}}""",
    },
    "structurer": {
        "model": "sonnet",
        "label": "Structurer",
        "description": "Extract KG entities from unstructured memory dumps",
        "prompt_template": """You are a Palace Structurer for wing "{wing}".

The Auditor found these issues:
{audit_findings}

Your job is to find memories that contain structured data (tables, lists, catalogs, specs) but have NO corresponding KG entities, and fix that.

Steps:
1. Use palace_recall wing="{wing}" to see the memories
2. Use palace_kg_stats and palace_kg_query to see what entities already exist
3. For each memory that contains entities not yet in the KG:
   a. Use palace_kg_add to create entity triples (subject -> predicate -> object)
   b. Focus on: names, types, categories, relationships, stats, dependencies
4. Look for patterns: if a memory has a table with 20 rows, each row is likely an entity

Entity extraction priorities:
- Named things (people, tools, files, products, places, game objects)
- Relationships between them (uses, depends_on, created_by, contains, equips)
- Categories and types (aircraft→fighter, weapon→air-to-air)
- Numeric properties as triples where meaningful (aircraft -> has_cost -> 1500)

IMPORTANT RULES:
- Only extract entities that are clearly present in the memories — don't invent
- Prefer specific, factual triples over vague ones
- Skip entities that are already in the KG (check first)
- If a wing has hundreds of memories but very few KG entities, that's the signal to act

Output your actions as JSON:
{{
  "entities_found": <count of new entities identified>,
  "triples_added": <count of KG triples created>,
  "categories": [<types of entities extracted, e.g. "aircraft", "weapons">],
  "skipped": <count skipped because already in KG>,
  "gaps_remaining": [<entity types that need more work>]
}}""",
    },
    "linker": {
        "model": "haiku",
        "label": "Linker",
        "description": "Find cross-wing connections",
        "prompt_template": """You are a Palace Linker. Your job is to find connections between wing "{wing}" and other wings.

1. Use palace_recall wing="{wing}" to see this wing's content
2. Use palace_patterns to find cross-wing patterns
3. Use palace_search with key entities from this wing (no wing filter) to find matches in other wings
4. For each cross-wing connection found, add a KG triple linking them via palace_kg_add

Focus on:
- Shared entities (people, tools, concepts that appear in multiple wings)
- Shared patterns (similar problems solved differently in different projects)
- Dependencies (one project's output feeding another)

Output your findings as JSON:
{{
  "connections_found": [<descriptions of cross-wing links>],
  "kg_triples_added": [<new cross-wing relationships>],
  "potential_links": [<uncertain connections worth investigating>]
}}""",
    },
}


# ── Execution ──────────────────────────────────────────────────────

def run_custodian(name: str, wing: str, budget: float,
                  context: dict = None, verbose: bool = False) -> dict:
    """Run a single custodian agent via claude --print."""
    custodian = CUSTODIANS[name]
    model = custodian["model"]

    # Build prompt from template
    prompt = custodian["prompt_template"].format(
        wing=wing,
        audit_findings=context.get("audit_findings", "N/A") if context else "N/A",
        palace_summary=context.get("palace_summary", "N/A") if context else "N/A",
    )

    # Palace MCP tools the custodians need
    allowed = ",".join([
        "mcp__palace__palace_status",
        "mcp__palace__palace_recall",
        "mcp__palace__palace_search",
        "mcp__palace__palace_add",
        "mcp__palace__palace_delete",
        "mcp__palace__palace_consolidate",
        "mcp__palace__palace_patterns",
        "mcp__palace__palace_kg_query",
        "mcp__palace__palace_kg_add",
        "mcp__palace__palace_kg_invalidate",
        "mcp__palace__palace_kg_stats",
        "mcp__palace__palace_kg_timeline",
        "mcp__palace__palace_kg_audit",
        "mcp__palace__palace_kg_verify",
        "mcp__palace__palace_retire",
        "mcp__palace__palace_supersede",
        "mcp__palace__palace_restore",
    ])

    cmd = [
        str(CLAUDE_EXE), "--print",
        "--model", model,
        "--output-format", "json",
        "--max-budget-usd", str(budget),
        "--allowedTools", allowed,
    ]
    cmd.extend(["-p", prompt])

    if verbose:
        print(f"  [{custodian['label']}] Starting ({model}, ${budget} budget)...",
              file=sys.stderr)

    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300, encoding="utf-8", errors="replace",
        )
        elapsed = round(time.time() - start, 1)

        output = result.stdout.strip()
        try:
            parsed = json.loads(output)
            text = parsed.get("result", output)
        except json.JSONDecodeError:
            text = output

        entry = {
            "custodian": name,
            "wing": wing,
            "model": model,
            "success": result.returncode == 0,
            "elapsed": elapsed,
            # Bumped 2000->6000: the auditor->verifier currency handoff
            # carries memory IDs (~50 chars each); at 2000 they were
            # truncated away and step-3 supersede/retire became
            # non-functional. Still bounded for the log.
            "output": text[:6000],
            "timestamp": datetime.now().isoformat(),
        }

        if verbose:
            status = "OK" if entry["success"] else "FAIL"
            print(f"  [{custodian['label']}] {status} ({elapsed}s)", file=sys.stderr)

        return entry

    except subprocess.TimeoutExpired:
        return {
            "custodian": name, "wing": wing, "model": model,
            "success": False, "elapsed": 300,
            "output": "TIMEOUT after 300s",
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {
            "custodian": name, "wing": wing, "model": model,
            "success": False, "elapsed": 0,
            "output": str(e),
            "timestamp": datetime.now().isoformat(),
        }


def log_entry(entry: dict):
    """Append entry to custodian log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def run_sweep(wing: str, custodian_names: list = None,
              budget: float = DEFAULT_BUDGET, bootstrap: bool = False,
              verbose: bool = False) -> list:
    """Run a full custodian sweep on a wing."""
    from active_lock import is_active

    if is_active():
        print("Active session detected — skipping custodian sweep", file=sys.stderr)
        return []

    names = custodian_names or ["auditor", "verifier", "structurer", "expander", "linker"]
    results = []
    context = {}

    for name in names:
        if name not in CUSTODIANS:
            print(f"Unknown custodian: {name}", file=sys.stderr)
            continue

        # Per-custodian budget overrides
        agent_budget = budget
        if name == "linker":
            agent_budget = max(budget, LINKER_BUDGET)
        if bootstrap and name in ("expander", "structurer"):
            agent_budget = max(budget, BOOTSTRAP_EXPANDER_BUDGET)
            if verbose:
                print(f"  [Bootstrap] {name.title()} budget: ${agent_budget}", file=sys.stderr)

        entry = run_custodian(name, wing, agent_budget, context=context, verbose=verbose)
        results.append(entry)
        log_entry(entry)

        # Linker escalation: if it failed, check recent history and retry on Sonnet
        if name == "linker" and not entry["success"]:
            recent_failures = _count_recent_linker_failures(wing)
            if recent_failures >= LINKER_MAX_FAILURES:
                if verbose:
                    print(f"  [Escalation] Linker failed {recent_failures}x on {wing} — retrying on Sonnet",
                          file=sys.stderr)
                # Temporarily override model to sonnet
                orig_model = CUSTODIANS["linker"]["model"]
                CUSTODIANS["linker"]["model"] = "sonnet"
                retry = run_custodian("linker", wing, agent_budget, context=context, verbose=verbose)
                CUSTODIANS["linker"]["model"] = orig_model
                results.append(retry)
                log_entry(retry)

        # Pass audit findings to downstream custodians
        if name == "auditor" and entry["success"]:
            # 5000 so currency_review (memory ID pairs) survives the
            # handoff to the Verifier; palace_summary stays small (it is
            # only a context blurb for the Expander).
            context["audit_findings"] = entry["output"][:5000]
            context["palace_summary"] = entry["output"][:1000]

    return results


def _count_recent_linker_failures(wing: str) -> int:
    """Count consecutive recent linker failures for a wing from the log."""
    if not LOG_FILE.exists():
        return 0
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        # Filter to linker entries for this wing, most recent first
        linker_entries = [e for e in reversed(entries)
                         if e.get("custodian") == "linker" and e.get("wing") == wing]
        count = 0
        for e in linker_entries[:5]:  # Look at last 5
            if not e.get("success"):
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def get_all_wings() -> list:
    """Get list of known wings from palace (authoritative: collection metadata)."""
    try:
        from palace import MemoryStack
        wings = MemoryStack().status().get("wings", {})
        return sorted(w for w in wings if w and w != "unknown")
    except Exception:
        # Fallback: known wings
        return [
            "small-towns-ai", "optimization", "adhd-reminders",
            "code-ac7-decoded", "claude-manager",
        ]


def main():
    parser = argparse.ArgumentParser(description="Palace custodian agents")
    parser.add_argument("--wing", help="Wing to audit")
    parser.add_argument("--all-wings", action="store_true", help="Sweep all known wings")
    parser.add_argument("--only", nargs="+", choices=list(CUSTODIANS.keys()),
                        help="Run only specific custodians")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help=f"USD budget per custodian call (default: ${DEFAULT_BUDGET})")
    parser.add_argument("--verbose", action="store_true", help="Show progress")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--bootstrap", action="store_true",
                        help=f"Use higher expander budget (${BOOTSTRAP_EXPANDER_BUDGET}) for initial population")
    parser.add_argument("--force", action="store_true",
                        help="Run even if active session detected")
    parser.add_argument("--lean", action="store_true",
                        help="Cost-minimal: if --only is not given, run just "
                             "auditor+verifier (Haiku, the arbiter loop) — "
                             "skips the Sonnet expander/structurer and linker")
    parser.add_argument("--wings", help="Comma-separated wing allowlist "
                        "(overrides --all-wings)")
    parser.add_argument("--wings-file", help="Path to a newline-delimited wing "
                        "allowlist (# comments and blanks ignored)")
    parser.add_argument("--max-wings", type=int, default=0,
                        help="Cap wings swept this run (0 = no cap). Lets a "
                             "schedule rotate a cheap fixed-size slice.")
    args = parser.parse_args()

    if not args.wing and not args.all_wings and not args.wings and not args.wings_file:
        parser.error("Specify --wing, --all-wings, --wings, or --wings-file")

    if args.force:
        # Temporarily disable lock check by removing it
        from active_lock import release_lock
        release_lock()

    # Wing set resolution (allowlist sources beat the broad ones).
    from palace import canonicalize_wing
    if args.wings_file:
        with open(args.wings_file, encoding="utf-8") as f:
            raw = [ln.strip() for ln in f]
        wings = [canonicalize_wing(w) for w in raw
                 if w and not w.startswith("#")]
    elif args.wings:
        wings = [canonicalize_wing(w) for w in args.wings.split(",") if w.strip()]
    elif args.all_wings:
        wings = get_all_wings()
    else:
        wings = [canonicalize_wing(args.wing)]

    if args.max_wings and len(wings) > args.max_wings:
        wings = wings[:args.max_wings]

    only = args.only or (["auditor", "verifier"] if args.lean else None)

    if args.verbose:
        print(f"Sweeping {len(wings)} wing(s) "
              f"[{'lean: auditor+verifier' if only == ['auditor', 'verifier'] and args.lean else (','.join(only) if only else 'all 5 custodians')}]: "
              f"{', '.join(wings)}", file=sys.stderr)

    all_results = []
    for wing in wings:
        if args.verbose:
            print(f"\n{'='*40}", file=sys.stderr)
            print(f"Wing: {wing}", file=sys.stderr)
            print(f"{'='*40}", file=sys.stderr)

        results = run_sweep(wing, custodian_names=only,
                            budget=args.budget, bootstrap=args.bootstrap,
                            verbose=args.verbose)
        all_results.extend(results)

    if args.json:
        print(json.dumps(all_results, indent=2))
    else:
        # Summary
        ok = sum(1 for r in all_results if r["success"])
        fail = len(all_results) - ok
        total_time = sum(r["elapsed"] for r in all_results)
        print(f"\nSweep complete: {ok} OK, {fail} FAIL, {total_time:.0f}s total")
        for r in all_results:
            status = "OK" if r["success"] else "FAIL"
            print(f"  {r['custodian']:10s} {r['wing']:25s} {status} ({r['elapsed']}s)")


if __name__ == "__main__":
    main()
