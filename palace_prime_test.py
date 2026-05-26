"""palace_prime_test.py — unit-test the no-CLAUDE.md/no-wing nudge.

Stubs the palace module so we don't need chromadb. Tests check_unknown_project
across the relevant branches.
"""
import os
import sys
import tempfile

# Stub palace module BEFORE palace_prime imports it.
class MockPalaceModule:
    @staticmethod
    def canonicalize_wing(w):
        return (w or "unknown").lower().strip("-")
sys.modules["palace"] = MockPalaceModule

# Now import palace_prime — its `from palace import ...` will use the stub.
from palace_prime import check_unknown_project


class MockCol:
    def __init__(self, has_match):
        self.has_match = has_match

    def get(self, where, limit):
        return {"ids": ["fake-id"] if self.has_match else []}


def gc(col):
    return lambda: col


def main():
    passed = 0
    failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    # CABINTools has CLAUDE.md (we wrote it earlier this session) → no nudge.
    r = check_unknown_project(r"E:/Claude/CABINTools", "cabin-tools", gc(MockCol(False)))
    check("CABINTools (has CLAUDE.md) returns None", r is None, f"got: {(r or '')[:80]}")

    # claude-palace has palace wing populated (mock has_match=True) → no nudge.
    r = check_unknown_project(r"E:/Claude/claude-palace", "claude-palace", gc(MockCol(True)))
    check("wing populated returns None", r is None, f"got: {(r or '')[:80]}")

    # Bogus dir, empty wing → nudge fires.
    r = check_unknown_project(r"E:/Claude/THIS_DOES_NOT_EXIST_xyz_123",
                              "some-new-wing", gc(MockCol(False)))
    check("blank slate fires nudge", r and "blank slate" in r.lower(),
          f"got: {str(r)[:200]}")

    # Empty cwd → defensive None.
    r = check_unknown_project("", "some-wing", gc(MockCol(False)))
    check("empty cwd defensive None", r is None)

    # cwd with .claude/CLAUDE.md instead of root CLAUDE.md → no nudge.
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, ".claude"))
        with open(os.path.join(td, ".claude", "CLAUDE.md"), "w") as f:
            f.write("# test")
        r = check_unknown_project(td, "temp-wing", gc(MockCol(False)))
        check(".claude/CLAUDE.md counts as CLAUDE.md", r is None,
              f"got: {(r or '')[:80]}")

    # Collection returns None (palace not init) → defensive None.
    r = check_unknown_project(r"E:/Claude/THIS_DOES_NOT_EXIST_xyz_123",
                              "some-wing", lambda: None)
    check("no collection defensive None", r is None)

    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
