#!/usr/bin/env python3
"""
palace_mcp.py — MCP server exposing memory palace tools to Claude Code
======================================================================

Install:
  claude mcp add palace -- python /path/to/claude-palace/palace_mcp.py

Tools (18 total):

  Read:
    palace_status          — overview: memory counts, wings, halls
    palace_search          — semantic search with wing/hall/room filters
    palace_recall          — on-demand L2 retrieval by wing/room
    palace_wake_up         — L0+L1 context for session start

  Write:
    palace_add             — store a memory (with duplicate check)
    palace_delete          — remove a memory by ID

  Knowledge Graph:
    palace_kg_query        — entity facts with temporal filtering
    palace_kg_add          — add a fact (subject → predicate → object)
    palace_kg_invalidate   — mark a fact as no longer true
    palace_kg_timeline     — chronological entity story
    palace_kg_stats        — graph overview

  Migration:
    palace_migrate         — import existing .md memories
"""

import sys
import json
import logging

# Add scripts dir to path so we can import palace
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from palace import (
    KnowledgeGraph, MemoryStack,
    add_memory, delete_memory, search_memories,
    migrate_existing_memories, onboard_project,
    detect_cross_project_patterns, consolidate_all,
)

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("palace_mcp")

_kg = KnowledgeGraph()
_stack = MemoryStack()


# ── Tool handlers ─────────────────────────────────────────────────────────────

def tool_status():
    return _stack.status()

def tool_search(query: str, limit: int = 5, wing: str = None, hall: str = None, room: str = None, drawer: str = None):
    return search_memories(query, wing=wing, hall=hall, room=room, drawer=drawer, n_results=limit)

def tool_recall(wing: str = None, room: str = None, n_results: int = 10):
    return {"text": _stack.recall(wing=wing, room=room, n_results=n_results)}

def tool_wake_up(wing: str = None):
    return {"text": _stack.wake_up(wing=wing)}

def tool_add(wing: str, hall: str, room: str, content: str,
             source_file: str = None, added_by: str = "claude", drawer: str = None):
    return add_memory(wing, hall, room, content, source_file=source_file, added_by=added_by, drawer=drawer)

def tool_delete(memory_id: str):
    return delete_memory(memory_id)

def tool_kg_query(entity: str, as_of: str = None, direction: str = "both"):
    results = _kg.query_entity(entity, as_of=as_of, direction=direction)
    return {"entity": entity, "as_of": as_of, "facts": results, "count": len(results)}

def tool_kg_add(subject: str, predicate: str, object: str,
                valid_from: str = None, source: str = None):
    triple_id = _kg.add_triple(subject, predicate, object,
                                valid_from=valid_from, source=source)
    return {"success": True, "triple_id": triple_id,
            "fact": f"{subject} -> {predicate} -> {object}"}

def tool_kg_invalidate(subject: str, predicate: str, object: str, ended: str = None):
    _kg.invalidate(subject, predicate, object, ended=ended)
    return {"success": True, "fact": f"{subject} -> {predicate} -> {object}",
            "ended": ended or "today"}

def tool_kg_timeline(entity: str = None):
    results = _kg.timeline(entity)
    return {"entity": entity or "all", "timeline": results, "count": len(results)}

def tool_kg_stats():
    return _kg.stats()

def tool_migrate(dry_run: bool = False):
    return migrate_existing_memories(dry_run=dry_run)

def tool_onboard(project_dir: str, wing: str = None):
    return onboard_project(project_dir, wing=wing)

def tool_patterns():
    return detect_cross_project_patterns()

def tool_consolidate(auto_delete: bool = False):
    return consolidate_all(auto_delete=auto_delete)

def tool_kg_audit(stale_days: int = 30):
    stale = _kg.audit(stale_days=stale_days)
    return {"stale_facts": len(stale), "stale_days_threshold": stale_days,
            "facts": stale[:50]}  # Cap output

def tool_kg_verify(triple_id: str):
    _kg.verify_triple(triple_id)
    return {"success": True, "triple_id": triple_id, "verified_at": "today"}

