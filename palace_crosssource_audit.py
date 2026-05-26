"""palace_crosssource_audit.py — cross-source audit: palace + convindex.

Extends palace_crosswing_audit with behavioral pattern detection from
convindex session openings. Palace indexes agent outputs; convindex
indexes USER triggers (what the user typed to start a session). Some
patterns are only visible at the trigger side — most notably the
"autonomous-while-away" pattern, which Claude doesn't describe in its
outputs the way the user does in their openings.

Adds two new signals on top of palace_crosswing_audit's three:

  5. cross-source-behavioral-pattern
     Behavior-trigger phrases (regex) matched against session openings
     (sessions.first_user_msg in convindex). When a pattern appears in
     ≥2 distinct projects, flag as a recurring behavioral pattern that
     palace probably doesn't capture (yet).

  6. autonomous-away-pattern (specialization of 5)
     The specific "while I'm gone/asleep/at work" pattern that surfaced
     during the manual audit. Treated as its own signal so it can be
     prioritized.

Combined with the existing 3 palace signals (1, 3, 4), this script
emits a unified candidates.jsonl that palace_viz can consume.

Usage:
    "E:/Claude/Optimization/venv/Scripts/python.exe" palace_crosssource_audit.py --out candidates.jsonl
    palace_crosssource_audit.py --signal cross-source-behavioral-pattern
    palace_crosssource_audit.py --summary
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# Import the palace-side signals from the prior audit. Same dir.
sys.path.insert(0, str(Path(__file__).parent))
from palace_crosswing_audit import (  # noqa: E402
    load_memories,
    known_wings,
    signal_wing_named,
    signal_orphan_infra,
    signal_friction,
)

CONVINDEX_DB = Path(os.path.expanduser("~")) / ".claude" / "conversation-index" / "index.db"


# Behavior-trigger patterns as FTS5 phrase queries. Each (phrases, label):
# phrases is a list of exact phrases — any match counts. The patterns are
# matched against role='user' messages in msg_fts (so we catch the patterns
# wherever they appear in user turns, not just session openings).
BEHAVIOR_PATTERNS = [
    (["\"while I'm gone\"", "\"while I'm away\"", "\"while I'm asleep\"",
      "\"while I'm at work\"", "\"while you sleep\"", "\"while you're away\""],
     "autonomous-away"),
    (["overnight"], "autonomous-overnight"),
    (["\"I'm panicking\"", "\"I'm overwhelmed\"", "\"I'm stressed\""],
     "emotional-trigger"),
    (["\"copy paste\"", "\"copy-paste\"", "\"copy pasting\""],
     "copy-paste-mention"),
    (["\"cheat engine\""], "cheat-engine"),
    (["\"into the console\"", "\"into the terminal\"", "\"paste into\""],
     "into-the-console"),
    (["unproductive", "\"burnt out\"", "exhausted"], "energy-trigger"),
]


def fts_search(conn, phrase):
    """Run FTS5 phrase query against msg_fts, return distinct session_ids."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM msg_fts "
            "WHERE msg_fts MATCH ? AND role = 'user'",
            (phrase,),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def load_sessions_meta():
    """{session_id: {project_dir, cwd, first_user_msg, start_ts}} for all sessions."""
    if not CONVINDEX_DB.exists():
        return {}
    conn = sqlite3.connect(f"file:{CONVINDEX_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    out = {}
    for r in conn.execute(
        "SELECT session_id, project_dir, cwd, first_user_msg, start_ts FROM sessions"
    ):
        out[r["session_id"]] = dict(r)
    conn.close()
    return out


def normalize_project(session):
    """Best-effort project identifier from a session row. Prefer project_dir,
    fall back to cwd-derived name. Skip temp/scratch dirs."""
    pd = session.get("project_dir") or ""
    if "Temp" in pd or "mcclaude-cwd" in pd:
        return None
    if pd:
        # Convert "C--Claude-claude-palace" style → "claude-palace"
        m = re.search(r"-([A-Za-z][\w-]+?)$", pd)
        if m:
            return m.group(1).lower()
        return pd.lower()
    cwd = session.get("cwd") or ""
    if cwd and "Temp" not in cwd:
        return Path(cwd).name.lower()
    return None


def signal_cross_source_behavioral(_unused=None):
    """Find behavior-trigger phrases appearing in ≥2 distinct projects.

    Uses FTS5 against msg_fts (user-role only). Each (phrase_list, label)
    pattern fires when matching sessions span ≥2 normalized projects.
    """
    if not CONVINDEX_DB.exists():
        print(f"# convindex db missing — skipping behavioral signal", file=sys.stderr)
        return []
    sessions_meta = load_sessions_meta()
    conn = sqlite3.connect(f"file:{CONVINDEX_DB}?mode=ro", uri=True, timeout=10)
    candidates = []
    for phrases, label in BEHAVIOR_PATTERNS:
        session_ids = set()
        for phrase in phrases:
            for sid in fts_search(conn, phrase):
                session_ids.add(sid)
        if not session_ids:
            continue
        by_project = {}
        for sid in session_ids:
            meta = sessions_meta.get(sid)
            if not meta:
                continue
            proj = normalize_project(meta)
            if not proj:
                continue
            by_project.setdefault(proj, []).append({
                "session_id": sid,
                "snippet": (meta.get("first_user_msg") or "")[:120],
            })
        if len(by_project) < 2:
            continue
        examples = []
        for proj, sess_list in sorted(by_project.items(), key=lambda x: -len(x[1])):
            examples.append({
                "project": proj,
                "session_count": len(sess_list),
                "first_session": sess_list[0]["session_id"][:8],
                "first_opening": sess_list[0]["snippet"],
            })
        candidates.append({
            "signal": "cross-source-behavioral-pattern",
            "memory_id": f"behavior:{label}",
            "current_wing": "(none — synthesis candidate)",
            "current_room": label,
            "evidence": {
                "pattern_label": label,
                "phrases_checked": phrases,
                "projects_affected": len(by_project),
                "total_sessions": sum(len(v) for v in by_project.values()),
                "examples": examples[:10],
            },
            "suggested_action": (
                f"Write a synthesis memory for behavior pattern '{label}'. "
                f"It appears in {len(by_project)} projects but isn't captured "
                f"in palace as a recurring pattern. Cite the example sessions."
            ),
            "confidence": min(0.5 + 0.1 * len(by_project), 0.9),
        })
    conn.close()
    return candidates


PALACE_SIGNALS = {
    "wing-named-in-other-wing": signal_wing_named,
    "wing-orphaned-infra": signal_orphan_infra,
    "friction-flag": signal_friction,
}

CONVINDEX_SIGNALS = {
    "cross-source-behavioral-pattern": signal_cross_source_behavioral,
}

ALL_SIGNALS = list(PALACE_SIGNALS.keys()) + list(CONVINDEX_SIGNALS.keys())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--signal", choices=ALL_SIGNALS,
                    help="Run only this signal (default: all)")
    ap.add_argument("--out", help="Write JSONL to file instead of stdout")
    ap.add_argument("--summary", action="store_true",
                    help="Print summary counts instead of JSONL")
    args = ap.parse_args()

    # Load both sides.
    memories = load_memories()
    wings = known_wings(memories)
    print(f"# palace: {len(memories)} memories across {len(wings)} wings",
          file=sys.stderr)

    selected = [args.signal] if args.signal else ALL_SIGNALS
    all_cands = []
    for name in selected:
        if name in PALACE_SIGNALS:
            fn = PALACE_SIGNALS[name]
            cands = fn(memories, wings) if name == "wing-named-in-other-wing" else fn(memories)
        elif name in CONVINDEX_SIGNALS:
            fn = CONVINDEX_SIGNALS[name]
            cands = fn()
        else:
            continue
        print(f"# {name}: {len(cands)} candidates", file=sys.stderr)
        all_cands.extend(cands)

    if args.summary:
        by_signal = {}
        for c in all_cands:
            by_signal[c["signal"]] = by_signal.get(c["signal"], 0) + 1
        print(json.dumps({
            "total_candidates": len(all_cands),
            "by_signal": by_signal,
        }, indent=2))
        return

    out_lines = [json.dumps(c, ensure_ascii=False) for c in all_cands]
    out_blob = "\n".join(out_lines) + "\n" if out_lines else ""
    if args.out:
        Path(args.out).write_text(out_blob, encoding="utf-8")
        print(f"# wrote {len(out_lines)} candidates to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out_blob)


if __name__ == "__main__":
    main()
