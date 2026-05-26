"""palace_crosswing_audit.py — find memories that should probably be
cross-linked, promoted to global, or refiled.

Reads chroma sqlite in read-only mode so it coexists with the live MCP
(no chromadb dep, same pattern as palace_migrate_links_safe.py).

Four signals; ships three (signal 2 deferred — needs semantic clustering):

  1. wing-named-in-other-wing  — memory in wing X mentions ≥2 other wing
     names. Common when one memory has consequences across projects
     (the W-2 IP carve-out is the canonical example).

  2. abstract-pattern-instances — same abstract pattern repeated across
     wings with different concrete instances. DEFERRED — embedding-based
     clustering required.

  3. wing-orphaned-infra        — memory uses generic infrastructure
     vocabulary (IMAP, SMTP, port, hostname, sqlite, etc.) but lives in
     one project wing. Likely useful cross-project. The proton-bridge
     case.

  4. friction-flag              — memory contains user-as-interface
     friction keywords (copy paste, manual transcription, into the
     console). Flags places where the user-as-interface-decay pattern
     might be active.

Output: JSONL to stdout (or --out path). One candidate per line.

Usage:
    "E:/Claude/Optimization/venv/Scripts/python.exe" palace_crosswing_audit.py
    palace_crosswing_audit.py --signal wing-named-in-other-wing
    palace_crosswing_audit.py --out candidates.jsonl
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

PALACE_DIR = Path(os.path.expanduser("~")) / ".claude" / "palace"
CHROMA_DB = PALACE_DIR / "chroma" / "chroma.sqlite3"
KG_DB = PALACE_DIR / "kg.sqlite3"


# Generic infrastructure vocabulary. Hits on these suggest a memory's
# topic is project-agnostic. Tuned empirically — adjust if false positives
# dominate.
INFRA_TERMS = re.compile(
    r"\b(imap|smtp|port|hostname|loopback|starttls|tls|ssl|sqlite|chromadb|redis|"
    r"postgres|mysql|http|https|websocket|grpc|json-rpc|stdin|stdout|stderr|"
    r"env(?:ironment)? variable|cron|systemd|launchd|schtasks|wal|fts5|venv|"
    r"virtualenv|pyenv|conda|pip install|npm install|github actions|webhook|"
    r"oauth|jwt|api key|secret manager|cloud run|lambda|kubernetes|docker)\b",
    re.IGNORECASE,
)

# User-as-interface friction signals.
FRICTION_PATTERNS = [
    (re.compile(r"\bcopy[ -]?paste\b", re.IGNORECASE), "copy-paste"),
    (re.compile(r"\bmanual(?:ly)?\s+(?:transcrib|copy|type|enter|paste|input)\w*", re.IGNORECASE), "manual-transcription"),
    (re.compile(r"\b(?:paste|type|copy)\s+(?:into|to)\s+(?:the\s+)?(?:console|terminal|browser|input)\b", re.IGNORECASE), "paste-into-X"),
    (re.compile(r"\b(?:user|dalton)\s+(?:has to|needs to|must)\s+(?:copy|paste|type|transcribe)\b", re.IGNORECASE), "user-must-X"),
    (re.compile(r"\bby hand\b", re.IGNORECASE), "by-hand"),
    (re.compile(r"\bcheat\s*engine\b", re.IGNORECASE), "cheat-engine"),
]


def load_memories():
    """Return list of {id, drawer_id, wing, room, hall, doc, current}."""
    conn = sqlite3.connect(f"file:{CHROMA_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    drawer_by_int = {r["id"]: r["embedding_id"]
                     for r in conn.execute("SELECT id, embedding_id FROM embeddings")}
    by_id = {}
    for r in conn.execute(
        "SELECT id, key, string_value, int_value, bool_value FROM embedding_metadata"
    ):
        val = (r["string_value"] if r["string_value"] is not None
               else (r["int_value"] if r["int_value"] is not None else r["bool_value"]))
        by_id.setdefault(r["id"], {})[r["key"]] = val
    conn.close()
    out = []
    for mid, meta in by_id.items():
        if not meta.get("current"):
            continue
        if meta.get("quarantined"):
            continue
        out.append({
            "id": drawer_by_int.get(mid, f"chid:{mid}"),
            "wing": meta.get("wing"),
            "room": meta.get("room"),
            "hall": meta.get("hall"),
            "doc": meta.get("chroma:document") or "",
        })
    return out


def known_wings(memories):
    """Return sorted list of canonical wing names present in palace."""
    return sorted({m["wing"] for m in memories if m["wing"]})


def signal_wing_named(memories, wings):
    """Memory mentions ≥2 OTHER wing names in its body."""
    # Build per-wing regex (word-boundary, case-insensitive). Skip very
    # short wing names that would match too much.
    safe_wings = [w for w in wings if len(w) >= 5 and w not in {"global", "palace"}]
    if not safe_wings:
        return []
    candidates = []
    for m in memories:
        if not m["doc"] or not m["wing"]:
            continue
        doc_l = m["doc"].lower()
        # Find mentions of OTHER wings (skip the memory's own wing).
        hits = set()
        for w in safe_wings:
            if w == m["wing"]:
                continue
            if re.search(rf"\b{re.escape(w)}\b", doc_l):
                hits.add(w)
        if len(hits) >= 2:
            candidates.append({
                "signal": "wing-named-in-other-wing",
                "memory_id": m["id"],
                "current_wing": m["wing"],
                "current_room": m["room"],
                "evidence": {"wings_mentioned": sorted(hits)},
                "suggested_action": (
                    "Consider: promote to global, or write a global synthesis "
                    "memory citing this and the wings it touches."
                ),
                "confidence": min(0.5 + 0.1 * len(hits), 0.9),
            })
    return candidates


def signal_orphan_infra(memories):
    """Generic infra terms + filed in a single non-global wing."""
    candidates = []
    for m in memories:
        if not m["doc"] or not m["wing"] or m["wing"] == "global":
            continue
        hits = INFRA_TERMS.findall(m["doc"])
        if len(hits) < 4:
            continue
        # Density check: lots of infra hits relative to doc length.
        doc_len = max(len(m["doc"]), 100)
        density = len(hits) / (doc_len / 1000.0)
        if density < 2.0:
            continue
        candidates.append({
            "signal": "wing-orphaned-infra",
            "memory_id": m["id"],
            "current_wing": m["wing"],
            "current_room": m["room"],
            "evidence": {
                "infra_terms_count": len(hits),
                "density_per_kchar": round(density, 2),
                "sample_terms": list(set(t.lower() for t in hits))[:8],
            },
            "suggested_action": (
                "Consider: promote to global/reference if this infrastructure "
                "is usable cross-project. The proton-bridge memory is the "
                "canonical example of this pattern."
            ),
            "confidence": min(0.4 + 0.05 * len(hits), 0.85),
        })
    return candidates


def signal_friction(memories):
    """Memory contains user-as-interface friction keywords."""
    candidates = []
    for m in memories:
        if not m["doc"]:
            continue
        hits = []
        for pat, label in FRICTION_PATTERNS:
            if pat.search(m["doc"]):
                hits.append(label)
        if not hits:
            continue
        candidates.append({
            "signal": "friction-flag",
            "memory_id": m["id"],
            "current_wing": m["wing"],
            "current_room": m["room"],
            "evidence": {"friction_signals": sorted(set(hits))},
            "suggested_action": (
                "Review: is the user currently in the loop as a transcription "
                "step? If yes, propose programmatic-access pivot before "
                "project decays. See [[user-as-interface-decay]] in global."
            ),
            "confidence": min(0.4 + 0.15 * len(hits), 0.9),
        })
    return candidates


SIGNALS = {
    "wing-named-in-other-wing": signal_wing_named,
    "wing-orphaned-infra": signal_orphan_infra,
    "friction-flag": signal_friction,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--signal", choices=list(SIGNALS.keys()),
                    help="Run only this signal (default: all)")
    ap.add_argument("--out", help="Write JSONL to file instead of stdout")
    ap.add_argument("--summary", action="store_true",
                    help="Print summary counts instead of JSONL")
    args = ap.parse_args()

    if not CHROMA_DB.exists():
        print(json.dumps({"error": f"chroma db not found at {CHROMA_DB}"}))
        sys.exit(1)

    memories = load_memories()
    wings = known_wings(memories)
    print(f"# loaded {len(memories)} current memories across {len(wings)} wings",
          file=sys.stderr)

    selected = [args.signal] if args.signal else list(SIGNALS.keys())
    all_cands = []
    for name in selected:
        fn = SIGNALS[name]
        if name == "wing-named-in-other-wing":
            cands = fn(memories, wings)
        else:
            cands = fn(memories)
        print(f"# {name}: {len(cands)} candidates", file=sys.stderr)
        all_cands.extend(cands)

    if args.summary:
        by_signal = {}
        by_wing = {}
        for c in all_cands:
            by_signal[c["signal"]] = by_signal.get(c["signal"], 0) + 1
            by_wing[c["current_wing"]] = by_wing.get(c["current_wing"], 0) + 1
        print(json.dumps({
            "total_candidates": len(all_cands),
            "by_signal": by_signal,
            "top_wings": dict(sorted(by_wing.items(), key=lambda x: -x[1])[:10]),
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
