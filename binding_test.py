"""binding_test.py — closed-environment test for palace memory binding.

Tests the [[room-name]] linking mechanism WITHOUT touching real palace.
Uses a tempdir sqlite3 with a minimal memories+triples schema. Mocks
chroma similarity with deterministic substring matching so we're testing
the binding logic, not the embedding model.

What the binding does:
  - At write: parse [[room-name]] from content, resolve to existing memory
    ids by room match, write bidirectional KG triples (linked_to predicate).
  - At read: after normal search returns top-k, expand via linked_to triples
    to fetch any linked memories that didn't make the cut. Tag them as
    `linked_via: <source_id>` so caller knows why.

Run: python binding_test.py
Exit 0 if all pass, 1 otherwise.
"""
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime

LINK_RE = re.compile(r"\[\[([a-z0-9_-]+)\]\]")


class TestPalace:
    """Minimal palace simulation: prose memories + KG triples in one sqlite."""

    def __init__(self, dbpath):
        self.conn = sqlite3.connect(dbpath, autocommit=True)
        self.conn.executescript("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                wing TEXT, hall TEXT, room TEXT,
                content TEXT, filed_at TEXT,
                current INTEGER DEFAULT 1
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT, predicate TEXT, object TEXT,
                valid_from TEXT,
                invalidated_at TEXT
            );
            CREATE INDEX idx_triples_subject ON triples(subject, predicate);
            CREATE INDEX idx_triples_object ON triples(object, predicate);
            CREATE INDEX idx_memories_room ON memories(room, current);
        """)

    def _resolve_room(self, room, prefer_wing=None):
        """Find current memory ids matching this room. Prefer same wing."""
        if prefer_wing:
            rows = self.conn.execute(
                "SELECT id FROM memories WHERE room=? AND wing=? AND current=1",
                (room, prefer_wing),
            ).fetchall()
            if rows:
                return [r[0] for r in rows]
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE room=? AND current=1",
            (room,),
        ).fetchall()
        return [r[0] for r in rows]

    def _new_id(self, wing, room, content):
        seed = (content[:50] + datetime.now().isoformat()).encode()
        return f"mem_{wing}_{room}_{hashlib.md5(seed).hexdigest()[:12]}"

    def _add_triple(self, subj, pred, obj):
        tid = hashlib.md5(f"{subj}|{pred}|{obj}".encode()).hexdigest()[:16]
        try:
            self.conn.execute(
                "INSERT INTO triples (id,subject,predicate,object,valid_from) VALUES (?,?,?,?,?)",
                (tid, subj, pred, obj, datetime.now().strftime("%Y-%m-%d")),
            )
        except sqlite3.IntegrityError:
            pass  # already exists

    def add(self, wing, hall, room, content):
        mem_id = self._new_id(wing, room, content)
        self.conn.execute(
            "INSERT INTO memories (id,wing,hall,room,content,filed_at) VALUES (?,?,?,?,?,?)",
            (mem_id, wing, hall, room, content, datetime.now().isoformat()),
        )
        # Parse [[room]] tokens, resolve, link bidirectionally.
        for target_room in set(LINK_RE.findall(content)):
            for target_id in self._resolve_room(target_room, prefer_wing=wing):
                if target_id == mem_id:
                    continue
                self._add_triple(mem_id, "linked_to", target_id)
                self._add_triple(target_id, "linked_to", mem_id)
        return mem_id

    def linked_ids(self, mem_id):
        """Return ids that this memory points to via linked_to."""
        rows = self.conn.execute(
            "SELECT DISTINCT object FROM triples WHERE subject=? AND predicate='linked_to' AND invalidated_at IS NULL",
            (mem_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def search(self, query, top_k=3, expand_links=True):
        """Mock chroma: substring match scored by occurrence count."""
        rows = self.conn.execute(
            "SELECT id, content FROM memories WHERE current=1"
        ).fetchall()
        scored = []
        ql = query.lower()
        for mid, content in rows:
            score = content.lower().count(ql)
            if score > 0:
                scored.append((mid, score, content, None))
        scored.sort(key=lambda x: -x[1])
        results = list(scored[:top_k])
        seen = {r[0] for r in results}

        if expand_links:
            # First pass: collect all linked ids from direct hits.
            for direct_id, _, _, _ in list(results):
                for lid in self.linked_ids(direct_id):
                    if lid in seen:
                        continue
                    row = self.conn.execute(
                        "SELECT id, content FROM memories WHERE id=? AND current=1",
                        (lid,),
                    ).fetchone()
                    if row:
                        results.append((row[0], 0, row[1], f"linked_via:{direct_id}"))
                        seen.add(lid)
        return results


# ─── Tests ────────────────────────────────────────────────────────────────

def run_tests():
    passed = 0
    failed = 0
    fails = []

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            fails.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "test.sqlite3")
        p = TestPalace(db)

        # Setup: A standalone, B and C both link to [[room-a]].
        a_id = p.add("test", "feedback", "room-a", "Memory A about coffee brewing.")
        b_id = p.add("test", "feedback", "room-b", "Memory B about tea but see [[room-a]] for ratios.")
        c_id = p.add("test", "feedback", "room-c", "Memory C also tea, references [[room-a]] for water temp.")

        print("\nTest 1: bidirectional binding from later writes")
        a_links = set(p.linked_ids(a_id))
        check("A links to B (back-link)", b_id in a_links, f"got {a_links}")
        check("A links to C (back-link)", c_id in a_links, f"got {a_links}")

        print("\nTest 2: forward links from B and C")
        check("B links to A", a_id in p.linked_ids(b_id))
        check("C links to A", a_id in p.linked_ids(c_id))

        print("\nTest 3: search for A surfaces linked B and C")
        results = p.search("coffee", top_k=1, expand_links=True)
        rids = {r[0] for r in results}
        check("A directly returned", a_id in rids)
        check("B surfaced via link", b_id in rids, f"got {rids}")
        check("C surfaced via link", c_id in rids, f"got {rids}")
        check("linked results tagged", any(r[3] and r[3].startswith("linked_via:") for r in results),
              "expected at least one linked_via tag")

        print("\nTest 4: search for B surfaces A back")
        results = p.search("tea", top_k=1, expand_links=True)
        rids = {r[0] for r in results}
        check("at least one tea memory returned", len(rids & {b_id, c_id}) >= 1)
        check("A surfaced via link", a_id in rids, f"got {rids}")

        print("\nTest 5: unrelated query returns nothing")
        results = p.search("zebra", top_k=5, expand_links=True)
        check("no false matches", len(results) == 0)

        print("\nTest 6: expand_links=False suppresses link expansion")
        results = p.search("coffee", top_k=1, expand_links=False)
        rids = {r[0] for r in results}
        check("A returned", a_id in rids)
        check("B NOT surfaced (expansion off)", b_id not in rids, f"got {rids}")
        check("C NOT surfaced (expansion off)", c_id not in rids, f"got {rids}")

        print("\nTest 7: duplicate link doesn't duplicate triples")
        # Add a memory referencing [[room-a]] twice in its body.
        d_id = p.add("test", "feedback", "room-d", "Memory D mentions [[room-a]] twice [[room-a]].")
        d_links = p.linked_ids(d_id)
        a_links_after_d = p.linked_ids(a_id)
        check("D links to A exactly once", d_links.count(a_id) == 1, f"got {d_links}")
        check("A back-links to D exactly once", a_links_after_d.count(d_id) == 1)

        print("\nTest 8: link to nonexistent room is a no-op (not an error)")
        e_id = p.add("test", "feedback", "room-e", "Memory E links to [[room-nonexistent]].")
        check("E exists", e_id is not None)
        check("E has no links", p.linked_ids(e_id) == [])

        print("\nTest 9: same-room wing-preference (multiple wings with same room)")
        # Create same room name in a different wing.
        p2_id = p.add("other-wing", "feedback", "room-a", "Other-wing memory A.")
        # Now write a new memory in 'test' wing linking to [[room-a]].
        # Should prefer the original test/room-a (a_id) over other-wing/room-a (p2_id).
        # But will link to BOTH if multiple match — current behavior. Document it.
        f_id = p.add("test", "feedback", "room-f", "Memory F links [[room-a]] from test wing.")
        f_links = set(p.linked_ids(f_id))
        check("F links to test-wing room-a", a_id in f_links,
              f"expected {a_id} in {f_links}")
        check("F does NOT link to other-wing room-a (wing preference)", p2_id not in f_links,
              f"got {f_links}")

        # Release sqlite handle before TemporaryDirectory tries to delete the file (Windows).
        p.conn.close()

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    if fails:
        print("\nFailures:")
        for name, detail in fails:
            print(f"  - {name}: {detail}")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
