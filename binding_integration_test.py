"""binding_integration_test.py — verify Step 1 + Step 2 against the REAL
palace.py module (with chromadb), but using a tempdir palace so the user's
actual ~/.claude/palace is untouched.

Run: python binding_integration_test.py
Exit 0 on pass, 1 on fail.
"""
import os
import sys
import tempfile

# Build a tempdir-rooted palace BEFORE importing palace.py so module
# constants pick up the override.
TEMP_ROOT = tempfile.mkdtemp(prefix="palace_binding_test_")
os.environ["TEST_PALACE_ROOT"] = TEMP_ROOT

import palace  # noqa: E402

# Monkey-patch path constants. palace.py caches clients keyed by path
# (_CHROMA_CLIENTS dict + _DEFAULT_KG dict), so changing the path is
# enough — new clients spawn for the new paths automatically.
palace.PALACE_DIR = TEMP_ROOT
palace.PALACE_DB = os.path.join(TEMP_ROOT, "chroma")
palace.KG_DB = os.path.join(TEMP_ROOT, "kg.sqlite3")
palace.IDENTITY_FILE = os.path.join(TEMP_ROOT, "identity.txt")
# _LOCK_FILE is computed at module-load from PALACE_DIR — override too,
# otherwise the test tries to grab the production lock the live MCP holds.
palace._LOCK_FILE = os.path.join(TEMP_ROOT, ".chroma.lock")

print(f"Using temp palace at: {TEMP_ROOT}\n")

passed = 0
failed = 0
fails = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        fails.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


# Setup: write three memories. A is standalone, B and C reference [[room-a]].
print("Setup: writing 3 memories with [[room-a]] cross-links")
a = palace.add_memory("test-binding", "feedback", "room-a",
                     "Memory A about coffee brewing methods.",
                     added_by="test")
print(f"  A id: {a.get('id')}, links_created: {a.get('links_created')}")

b = palace.add_memory("test-binding", "feedback", "room-b",
                     "Memory B about tea brewing, but see [[room-a]] for ratios.",
                     added_by="test")
print(f"  B id: {b.get('id')}, links_created: {b.get('links_created')}")

c = palace.add_memory("test-binding", "feedback", "room-c",
                     "Memory C also about tea, references [[room-a]] for water temperature.",
                     added_by="test")
print(f"  C id: {c.get('id')}, links_created: {c.get('links_created')}")

a_id, b_id, c_id = a["id"], b["id"], c["id"]

print("\nTest 1: B and C report linking to A at write time")
check("B linked to A", a_id in (b.get("links_created") or []),
      f"got {b.get('links_created')}")
check("C linked to A", a_id in (c.get("links_created") or []),
      f"got {c.get('links_created')}")

print("\nTest 2: KG triples exist (bidirectional)")
linked_from_b = palace._linked_ids_for([b_id]).get(b_id, [])
linked_from_a = palace._linked_ids_for([a_id]).get(a_id, [])
check("B → A triple", a_id in linked_from_b, f"got {linked_from_b}")
check("A → B triple (back-link)", b_id in linked_from_a, f"got {linked_from_a}")
check("A → C triple (back-link)", c_id in linked_from_a, f"got {linked_from_a}")

print("\nTest 3: search for 'coffee' surfaces A directly + B and C via link")
res = palace.search_memories("coffee brewing methods", wing="test-binding",
                              n_results=1, expand_links=True)
rids = {r["id"] for r in res["results"]}
linked_via_rows = [r for r in res["results"] if r.get("linked_via")]
check("A directly returned", a_id in rids, f"got {rids}")
check("B surfaced via link", b_id in rids, f"got {rids}")
check("C surfaced via link", c_id in rids, f"got {rids}")
check("linked rows tagged with linked_via", len(linked_via_rows) >= 2,
      f"got {len(linked_via_rows)} tagged rows")

print("\nTest 4: expand_links=False suppresses link expansion")
res = palace.search_memories("coffee brewing methods", wing="test-binding",
                              n_results=1, expand_links=False)
rids = {r["id"] for r in res["results"]}
check("A still returned", a_id in rids)
check("B NOT surfaced (expansion off)", b_id not in rids, f"got {rids}")
check("C NOT surfaced (expansion off)", c_id not in rids, f"got {rids}")

print("\nTest 5: link to nonexistent room is a no-op (memory still writes)")
d = palace.add_memory("test-binding", "feedback", "room-d",
                     "Memory D mentions [[nonexistent-room]] and nothing else.",
                     added_by="test")
check("D writes successfully", d.get("success") is True)
check("D has empty links_created", d.get("links_created") == [],
      f"got {d.get('links_created')}")

print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed")
print(f"  temp palace: {TEMP_ROOT}  (manual cleanup if needed)")
if fails:
    print("\nFailures:")
    for name, detail in fails:
        print(f"  - {name}: {detail}")
sys.exit(0 if failed == 0 else 1)