def tool_complexity_estimate(task_description: str, wing: str = None):
    """Estimate task complexity by palace context density."""
    results = search_memories(task_description, wing=wing, n_results=10)
    hits = results.get("results", [])

    if not hits:
        return {"complexity": "unknown", "reason": "No related memories found",
                "related_memories": 0, "recommendation": "sonnet"}

    high_sim = sum(1 for h in hits if h.get("similarity", 0) > 0.3)
    wings_involved = len(set(h.get("wing", "") for h in hits))
    kg_depth = 0
    for h in hits[:3]:
        text_snippet = h.get("text", "")[:50]
        facts = _kg.query_entity(text_snippet.split()[0] if text_snippet.split() else "", direction="both")
        kg_depth += len(facts)

    if wings_involved >= 3 or kg_depth > 15 or len(hits) >= 10:
        level, model = "high", "opus"
    elif high_sim >= 3 or kg_depth > 5:
        level, model = "medium", "sonnet"
    else:
        level, model = "low", "haiku"

    return {"complexity": level, "recommendation": model,
            "related_memories": len(hits), "high_similarity_hits": high_sim,
            "wings_involved": wings_involved, "kg_connections": kg_depth}


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = {
    "palace_status": {
        "description": "Memory palace overview — total memories, wings (projects), halls (memory types), counts.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    "palace_search": {
        "description": "Semantic search across all memories. Returns verbatim content ranked by similarity. Filter by wing (project), hall (memory type: user/feedback/project/reference/task_context), or room (topic).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
                "wing": {"type": "string", "description": "Filter by project (e.g. 'small-towns-ai')"},
                "hall": {"type": "string", "description": "Filter by memory type (user/feedback/project/reference/task_context)"},
                "room": {"type": "string", "description": "Filter by topic (e.g. 'vertex-ai-search')"},
                "drawer": {"type": "string", "description": "Filter by sub-topic within room (optional, 4th hierarchy level)"},
            },
            "required": ["query"],
        },
        "handler": tool_search,
    },
    "palace_recall": {
        "description": "On-demand retrieval of memories for a specific project or topic. Use when a wing/room comes up in conversation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Project to recall (e.g. 'small-towns-ai')"},
                "room": {"type": "string", "description": "Topic to recall (e.g. 'alger-county')"},
                "n_results": {"type": "integer", "description": "Max results (default 10)"},
            },
        },
        "handler": tool_recall,
    },
    "palace_wake_up": {
        "description": "Load identity + essential memories for session start. Returns L0+L1 context (~200-600 tokens).",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Optional project focus for wake-up"},
            },
        },
        "handler": tool_wake_up,
    },
    "palace_add": {
        "description": "Store a memory in the palace. Checks for duplicates automatically. Wing=project, hall=type (user/feedback/project/reference/task_context), room=topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Project (e.g. 'small-towns-ai', 'optimization')"},
                "hall": {"type": "string", "description": "Memory type: user, feedback, project, reference, or task_context"},
                "room": {"type": "string", "description": "Topic (e.g. 'vertex-ai-search', 'demo-server')"},
                "content": {"type": "string", "description": "Memory content — verbatim, never summarized"},
                "source_file": {"type": "string", "description": "Source file path (optional)"},
                "added_by": {"type": "string", "description": "Who stored this (default: claude)"},
                "drawer": {"type": "string", "description": "Sub-topic within room (optional, 4th hierarchy level)"},
            },
            "required": ["wing", "hall", "room", "content"],
        },
        "handler": tool_add,
    },
    "palace_delete": {
        "description": "Delete a memory by ID. Irreversible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "ID of the memory to delete"},
            },
            "required": ["memory_id"],
        },
        "handler": tool_delete,
    },
    "palace_kg_query": {
        "description": "Query the knowledge graph for an entity's relationships. Returns typed facts with temporal validity. Use as_of to filter by date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity to query (person, project, concept)"},
                "as_of": {"type": "string", "description": "Date filter YYYY-MM-DD (optional)"},
                "direction": {"type": "string", "description": "outgoing, incoming, or both (default: both)"},
            },
            "required": ["entity"],
        },
        "handler": tool_kg_query,
    },
    "palace_kg_add": {
        "description": "Add a fact to the knowledge graph. Subject -> predicate -> object with optional time window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Entity doing/being something"},
                "predicate": {"type": "string", "description": "Relationship (e.g. 'works_on', 'decided', 'uses')"},
                "object": {"type": "string", "description": "Connected entity or value"},
                "valid_from": {"type": "string", "description": "When this became true YYYY-MM-DD (optional)"},
                "source": {"type": "string", "description": "Where this fact came from (optional)"},
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": tool_kg_add,
    },
    "palace_kg_invalidate": {
        "description": "Mark a fact as no longer true (set end date).",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "ended": {"type": "string", "description": "When it stopped being true YYYY-MM-DD (default: today)"},
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": tool_kg_invalidate,
    },
    "palace_kg_timeline": {
        "description": "Chronological timeline of facts. Optionally filter by entity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity to get timeline for (optional)"},
            },
        },
        "handler": tool_kg_timeline,
    },
    "palace_kg_stats": {
        "description": "Knowledge graph overview: entity count, triple count, relationship types.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_kg_stats,
    },
    "palace_migrate": {
        "description": "Import all existing .md memory files from ~/.claude/ into the palace. Run once to bootstrap.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Preview without importing (default: false)"},
            },
        },
        "handler": tool_migrate,
    },
    "palace_onboard": {
        "description": "Auto-scan a project directory and create a palace wing. Detects package.json/pyproject.toml, directory structure, CLAUDE.md, README, git info. Run when entering a new project for the first time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Path to the project directory to scan"},
                "wing": {"type": "string", "description": "Wing name override (default: dir name)"},
            },
            "required": ["project_dir"],
        },
        "handler": tool_onboard,
    },
    "palace_patterns": {
        "description": "Detect cross-project patterns — find memories that appear semantically similar across different wings and promote recurring patterns to global. Self-improving: discovers insights that span projects.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_patterns,
    },
    "palace_consolidate": {
        "description": "Find and optionally remove duplicate/near-duplicate memories across all wings. Self-cleaning: keeps the palace lean.",
        "input_schema": {
            "type": "object",
            "properties": {
                "auto_delete": {"type": "boolean", "description": "Auto-delete near-exact duplicates (default: false, report only)"},
            },
        },
        "handler": tool_consolidate,
    },
    "palace_kg_audit": {
        "description": "Find stale knowledge graph facts that haven't been verified recently. Returns triples needing re-verification. Use for palace maintenance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stale_days": {"type": "integer", "description": "Days since last verification to consider stale (default: 30)"},
            },
        },
        "handler": tool_kg_audit,
    },
    "palace_kg_verify": {
        "description": "Mark a knowledge graph triple as verified (still true). Updates last_verified timestamp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "triple_id": {"type": "string", "description": "ID of the triple to verify"},
            },
            "required": ["triple_id"],
        },
        "handler": tool_kg_verify,
    },
    "palace_complexity_estimate": {
        "description": "Estimate task complexity by checking palace context density. Returns recommended model tier (haiku/sonnet/opus) based on related memories, cross-wing references, and KG depth.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_description": {"type": "string", "description": "Description of the task to estimate"},
                "wing": {"type": "string", "description": "Optional wing filter"},
            },
            "required": ["task_description"],
        },
        "handler": tool_complexity_estimate,
    },
}


# ── MCP Protocol ──────────────────────────────────────────────────────────────

def handle_request(request):
    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "palace", "version": "1.0.0"},
            },
        }
    elif method == "notifications/initialized":
        return None
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
                    for n, t in TOOLS.items()
                ]
            },
        }
    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        try:
            result = TOOLS[tool_name]["handler"](**tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }
        except Exception as e:
            logger.error(f"Tool error in {tool_name}: {e}")
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32000, "message": str(e)}}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main():
    logger.info("Palace MCP Server starting...")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Server error: {e}")


if __name__ == "__main__":
    main()
