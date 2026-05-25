"""palace_migrate_links.py — backfill linked_to KG triples for existing
[[room-name]] references across the entire palace.

Walks every current memory, parses [[room-name]] tokens from the body,
resolves each to memory ids (preferring same-wing matches), and writes
bidirectional linked_to triples. Idempotent — add_triple already dedups.

Default: dry-run (prints proposed triples, no writes).
Pass --commit to actually write.

Requires the deployed palace.py with binding support (in
E:/Claude/Optimization/scripts/palace.py). Only safe to run when the
live MCP server has been restarted with the new code (otherwise the
running MCP holds the chroma sqlite and this script will hang).

Run:
    "E:/Claude/Optimization/venv/Scripts/python.exe" palace_migrate_links.py --dry-run
    "E:/Claude/Optimization/venv/Scripts/python.exe" palace_migrate_links.py --commit
"""
import argparse
import re
import sys

sys.path.insert(0, r"E:\Claude\Optimization\scripts")
import palace  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true",
                    help="Actually write triples (default is dry-run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only first N memories (0 = all)")
    args = ap.parse_args()

    if args.commit:
        print("=" * 60)
        print("COMMIT MODE — triples WILL be written to the live KG")
        print("=" * 60)
    else:
        print("=" * 60)
        print("DRY-RUN — no writes. Pass --commit to actually write.")
        print("=" * 60)

    col = palace._get_collection()
    if not col:
        print("ERROR: no palace collection found")
        sys.exit(1)

    print("\nFetching all current memories...")
    res = col.get(where={"current": {"$eq": True}},
                  include=["documents", "metadatas"])
    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    print(f"  got {len(ids)} current memories")

    if args.limit > 0:
        ids = ids[:args.limit]
        docs = docs[:args.limit]
        metas = metas[:args.limit]
        print(f"  limited to first {args.limit}")

    kg = palace._default_kg()

    stats = {
        "scanned": 0,
        "with_links": 0,
        "tokens_total": 0,
        "tokens_unresolved": 0,
        "triples_proposed": 0,
        "triples_skipped_self": 0,
    }
    by_memory = []

    for mem_id, doc, meta in zip(ids, docs, metas):
        stats["scanned"] += 1
        if not doc:
            continue
        tokens = list(set(palace._LINK_RE.findall(doc)))
        if not tokens:
            continue
        stats["with_links"] += 1
        stats["tokens_total"] += len(tokens)
        wing = meta.get("wing")
        mem_info = {
            "id": mem_id,
            "wing": wing,
            "room": meta.get("room"),
            "tokens": tokens,
            "links": [],  # [{token, resolved_ids: [...]}, ...]
        }
        for token in tokens:
            target_ids = palace._resolve_link_targets([token], prefer_wing=wing)
            target_ids = [tid for tid in target_ids if tid != mem_id]
            if not target_ids:
                stats["tokens_unresolved"] += 1
            for tid in target_ids:
                stats["triples_proposed"] += 2  # bidirectional
            mem_info["links"].append({"token": token, "resolved_ids": target_ids})
        by_memory.append(mem_info)

    # Report
    print(f"\nScanned: {stats['scanned']}")
    print(f"  with [[link]] tokens: {stats['with_links']}")
    print(f"  total tokens: {stats['tokens_total']}")
    print(f"  unresolved tokens: {stats['tokens_unresolved']}")
    print(f"  triples proposed (bidirectional pairs counted twice): {stats['triples_proposed']}")

    print("\nFirst 20 memories with links:")
    for mem in by_memory[:20]:
        print(f"  [{mem['wing']}/{mem['room']}]")
        for link in mem["links"]:
            if link["resolved_ids"]:
                print(f"    [[{link['token']}]] -> {len(link['resolved_ids'])} target(s)")
                for tid in link["resolved_ids"][:3]:
                    print(f"      -> {tid}")
                if len(link["resolved_ids"]) > 3:
                    print(f"      ... and {len(link['resolved_ids']) - 3} more")
            else:
                print(f"    [[{link['token']}]] -> UNRESOLVED")

    if len(by_memory) > 20:
        print(f"  ... and {len(by_memory) - 20} more memories with links")

    # Commit
    if args.commit:
        print("\nWriting triples...")
        written = 0
        for mem in by_memory:
            for link in mem["links"]:
                for tid in link["resolved_ids"]:
                    try:
                        kg.add_triple(mem["id"], "linked_to", tid,
                                     confidence=1.0,
                                     source=f"migration:link:{link['token']}")
                        kg.add_triple(tid, "linked_to", mem["id"],
                                     confidence=1.0,
                                     source=f"migration:link:{link['token']}")
                        written += 2
                    except Exception as e:
                        print(f"  ERROR writing triple {mem['id']}->{tid}: {e}")
        print(f"  wrote {written} triples (existing duplicates skipped by add_triple)")
    else:
        print("\n(dry-run — no triples written. re-run with --commit when ready.)")


if __name__ == "__main__":
    main()
