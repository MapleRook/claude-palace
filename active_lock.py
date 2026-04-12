#!/usr/bin/env python3
"""
active_lock.py — Token conservation lockfile for background agents
==================================================================

Prevents background Claude agents (custodians, batch jobs) from running
while the user is actively working. Uses a simple lockfile approach.

Usage:
    # In your active session (hook or manual):
    python active_lock.py acquire          # Create lockfile
    python active_lock.py release          # Remove lockfile
    python active_lock.py check            # Exit 0 if locked, 1 if free
    python active_lock.py guard <command>  # Run command only if unlocked

    # From Python:
    from active_lock import is_active, acquire_lock, release_lock
"""

import os
import sys
import time
import json
import subprocess
from pathlib import Path

LOCK_FILE = Path.home() / ".claude" / "palace" / ".active_lock"
LOCK_TIMEOUT = 3600  # 1 hour — stale lock auto-expires


def acquire_lock(reason: str = "interactive session") -> bool:
    """Create the lockfile. Returns True if acquired."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "timestamp": time.time(),
        "reason": reason,
    }
    LOCK_FILE.write_text(json.dumps(payload), encoding="utf-8")
    return True


def release_lock() -> bool:
    """Remove the lockfile. Returns True if released."""
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()
        return True
    return False


def is_active() -> bool:
    """Check if an active session lock exists and is not stale."""
    if not LOCK_FILE.exists():
        return False
    try:
        payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        age = time.time() - payload.get("timestamp", 0)
        if age > LOCK_TIMEOUT:
            # Stale lock — clean it up
            LOCK_FILE.unlink()
            return False
        return True
    except (json.JSONDecodeError, OSError):
        return False


def lock_info() -> dict:
    """Get lock details, or empty dict if unlocked."""
    if not is_active():
        return {}
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    if len(sys.argv) < 2:
        print("Usage: active_lock.py {acquire|release|check|guard}")
        sys.exit(2)

    cmd = sys.argv[1].lower()

    if cmd == "acquire":
        reason = " ".join(sys.argv[2:]) or "interactive session"
        acquire_lock(reason)
        print(f"Lock acquired: {reason}")

    elif cmd == "release":
        if release_lock():
            print("Lock released")
        else:
            print("No lock to release")

    elif cmd == "check":
        if is_active():
            info = lock_info()
            print(f"LOCKED — {info.get('reason', '?')} (age: {int(time.time() - info.get('timestamp', 0))}s)")
            sys.exit(0)
        else:
            print("FREE")
            sys.exit(1)

    elif cmd == "guard":
        if is_active():
            print("Active session detected — skipping background work", file=sys.stderr)
            sys.exit(0)
        if len(sys.argv) < 3:
            print("guard requires a command", file=sys.stderr)
            sys.exit(2)
        # Run the guarded command
        result = subprocess.run(sys.argv[2:], shell=False)
        sys.exit(result.returncode)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
