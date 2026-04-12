# claude-palace

Local memory palace for Claude Code. Cross-session context stored in a ChromaDB vector store and a SQLite knowledge graph, with autonomous agents that walk the palace and keep it maintained.

## Lineage

Reimplementation of concepts from [MemPalace](https://github.com/MemPalace/mempalace), trimmed for a single-developer Claude Code setup. The ideas are theirs. The code is mine, written to match the patterns I needed and skip the ones I didn't (no network calls beyond LLM API traffic, no AAAK, no entity registry, no Wikipedia lookups). MemPalace ships under MIT. If you want the full upstream tool with the features I skipped, use theirs.

## Why it exists

Claude Code sessions forget each other. I wanted one that didn't. Palace holds over a thousand memories across every project I work on. Every session starts with a context prime. Every session ends with a digest that extracts discoveries and writes them back. Custodian agents run on a schedule to audit, verify, fill gaps, and link memories across projects.

## Architecture

**Storage.** ChromaDB for semantic search over memory content. SQLite for the knowledge graph (entities, triples, temporal validity). Both live under `~/.claude/palace/`. No server, no network round-trips for storage.

**Hierarchy.** Wing → Hall → Room → Drawer.
- **Wing** is a project. `small-towns-ai`, `optimization`, `tasktoss`, `villain-monologue`.
- **Hall** is a memory type. Five halls: `user`, `feedback`, `project`, `reference`, `task_context`.
- **Room** is a topic inside a hall.
- **Drawer** is an optional sub-topic.

Every memory lives at a specific location in this tree. Search can filter by any level, or go wide.

**Knowledge graph.** Entity-predicate-entity triples with `valid_from` and `valid_to` timestamps. A fact that used to be true and isn't anymore gets invalidated, not deleted. Queries can ask "what was true as of this date" and get the right answer.

## MCP tools

Exposed to Claude Code over the MCP protocol:

- **Read.** `palace_status`, `palace_search`, `palace_recall`, `palace_wake_up`
- **Write.** `palace_add`, `palace_delete`, `palace_consolidate`
- **Patterns.** `palace_patterns`
- **Knowledge graph.** `palace_kg_query`, `palace_kg_add`, `palace_kg_invalidate`, `palace_kg_timeline`, `palace_kg_stats`, `palace_kg_audit`, `palace_kg_verify`
- **Setup.** `palace_migrate`, `palace_onboard`
- **Estimation.** `palace_complexity_estimate`

## Custodians

Five agents run the maintenance sweep:

1. **Auditor** (Haiku). Walks rooms, flags stale facts, empty rooms, duplicates, missing KG connections.
2. **Verifier** (Haiku). Cross-references flagged items against the actual codebase. Emits an `escalated` list for items it couldn't confidently resolve.
3. **Structurer** (Sonnet). Finds memories with tables, lists, or catalogs that have no corresponding KG entities, and extracts them into triples.
4. **Expander** (Sonnet). Researches adjacent topics, adds new memories with a confidence score.
5. **Linker** (Haiku, escalates to Sonnet after two consecutive failures on a wing). Finds cross-wing connections and adds KG triples linking them.

Each custodian runs inside a budget (default $0.15 per call, $0.25 for the Linker, $0.50 for the Expander and Structurer during initial bootstrap). An active-lock file prevents custodians from running while an interactive Claude Code session is open, so they never fight the user for resources.

## Session hooks

- `palace_prime.py` runs at session start. Loads wing context, surfaces relevant memories, injects them into the session.
- `palace_digest.py` runs on stop and on pre-compact. Parses the current session for discoveries, extracts facts, writes them back.

Both are registered as Claude Code hooks in `~/.claude/settings.json`.

## Sync

`palace_sync.py` exports the palace to a portable JSONL + SQLite format under `~/.claude/memory-sync/palace/`. ChromaDB's native storage isn't portable across machines (path-sensitive, embedding model caches), so export/import is the cross-machine path. Commit the export dir to a private git repo and pull it on the other side.

## Status

Personal tool, running on my machines. Not packaged for general distribution. The code is here if you want to read it; the concepts are better-documented upstream at MemPalace.

## Credit

MemPalace is by the MemPalace contributors, MIT licensed. This work rebuilds a subset of those ideas in Python for a different use case.
