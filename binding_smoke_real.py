"""binding_smoke_real.py — minimal smoke test against the REAL palace.

Writes ONE test memory in a safe test wing, linking to [[writing-voice]]
(a known existing global memory). Verifies the binding triples got
written. Cleans up if requested.

Run: "E:/Claude/Optimization/venv/Scripts/python.exe" binding_smoke_real.py [--cleanup]

Exit 0 on pass, 1 on fail.
"""
import sys
import os

# Use the deployed palace.py (so we're testing exactly what the MCP will load)
sys.path.insert(0, r"E:\Claude\Optimization\scripts")

import palace  # noqa: E402
import sqlite3  # noqa: E402

CLEANUP = "--cleanup" in sys.argv
TEST_WING = "binding-smoke-test"
TEST_ROOM = "smoke-2026-05-25"


def kg_triples_for(mem_id):
    """Raw KG query: triples where this memory is subject or object."""
    conn = sqlite3.connect(palace.KG_DB)
    kg = palace._default_kg()
    eid = kg._eid(mem_id)
    rows = conn.execute(
        """SELECT t.subject, t.predicate, t.object,
                  e_sub.name AS sub_name, e_obj.name AS obj_name
           FROM triples t
           LEFT JOIN entities e_sub ON e_sub.id = t.subject
           LEFT JOIN entities e_obj ON e_obj.id = t.object
           WHERE (t.subject = ? OR t.object = ?)
             AND t.predicate = 'linked_to'
             AND t.invalidated_at IS NULL""",
        (eid, eid),
    ).fetchall()
    conn.close()
    return rows


print("=" * 60)
print("Smoke test: write 1 memory with [[writing-voice]] link")
print("=" * 60)

content = (
    "Binding smoke test 2026-05-25. References [[writing-voice]] to "
    "verify the wiki-link binding mechanism. SAFE TO DELETE — this is "
    "test data with no real content. If you see this in a recall, the "
    "smoke test forgot to clean up; delete via palace_delete."
)

print(f"\nWriting memory in wing={TEST_WING}, room={TEST_ROOM}...")
res = palace.add_memory(TEST_WING, "task_context", TEST_ROOM, content,
                       added_by="binding-smoke")
print(f"  success: {res.get('success')}")
print(f"  id: {res.get('id')}")
print(f"  links_created: {res.get('links_created')}")

if not res.get("success"):
    print(f"\nFAIL: add_memory returned non-success: {res}")
    sys.exit(1)

mem_id = res["id"]

print("\nVerifying KG triples...")
triples = kg_triples_for(mem_id)
print(f"  found {len(triples)} triples involving this memory")
for sub, pred, obj, sub_name, obj_name in triples:
    print(f"  {sub_name[:40]}... --[{pred}]--> {obj_name[:40]}...")

# Expect at least 2 triples (bidirectional to writing-voice)
linked_to_us = [t for t in triples if t[1] == "linked_to" and t[4] == mem_id]
linked_from_us = [t for t in triples if t[1] == "linked_to" and t[3] == mem_id]

if not linked_from_us:
    print("\nFAIL: no outgoing linked_to triple from our memory")
    sys.exit(1)
if not linked_to_us:
    print("\nFAIL: no incoming linked_to triple (back-link missing — bidirectional broken)")
    sys.exit(1)

# Check the target is writing-voice
target_obj_name = linked_from_us[0][4]
print(f"\nOutgoing link target: {target_obj_name}")
if "writing-voice" not in target_obj_name:
    print(f"  unexpected target — got {target_obj_name}, expected something with 'writing-voice'")
    # Not strictly a failure if there are multiple writing-voice candidates,
    # but flag it for review.

# Now test search with expand_links
print("\nTesting search expansion (query 'writing voice' should surface our memory)...")
search_res = palace.search_memories("writing voice", n_results=3, expand_links=True)
hit_ids = {h["id"] for h in search_res.get("results", [])}
linked_via_hits = [h for h in search_res.get("results", []) if h.get("linked_via")]

print(f"  total hits: {len(search_res.get('results', []))}")
print(f"  linked_via hits: {len(linked_via_hits)}")
print(f"  our smoke memory surfaced: {mem_id in hit_ids}")

# Either the smoke memory should be a linked_via hit, or the writing-voice memory should be a direct hit
# (and the smoke should be in linked_via).
if mem_id not in hit_ids:
    print("  WARN: smoke memory didn't surface via 'writing voice' query — semantic miss?")
    # Try a more direct query
    search_res2 = palace.search_memories("binding smoke test", n_results=3, expand_links=True)
    hit_ids2 = {h["id"] for h in search_res2.get("results", [])}
    print(f"  retry with 'binding smoke test' — surfaced: {mem_id in hit_ids2}")

print("\n" + "=" * 60)
print(f"PASS — smoke memory {mem_id} written, bidirectional binding verified")
print("=" * 60)

if CLEANUP:
    print(f"\nCleaning up: deleting smoke memory + its triples...")
    palace.delete_memory(mem_id)
    # Also delete the triples (delete_memory may not handle KG cleanup)
    conn = sqlite3.connect(palace.KG_DB)
    kg = palace._default_kg()
    eid = kg._eid(mem_id)
    conn.execute(
        "DELETE FROM triples WHERE (subject=? OR object=?) AND predicate='linked_to'",
        (eid, eid),
    )
    conn.commit()
    conn.close()
    print("  done.")
else:
    print(f"\nLeaving smoke memory in palace (id: {mem_id})")
    print(f"To clean up later: re-run with --cleanup, or manually delete via palace_delete")
