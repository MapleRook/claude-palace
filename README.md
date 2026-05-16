# claude-palace

Local memory palace for Claude Code. Cross-session context stored in a ChromaDB vector store and a SQLite knowledge graph, with autonomous agents that walk the palace and keep it maintained.

## Lineage

Reimplementation of concepts from [MemPalace](https://github.com/MemPalace/mempalace), trimmed for a single-developer Claude Code setup. The ideas are theirs. The code is mine, written to match the patterns I needed and skip the ones I didn't (no network calls beyond LLM API traffic, no AAAK, no entity registry, no Wikipedia lookups). MemPalace ships under MIT. If you want the full upstream tool with the features I skipped, use theirs.

## Why it exists

Claude Code sessions forget each other. I wanted one that didn't. Palace holds over a thousand memories across every project I work on. Every session starts with a context prime. Every session ends with a digest that extracts discoveries and writes them back. Custodian agents run on a schedule to audit, verify, fill gaps, and link memories across projects.

## Architecture

**Storage.** ChromaDB for semantic search over memory content. SQLite for the bitemporal knowledge graph (entities, triples, valid-time + transaction-time). Both live under `~/.claude/palace/`. No server, no network round-trips for storage.

**Hierarchy.** Wing → Hall → Room → Drawer.
- **Wing** is a project. `small-towns-ai`, `optimization`, `tasktoss`, `villain-monologue`.
- **Hall** is a memory type. Five halls: `user`, `feedback`, `project`, `reference`, `task_context`.
- **Room** is a topic inside a hall.
- **Drawer** is an optional sub-topic.

Every memory lives at a specific location in this tree. Search can filter by any level, or go wide.

**Knowledge graph.** Entity-predicate-entity triples, bitemporal: `valid_from`/`valid_to` track when a fact was true in the world, `created_at`/`invalidated_at` track when we believed it. A fact that stopped being true gets a `valid_to`; a fact we no longer believe gets retracted (`invalidated_at`), not deleted. Queries can ask "what was true as of date X" (`as_of`) *or* "what did I believe as of date X" (`as_believed`) and get the right answer separately. `supersede` is the "decision moved A→B" primitive; the contradiction detector finds same-subject/predicate facts the graph believes two answers to at once.

**Confidence.** Every memory carries a confidence score and a quarantine flag derived from provenance. Regex digest extracts and speculative custodian fill are quarantined — stored but kept out of default retrieval until promoted. Curated and migrated memories are trusted. Discrete facts in trusted memories are mirrored into the KG, sourced back to the memory they came from.

## MCP tools

Exposed to Claude Code over the MCP protocol:

- **Read.** `palace_status`, `palace_search`, `palace_recall`, `palace_wake_up` (search/recall/wake_up take `include_quarantined`)
- **Write.** `palace_add` (optional `confidence`), `palace_delete`, `palace_promote` (un-quarantine a verified memory), `palace_consolidate`
- **Patterns.** `palace_patterns`, `palace_contradictions`
- **Knowledge graph.** `palace_kg_query` (`as_of` / `as_believed`), `palace_kg_add`, `palace_kg_invalidate` (`retract`), `palace_kg_supersede`, `palace_kg_timeline`, `palace_kg_stats`, `palace_kg_audit`, `palace_kg_verify`
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

## What it costs to run

Palace runs locally. There is no hosted service.

The custodian sweep invokes `claude --print` as a subprocess, which uses whatever Claude Code auth is set up on the machine running it. Your auth, your bill. Each call passes `--max-budget-usd` and stops if it hits the cap. Defaults are conservative: $0.15 per custodian call, $0.25 for the Linker, $0.50 for the Expander and Structurer during initial bootstrap. A full sweep across a handful of wings typically runs under a dollar.

ChromaDB and SQLite store everything under `~/.claude/palace/`. No telemetry, no network calls beyond the LLM API traffic the custodians themselves make.

## Session hooks

- `palace_prime.py` runs at session start. Loads wing context, surfaces relevant memories, injects them into the session.
- `palace_digest.py` runs on stop and on pre-compact. If the session already curated memories via the tools, it does nothing. Otherwise it runs one budget-capped Haiku pass to extract durable memories (verified ingestion); on any failure or if disabled via `~/.claude/palace/digest_llm.off`, it falls back to a zero-cost regex pass filed quarantined so it never pollutes default recall.

Both are registered as Claude Code hooks in `~/.claude/settings.json`.

## Sync

`palace_sync.py` exports the palace to a portable JSONL + SQLite format under `~/.claude/memory-sync/palace/`. ChromaDB's native storage isn't portable across machines (path-sensitive, embedding model caches), so export/import is the cross-machine path. Commit the export dir to a private git repo and pull it on the other side.

## Status

Personal tool, running on my machines. Not packaged for general distribution. The code is here if you want to read it; the concepts are better-documented upstream at MemPalace.

## Sponsors

See [SPONSORS.md](SPONSORS.md) for sponsorship tiers and current sponsors.

## Credit

MemPalace is by the MemPalace contributors, MIT licensed. This work rebuilds a subset of those ideas in Python for a different use case.
