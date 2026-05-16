#!/usr/bin/env python3
"""
palace_digest.py — Session Digest Hook for Palace
===================================================

Runs on PreCompact (before context is lost) and Stop (session end).
Reads the conversation transcript, extracts discoveries/decisions/corrections,
and stores them in the palace via ChromaDB.

Hook input (stdin JSON):
  { session_id, transcript_path, cwd, hook_event_name, ... }

Usage in settings.json hooks:
  PreCompact/Stop hook → python palace_digest.py
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Add scripts dir for palace import
sys.path.insert(0, str(Path(__file__).parent))

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def detect_wing_from_cwd(cwd: str) -> str:
    """Map cwd to a palace wing name."""
    cwd_lower = cwd.replace("\\", "/").lower()
    mappings = {
        "small_towns_ai": "small-towns-ai",
        "small-towns": "small-towns-ai",
        "/ai/": "small-towns-ai",
        "c--ai": "small-towns-ai",
        "optimization": "optimization",
        "adhdreminders": "adhd-reminders",
        "adhd": "adhd-reminders",
        "musicmanagement": "music-management",
        "semanticsearch": "semantic-search",
        "sidehustle": "side-hustle",
        "contractordraft": "contractor-draft",
        "ticketmachine": "ticket-machine",
        "roadquality": "road-quality",
        "ac7_decoded": "code-ac7-decoded",
        "ac7_asymmetrical": "code-ac7-asymmetrical",
        "ac7_mapfix": "code-ac7-mapfix",
        "ac7_nausicaa": "code-ac7-nausicaa",
        "ac7_nocheatmods": "code-ac7-nocheatmods",
        "ac7": "code-ac7",
        "sw5e": "code-sw5e",
        "aheadofthegame": "aheadofthegame",
        "codelearning": "codelearning",
        "claudemanager": "claude-manager",
    }
    for pattern, wing in mappings.items():
        if pattern in cwd_lower:
            return wing
    # Fallback: extract last dir component
    name = Path(cwd).name.lower().replace(" ", "-")
    return name or "unknown"


def extract_last_n_messages(transcript_path: str, n: int = 40) -> str:
    """Read the last N messages from a Claude Code transcript file."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""

    # Transcript is JSONL — each line is a message object
    messages = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            messages.append(msg)
        except json.JSONDecodeError:
            continue

    # Take last N messages
    recent = messages[-n:]

    # Extract text content
    parts = []
    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        # Skip large tool results, just note what tool was used
                        text_parts.append(f"[tool_result]")
                elif isinstance(block, str):
                    text_parts.append(block)
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if text.strip():
            parts.append(f"[{role}]: {text[:2000]}")  # Cap per-message length

    return "\n\n".join(parts[-30:])  # Cap total messages


def extract_discoveries(transcript_text: str, wing: str) -> list:
    """Extract structured discoveries from transcript text using heuristics.

    Returns list of dicts: { hall, room, content }
    No LLM call — pure pattern matching for speed and zero cost.
    """
    discoveries = []

    # Patterns that indicate discoveries/decisions/corrections
    patterns = {
        "feedback": [
            (r"(?:don't|never|stop|avoid|wrong|incorrect|shouldn't)\s+(.{20,200})", "correction"),
            (r"(?:always|prefer|instead|better to|should always)\s+(.{20,200})", "preference"),
        ],
        "project": [
            (r"(?:decided|decision|chose|went with|picked|selected)\s+(.{20,200})", "decision"),
            (r"(?:discovered|found out|turns out|realized|learned|TIL)\s+(.{20,200})", "discovery"),
            (r"(?:fixed|resolved|root cause|the issue was|bug was)\s+(.{20,200})", "fix"),
            (r"(?:deployed|shipped|released|launched|went live)\s+(.{20,200})", "milestone"),
        ],
        "task_context": [
            (r"(?:workaround|hack|trick|gotcha|caveat|warning)\s*:?\s*(.{20,200})", "workaround"),
            (r"(?:the API|the SDK|the endpoint|the format)\s+(?:requires|needs|expects|uses)\s+(.{20,200})", "api-detail"),
        ],
    }

    for hall, pattern_list in patterns.items():
        for regex, room in pattern_list:
            matches = re.findall(regex, transcript_text, re.IGNORECASE)
            for match in matches[:3]:  # Cap at 3 per pattern to avoid noise
                content = match.strip()
                # Skip fragments: too short, too few words, or code-like
                if len(content) < 40 or len(content.split()) < 5 or content.count("{") > 2:
                    continue
                discoveries.append({
                    "hall": hall,
                    "room": room,
                    "content": content,
                })

    return discoveries


def session_used_palace(transcript_text: str) -> bool:
    """Check if Claude called palace_add or palace_kg_add during this session."""
    palace_patterns = [
        r"palace_add",
        r"palace_kg_add",
        r"mcp__palace__palace_add",
        r"mcp__palace__palace_kg_add",
    ]
    for pat in palace_patterns:
        if re.search(pat, transcript_text):
            return True
    return False


def run_digest(hook_input: dict):
    """Main digest logic."""
    from palace import add_memory, canonicalize_wing

    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "")
    event = hook_input.get("hook_event_name", "")

    if not transcript_path or not os.path.exists(transcript_path):
        return

    wing = canonicalize_wing(detect_wing_from_cwd(cwd))
    transcript_text = extract_last_n_messages(transcript_path)

    if not transcript_text or len(transcript_text) < 100:
        return

    # If Claude used palace tools this session it already curated the
    # memories worth keeping. Regex extraction on top of a curated session
    # only adds noise — skip entirely.
    if session_used_palace(transcript_text):
        return

    # Claude saved nothing. Fall back to regex extraction, but file it as
    # unverified so it never pollutes first-class rooms. A custodian or a
    # later session can promote anything that turns out to matter.
    discoveries = extract_discoveries(transcript_text, wing)
    if len(transcript_text) > 1000:
        wider = extract_discoveries(extract_last_n_messages(transcript_path, n=80), wing)
        seen = {d["content"] for d in discoveries}
        for wd in wider:
            if wd["content"] not in seen:
                discoveries.append(wd)
                seen.add(wd["content"])

    stored = 0
    for disc in discoveries:
        result = add_memory(
            wing=wing,
            hall=disc["hall"],
            room="digest-unverified",
            content=f"[unverified digest extract] {disc['content']}",
            source_file=f"session:{session_id}",
            added_by="digest-hook",
        )
        if result.get("success"):
            stored += 1

    if stored > 0:
        print(f"Palace digest: {stored} unverified extracts filed for {wing} "
              f"(no palace tools used this session)", file=sys.stderr)


def main():
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        return

    run_digest(hook_input)


if __name__ == "__main__":
    main()
