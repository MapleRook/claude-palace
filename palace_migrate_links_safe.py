"""palace_migrate_links_safe.py — like palace_migrate_links.py but coexists
with a running palace MCP server.

The original hangs because chromadb's PersistentClient is not multi-process
safe (see palace.py:91). This variant bypasses chromadb entirely:

  * READS go straight to chroma.sqlite3 in read-only mode (mode=ro), so
    they cannot contend with the live MCP's PersistentClient.
  * WRITES go through palace's KnowledgeGraph, which uses sqlite WAL +
    busy_timeout — concurrent writers from the MCP and this script
    serialize cleanly without corrupting state.

Default: dry-run. Pass --commit to actually write.

Run:
    "E:/Claude/Optimization/venv/Scripts/python.exe" palace_migrate_links_safe.py
    "E:/Claude/Optimization/venv/Scripts/python.exe" palace_migrate_links_safe.py --commit
"""
import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, r"E:\Claude\Optimization\scripts")
import palace  # noqa: E402

CHROMA_DB = os.path.join(palace.PALACE_DB, "chroma.sqlite3")
LINK_RE = palace._LINK_RE


def open_chroma_ro():
    uri = f"file:{CHROMA_DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_current_memories(conn):
    """Return list of {id, wing, room, document} for memories where
    current=True (or the legacy case where the key is absent — treat
    absent as not-current per palace's read-gate semantics).

    We pivot embedding_metadata (one row per key) into per-memory dicts.
    """
    rows = conn.execute(
        "SELECT e.embedding_id AS eid, m.key, m.string_value, m.bool_value "
        "FROM embeddings e "
        "JOIN embedding_metadata m ON m.id = e.id "
        "WHERE m.key IN ('current','room','wing','chroma:document')"
    ).fetchall()

    by_id = {}
    for r in rows:
        d = by_id.setdefault(r["eid"], {})
        k = r["key"]
        if k == "chroma:document":
            d["document"] = r["string_value"]
        elif k == "current":
            d["current"] = bool(r["bool_value"]) if r["bool_value"] is not None else False
        elif k == "room":
            d["room"] = r["string_value"]
        elif k == "wing":
            d["wing"] = r["string_value"]

    out = []
    for eid, d in by_id.items():
        if not d.get("current"):
            continue
        out.append({
            "id": eid,
            "wing": d.get("wing"),
            "room": d.get("room"),
            "document": d.get("document") or "",
        })
    return out


def build_room_index(memories):
    """room -> {wing: [ids], None: [ids]} for resolution."""
    idx = {}
    for m in memories:
        room = m["room"]
        if not room:
            continue
        idx.setdefault(room, {}).setdefault(m["wing"], []).append(m["id"])
    return idx


def resolve(room, prefer_wing, idx, exclude_id):
    bucket = idx.get(room)
    if not bucket:
        return []
    ids = []
    if prefer_wing and prefer_wing in bucket:
        ids.extend(bucket[prefer_wing])
    # Fall back to other wings only when same-wing was empty
    if not ids:
        for w, lst in bucket.items():
            ids.extend(lst)
    return [i for i in ids if i != exclude_id]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true",
                    help="Actually write triples (default is dry-run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only first N linked memories (0 = all)")
    args = ap.parse_args()

    banner = "COMMIT MODE — triples WILL be written" if args.commit \
        else "DRY-RUN — no writes. Pass --commit to actually write."
    print("=" * 60)
    print(banner)
    print("=" * 60)

    if not os.path.exists(CHROMA_DB):
        print(f"ERROR: chroma sqlite not found at {CHROMA_DB}")
        sys.exit(1)

    print(f"\nReading chroma sqlite (read-only): {CHROMA_DB}")
    conn = open_chroma_ro()
    memories = load_current_memories(conn)
    conn.close()
    print(f"  loaded {len(memories)} current memories")

    idx = build_room_index(memories)
    print(f"  indexed {len(idx)} distinct rooms")

    # Find memories that actually contain [[link]] tokens.
    with_links = []
    for m in memories:
        tokens = sorted(set(LINK_RE.findall(m["document"])))
        if tokens:
            with_links.append((m, tokens))

    if args.limit > 0:
        with_links = with_links[: args.limit]

    stats = {
        "with_links": len(with_links),
        "tokens_total": 0,
        "tokens_unresolved": 0,
        "triples_proposed": 0,  # bidirectional pairs counted twice
    }
    plan = []  # [(src_id, src_wing, src_room, token, [tgt_ids]), ...]
    for m, tokens in with_links:
        stats["tokens_total"] += len(tokens)
        for tok in tokens:
            tgts = resolve(tok, m["wing"], idx, m["id"])
            if not tgts:
                stats["tokens_unresolved"] += 1
            stats["triples_proposed"] += 2 * len(tgts)
            plan.append((m["id"], m["wing"], m["room"], tok, tgts))

    print(f"\nMemories with [[link]] tokens : {stats['with_links']}")
    print(f"  total tokens                : {stats['tokens_total']}")
    print(f"  unresolved tokens           : {stats['tokens_unresolved']}")
    print(f"  triples proposed (bidir x2) : {stats['triples_proposed']}")

    print("\nSample (first 15 link resolutions):")
    for src_id, wing, room, tok, tgts in plan[:15]:
        if tgts:
            head = f"  [{wing}/{room}] -- [[{tok}]] --> {len(tgts)} target(s)"
            print(head)
            for t in tgts[:3]:
                print(f"      -> {t}")
            if len(tgts) > 3:
                print(f"      ... +{len(tgts) - 3} more")
        else:
            print(f"  [{wing}/{room}] -- [[{tok}]] --> UNRESOLVED")

    if not args.commit:
        print("\n(dry-run — no triples written. re-run with --commit to apply.)")
        return

    print("\nWriting triples via KnowledgeGraph (WAL, busy_timeout=8s)...")
    kg = palace.KnowledgeGraph()
    written = 0
    errors = 0
    for src_id, wing, room, tok, tgts in plan:
        for tid in tgts:
            try:
                kg.add_triple(src_id, "linked_to", tid,
                              confidence=1.0,
                              source=f"migration:link:{tok}")
                kg.add_triple(tid, "linked_to", src_id,
                              confidence=1.0,
                              source=f"migration:link:{tok}")
                written += 2
            except Exception as e:
                errors += 1
                print(f"  ERROR {src_id} -> {tid}: {e}", file=sys.stderr)
    print(f"\nDone. wrote {written} triples (existing duplicates skipped). errors={errors}")


if __name__ == "__main__":
    main()
