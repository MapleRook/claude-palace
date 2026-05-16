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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add scripts dir for palace import
sys.path.insert(0, str(Path(__file__).parent))

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Verified ingestion (E) ────────────────────────────────────────────────────
# One budget-capped Haiku pass per uncurated session beats regex fragments.
# Cost is real and per-session — hard-capped, kill-switchable, and it always
# degrades to the zero-cost regex path on any failure.
CLAUDE_EXE = Path.home() / ".local" / "bin" / "claude.exe"
DIGEST_LLM_BUDGET = 0.05  # hard USD cap per session digest call
DIGEST_LLM_OFF = Path.home() / ".claude" / "palace" / "digest_llm.off"
_HALLS = {"user", "feedback", "project", "reference", "task_context"}


def llm_extract(transcript_text: str):
    """One budgeted Haiku pass extracting durable memories as JSON.
    Returns list[{hall,room,content}], or None on any failure/disable so
    the caller falls back to regex. Cost bounded by --max-budget-usd."""
    if DIGEST_LLM_OFF.exists() or not CLAUDE_EXE.exists():
        return None
    prompt = (
        "Extract durable cross-session memories from this coding session "
        "transcript. Output ONLY a JSON array (no prose, no code fences) of "
        '{"hall","room","content"} objects. hall is one of: user, feedback, '
        "project, reference, task_context. room is a short kebab-case topic. "
        "content is ONE specific durable fact, decision, correction, or "
        "technical detail, stated plainly, no fluff. Skip transient chatter "
        "and anything not worth recalling next session. Max 6 items. If "
        "nothing durable, output []\n\n--- TRANSCRIPT ---\n"
        + transcript_text[:12000]
    )
    cmd = [str(CLAUDE_EXE), "--print", "--model", "haiku",
           "--output-format", "json", "--max-budget-usd", str(DIGEST_LLM_BUDGET),
           "-p", prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return None
        outer = json.loads(r.stdout.strip())
        text = (outer.get("result", "") if isinstance(outer, dict) else "").strip()
        if "[" not in text or "]" not in text:
            return None
        arr = json.loads(text[text.find("["): text.rfind("]") + 1])
        out = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            hall = str(it.get("hall", "")).strip()
            room = str(it.get("room", "")).strip()
            content = str(it.get("content", "")).strip()
            if hall in _HALLS and room and len(content) >= 20:
                out.append({"hall": hall, "room": room[:60], "content": content})
        return out[:6]
    except Exception:
        return None


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

    # Claude saved nothing. Verified ingestion (E): one budget-capped
    # Haiku pass extracts durable memories at confidence 0.7 (visible,
    # better than regex). On any failure/disable, fall back to the
    # zero-cost regex path, filed quarantined so it never pollutes recall.
    llm = llm_extract(transcript_text)
    if llm:
        stored = 0
        for d in llm:
            if add_memory(
                wing=wing, hall=d["hall"], room=d["room"], content=d["content"],
                source_file=f"session:{session_id}",
                added_by="digest-haiku", confidence=0.7,
            ).get("success"):
                stored += 1
        if stored:
            print(f"Palace digest: {stored} Haiku-verified memories for {wing}",
                  file=sys.stderr)
        return

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
              f"(LLM verify unavailable)", file=sys.stderr)


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
