#!/usr/bin/env python3
"""
palace.py — Local memory palace for Claude Code
================================================

Reimplementation of MemPalace concepts, trimmed for our use case:
- ChromaDB semantic search over memories
- SQLite knowledge graph with temporal validity
- 4-layer memory stack (L0-L3)
- No network calls, no AAAK, no entity registry, no Wikipedia lookups

Hierarchy mapping:
  Wing  = project (e.g. "small-towns-ai", "optimization", "adhd-reminders")
  Hall  = memory type (user, feedback, project, reference, task_context)
  Room  = specific topic (e.g. "vertex-ai-search", "alger-county", "demo-server")

Storage:
  ChromaDB (~/.claude/palace/)     — semantic search over memory content
  SQLite   (~/.claude/palace/kg.sqlite3) — temporal knowledge graph
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import chromadb

# ── Config ────────────────────────────────────────────────────────────────────

PALACE_DIR = os.path.expanduser("~/.claude/palace")
PALACE_DB = os.path.join(PALACE_DIR, "chroma")
KG_DB = os.path.join(PALACE_DIR, "kg.sqlite3")
IDENTITY_FILE = os.path.join(PALACE_DIR, "identity.txt")
COLLECTION_NAME = "claude_memories"

# Our memory types map to halls
HALLS = ["user", "feedback", "project", "reference", "task_context"]

# Wing-name variants that lost their separators in older digest runs.
# Keyed by the fully separator-stripped, lowercased form.
WING_ALIASES = {
    "villainmonologue": "villain-monologue",
    "budgetanalysis": "budget-analysis",
    "paralleldevicesync": "parallel-device-sync",
    "minecraftclaudeintegration": "minecraft-claude-integration",
    "mmzerodecoded": "mmzero-decoded",
}


def canonicalize_wing(raw: str) -> str:
    """Normalize a wing name to one canonical form.

    Pure-formatting differences (case, underscores, spaces, dots, repeated
    or edge dashes) collapse together. Known separator-stripped variants are
    mapped explicitly via WING_ALIASES. Idempotent, so it is safe to route
    every write and query through it.
    """
    if not raw:
        return "unknown"
    w = re.sub(r"[\s_.]+", "-", raw.strip().lower())
    w = re.sub(r"-{2,}", "-", w).strip("-")
    if not w:
        return "unknown"
    return WING_ALIASES.get(w.replace("-", ""), w)


def _ensure_dirs():
    Path(PALACE_DIR).mkdir(parents=True, exist_ok=True)


def _get_collection(create=False):
    _ensure_dirs()
    client = chromadb.PersistentClient(path=PALACE_DB)
    if create:
        return client.get_or_create_collection(COLLECTION_NAME)
    try:
        return client.get_collection(COLLECTION_NAME)
    except Exception:
        return None


# ── Knowledge Graph ───────────────────────────────────────────────────────────


class KnowledgeGraph:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or KG_DB
        _ensure_dirs()
        self._init_db()

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT DEFAULT 'unknown',
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                confidence REAL DEFAULT 1.0,
                source TEXT,
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_verified TEXT,
                created_at TEXT,
                invalidated_at TEXT,
                FOREIGN KEY (subject) REFERENCES entities(id),
                FOREIGN KEY (object) REFERENCES entities(id)
            );
            CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
            CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
            CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
        """)
        # Migrations for existing DBs — each ALTER is a no-op if the column
        # already exists. Bitemporal model adds transaction-time alongside
        # the existing valid-time (valid_from/valid_to):
        #   created_at      — when we recorded this belief (txn-time start)
        #   invalidated_at  — when we retracted it (txn-time end); NULL = believed
        for col in ("last_verified TEXT", "created_at TEXT", "invalidated_at TEXT"):
            try:
                conn.execute(f"ALTER TABLE triples ADD COLUMN {col}")
            except Exception:
                pass  # Column already exists
        # Backfill created_at from extracted_at for pre-bitemporal rows.
        conn.execute(
            "UPDATE triples SET created_at = COALESCE(extracted_at, CURRENT_TIMESTAMP) "
            "WHERE created_at IS NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_invalidated ON triples(invalidated_at)")
        conn.commit()
        conn.close()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _eid(self, name: str) -> str:
        return name.lower().replace(" ", "_").replace("'", "")

    def add_entity(self, name: str, entity_type: str = "unknown", properties: dict = None):
        eid = self._eid(name)
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
            (eid, name, entity_type, json.dumps(properties or {}))
        )
        conn.commit()
        conn.close()
        return eid

    def add_triple(self, subject: str, predicate: str, obj: str,
                   valid_from: str = None, valid_to: str = None,
                   confidence: float = 1.0, source: str = None):
        sub_id = self._eid(subject)
        obj_id = self._eid(obj)
        pred = predicate.lower().replace(" ", "_")

        conn = self._conn()
        # Auto-create entities
        conn.execute("INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)", (sub_id, subject))
        conn.execute("INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)", (obj_id, obj))

        # Skip if an identical, currently-believed triple exists. A
        # retracted (invalidated_at) triple does NOT block re-assertion —
        # re-asserting creates a fresh believed row, preserving the gap.
        existing = conn.execute(
            "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=? "
            "AND valid_to IS NULL AND invalidated_at IS NULL",
            (sub_id, pred, obj_id)
        ).fetchone()
        if existing:
            conn.close()
            return existing[0]

        now = datetime.now().isoformat()
        triple_id = f"t_{sub_id}_{pred}_{obj_id}_{hashlib.md5(f'{valid_from}{now}'.encode()).hexdigest()[:8]}"
        conn.execute(
            "INSERT INTO triples (id, subject, predicate, object, valid_from, valid_to, confidence, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (triple_id, sub_id, pred, obj_id, valid_from, valid_to, confidence, source, now)
        )
        conn.commit()
        conn.close()
        return triple_id

    def invalidate(self, subject: str, predicate: str, obj: str,
                   ended: str = None, retract: bool = False):
        """End a fact.

        retract=False (default): valid-time end — the fact stopped being
        true in the world on `ended`. The record is still believed and
        still answers "what was true as of <date before ended>".

        retract=True: transaction-time retraction — we no longer believe
        this record (it was wrong / superseded). It is excluded from
        normal queries but retained so `as_believed` can still answer
        "what did we believe as of <date before now>".
        """
        sub_id = self._eid(subject)
        obj_id = self._eid(obj)
        pred = predicate.lower().replace(" ", "_")
        conn = self._conn()
        if retract:
            conn.execute(
                "UPDATE triples SET invalidated_at=? WHERE subject=? AND predicate=? "
                "AND object=? AND invalidated_at IS NULL",
                (datetime.now().isoformat(), sub_id, pred, obj_id)
            )
        else:
            ended = ended or date.today().isoformat()
            conn.execute(
                "UPDATE triples SET valid_to=? WHERE subject=? AND predicate=? "
                "AND object=? AND valid_to IS NULL AND invalidated_at IS NULL",
                (ended, sub_id, pred, obj_id)
            )
        conn.commit()
        conn.close()

    def supersede(self, subject: str, predicate: str, old_obj: str, new_obj: str,
                  valid_from: str = None, source: str = None):
        """Replace a fact's object, preserving belief history.

        Retracts the old triple in transaction-time and inserts the new
        one. The 'decision moved A -> B' primitive: after this, normal
        queries see only B, but `as_believed=<date before now>` still
        sees A.
        """
        self.invalidate(subject, predicate, old_obj, retract=True)
        new_id = self.add_triple(
            subject, predicate, new_obj,
            valid_from=valid_from or date.today().isoformat(), source=source,
        )
        return {
            "superseded": f"{subject} -> {predicate} -> {old_obj}",
            "now": f"{subject} -> {predicate} -> {new_obj}",
            "triple_id": new_id,
        }

    def query_entity(self, name: str, as_of: str = None, direction: str = "both",
                     as_believed: str = None):
        """Query an entity's facts.

        as_of       — valid-time filter: facts true in the world on that date.
        as_believed — transaction-time filter: facts we believed on that date
                      (includes facts later retracted). Default (None) returns
                      only currently-believed facts (invalidated_at IS NULL).
        """
        eid = self._eid(name)
        conn = self._conn()
        results = []

        def _temporal():
            clause, params = "", []
            if as_of:
                clause += (" AND (t.valid_from IS NULL OR t.valid_from <= ?)"
                           " AND (t.valid_to IS NULL OR t.valid_to >= ?)")
                params += [as_of, as_of]
            if as_believed:
                clause += (" AND (t.created_at IS NULL OR t.created_at <= ?)"
                           " AND (t.invalidated_at IS NULL OR t.invalidated_at > ?)")
                params += [as_believed, as_believed]
            else:
                clause += " AND t.invalidated_at IS NULL"
            return clause, params

        cols = "t.predicate, t.valid_from, t.valid_to, t.confidence, t.invalidated_at, e.name"

        if direction in ("outgoing", "both"):
            clause, tparams = _temporal()
            q = (f"SELECT {cols} FROM triples t JOIN entities e ON t.object = e.id "
                 f"WHERE t.subject = ?{clause}")
            for pred, vf, vt, conf, inv, oname in conn.execute(q, [eid] + tparams).fetchall():
                results.append({
                    "direction": "outgoing", "subject": name,
                    "predicate": pred, "object": oname,
                    "valid_from": vf, "valid_to": vt, "confidence": conf,
                    "current": vt is None, "believed": inv is None,
                    "retracted_at": inv,
                })

        if direction in ("incoming", "both"):
            clause, tparams = _temporal()
            q = (f"SELECT {cols} FROM triples t JOIN entities e ON t.subject = e.id "
                 f"WHERE t.object = ?{clause}")
            for pred, vf, vt, conf, inv, sname in conn.execute(q, [eid] + tparams).fetchall():
                results.append({
                    "direction": "incoming", "subject": sname,
                    "predicate": pred, "object": name,
                    "valid_from": vf, "valid_to": vt, "confidence": conf,
                    "current": vt is None, "believed": inv is None,
                    "retracted_at": inv,
                })

        conn.close()
        return results

    def timeline(self, entity_name: str = None):
        """Chronological history. Includes retracted facts (flagged
        believed=False) — a timeline is a history view, not current state."""
        conn = self._conn()
        cols = ("s.name, t.predicate, o.name, t.valid_from, t.valid_to, "
                "t.invalidated_at, t.created_at")
        base = (f"SELECT {cols} FROM triples t "
                "JOIN entities s ON t.subject = s.id "
                "JOIN entities o ON t.object = o.id")
        if entity_name:
            eid = self._eid(entity_name)
            rows = conn.execute(
                base + " WHERE (t.subject = ? OR t.object = ?) "
                "ORDER BY t.valid_from ASC NULLS LAST", (eid, eid)
            ).fetchall()
        else:
            rows = conn.execute(
                base + " ORDER BY t.valid_from ASC NULLS LAST LIMIT 100"
            ).fetchall()
        conn.close()
        return [{"subject": s, "predicate": p, "object": o,
                 "valid_from": vf, "valid_to": vt, "current": vt is None,
                 "believed": inv is None, "retracted_at": inv,
                 "recorded_at": cre}
                for s, p, o, vf, vt, inv, cre in rows]

    def verify_triple(self, triple_id: str):
        """Mark a triple as verified now."""
        conn = self._conn()
        conn.execute(
            "UPDATE triples SET last_verified=? WHERE id=?",
            (date.today().isoformat(), triple_id)
        )
        conn.commit()
        conn.close()

    def audit(self, stale_days: int = 30):
        """Find triples that haven't been verified in stale_days.
        Returns list of stale triples needing re-verification."""
        conn = self._conn()
        cutoff = date.today().isoformat()
        rows = conn.execute("""
            SELECT t.id, s.name as subject, t.predicate, o.name as object,
                   t.valid_from, t.confidence, t.last_verified, t.extracted_at
            FROM triples t
            JOIN entities s ON t.subject = s.id
            JOIN entities o ON t.object = o.id
            WHERE t.valid_to IS NULL
              AND t.invalidated_at IS NULL
              AND (t.last_verified IS NULL
                   OR julianday(?) - julianday(t.last_verified) > ?)
            ORDER BY t.last_verified ASC NULLS FIRST, t.extracted_at ASC
        """, (cutoff, stale_days)).fetchall()
        conn.close()
        return [{
            "triple_id": r[0], "subject": r[1], "predicate": r[2], "object": r[3],
            "valid_from": r[4], "confidence": r[5],
            "last_verified": r[6], "extracted_at": r[7],
            "days_stale": "never" if r[6] is None else f"{stale_days}+",
        } for r in rows]

    def stats(self):
        conn = self._conn()
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        believed = conn.execute("SELECT COUNT(*) FROM triples WHERE invalidated_at IS NULL").fetchone()[0]
        retracted = triples - believed
        current = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE valid_to IS NULL AND invalidated_at IS NULL"
        ).fetchone()[0]
        never_verified = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE valid_to IS NULL "
            "AND invalidated_at IS NULL AND last_verified IS NULL"
        ).fetchone()[0]
        predicates = [r[0] for r in conn.execute("SELECT DISTINCT predicate FROM triples ORDER BY predicate").fetchall()]
        conn.close()
        return {"entities": entities, "triples": triples, "current_facts": current,
                "expired_facts": believed - current, "believed_facts": believed,
                "retracted_facts": retracted, "never_verified": never_verified,
                "relationship_types": predicates}


# ── Memory Storage (ChromaDB) ─────────────────────────────────────────────────


def add_memory(wing: str, hall: str, room: str, content: str,
               source_file: str = None, added_by: str = "claude",
               drawer: str = None):
    col = _get_collection(create=True)
    wing = canonicalize_wing(wing)

    # Duplicate check
    try:
        results = col.query(query_texts=[content], n_results=1, include=["distances"])
        if results["distances"] and results["distances"][0]:
            if (1 - results["distances"][0][0]) >= 0.95:
                return {"success": False, "reason": "duplicate"}
    except Exception:
        pass

    drawer_id = f"mem_{wing}_{room}_{hashlib.md5((content[:100] + datetime.now().isoformat()).encode()).hexdigest()[:16]}"
    meta = {
        "wing": wing, "hall": hall, "room": room,
        "source_file": source_file or "",
        "added_by": added_by,
        "filed_at": datetime.now().isoformat(),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    if drawer:
        meta["drawer"] = drawer
    col.add(ids=[drawer_id], documents=[content], metadatas=[meta])
    return {"success": True, "id": drawer_id, "wing": wing, "hall": hall, "room": room,
            **({"drawer": drawer} if drawer else {})}


def delete_memory(memory_id: str):
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    existing = col.get(ids=[memory_id])
    if not existing["ids"]:
        return {"success": False, "error": f"Not found: {memory_id}"}
    col.delete(ids=[memory_id])
    return {"success": True, "id": memory_id}


def search_memories(query: str, wing: str = None, hall: str = None,
                    room: str = None, drawer: str = None, n_results: int = 5):
    col = _get_collection()
    if not col:
        return {"error": "No palace found", "results": []}

    where_clauses = []
    if wing:
        where_clauses.append({"wing": canonicalize_wing(wing)})
    if hall:
        where_clauses.append({"hall": hall})
    if room:
        where_clauses.append({"room": room})
    if drawer:
        where_clauses.append({"drawer": drawer})

    where = None
    if len(where_clauses) > 1:
        where = {"$and": where_clauses}
    elif len(where_clauses) == 1:
        where = where_clauses[0]

    kwargs = {"query_texts": [query], "n_results": n_results,
              "include": ["documents", "metadatas", "distances"]}
    if where:
        kwargs["where"] = where

    try:
        results = col.query(**kwargs)
    except Exception as e:
        return {"error": str(e), "results": []}

    hits = []
    for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results["distances"][0]):
        hits.append({
            "text": doc,
            "wing": meta.get("wing", "?"), "hall": meta.get("hall", "?"),
            "room": meta.get("room", "?"),
            "source_file": Path(meta.get("source_file", "")).name if meta.get("source_file") else "",
            "similarity": round(1 - dist, 3),
            "date": meta.get("date", ""),
        })
    return {"query": query, "results": hits}


# ── Memory Layers ─────────────────────────────────────────────────────────────


class MemoryStack:
    def __init__(self):
        self.palace_path = PALACE_DB

    def wake_up(self, wing: str = None) -> str:
        """L0 (identity) + L1 (essential facts). ~200-600 tokens."""
        parts = []

        # L0: Identity
        if os.path.exists(IDENTITY_FILE):
            with open(IDENTITY_FILE) as f:
                parts.append(f.read().strip())
        else:
            parts.append("No identity configured. Create ~/.claude/palace/identity.txt")

        # L1: Top memories grouped by room
        col = _get_collection()
        if col:
            kwargs = {"include": ["documents", "metadatas"]}
            if wing:
                kwargs["where"] = {"wing": canonicalize_wing(wing)}
            try:
                results = col.get(**kwargs)
                docs = results.get("documents", [])
                metas = results.get("metadatas", [])
                if docs:
                    by_room = defaultdict(list)
                    for doc, meta in zip(docs, metas):
                        room = meta.get("room", "general")
                        by_room[room].append(doc)

                    parts.append("\n## Essential Memory")
                    total = 0
                    for room, entries in sorted(by_room.items()):
                        parts.append(f"\n[{room}]")
                        for entry in entries[:3]:
                            snippet = entry.strip().replace("\n", " ")
                            if len(snippet) > 200:
                                snippet = snippet[:197] + "..."
                            parts.append(f"  - {snippet}")
                            total += len(snippet)
                            if total > 3000:
                                parts.append("  ... (use search for more)")
                                return "\n".join(parts)
            except Exception:
                pass

        return "\n".join(parts)

    def recall(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """L2: On-demand retrieval by wing/room."""
        col = _get_collection()
        if not col:
            return "No palace found."

        where_clauses = []
        if wing:
            where_clauses.append({"wing": canonicalize_wing(wing)})
        if room:
            where_clauses.append({"room": room})

        where = None
        if len(where_clauses) > 1:
            where = {"$and": where_clauses}
        elif len(where_clauses) == 1:
            where = where_clauses[0]

        kwargs = {"include": ["documents", "metadatas"], "limit": n_results}
        if where:
            kwargs["where"] = where

        try:
            results = col.get(**kwargs)
        except Exception as e:
            return f"Error: {e}"

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])
        if not docs:
            return f"No memories found for wing={wing} room={room}"

        lines = [f"## Recall ({len(docs)} memories)"]
        for doc, meta in zip(docs, metas):
            room_name = meta.get("room", "?")
            hall = meta.get("hall", "?")
            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            lines.append(f"  [{hall}/{room_name}] {snippet}")
        return "\n".join(lines)

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """L3: Deep semantic search."""
        result = search_memories(query, wing=wing, room=room, n_results=n_results)
        if result.get("error"):
            return result["error"]
        if not result["results"]:
            return "No results found."
        lines = [f'## Search: "{query}"']
        for i, hit in enumerate(result["results"], 1):
            lines.append(f"  [{i}] {hit['wing']}/{hit['hall']}/{hit['room']} (sim={hit['similarity']})")
            snippet = hit["text"].strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            lines.append(f"      {snippet}")
        return "\n".join(lines)

    def status(self) -> dict:
        result = {"palace_path": PALACE_DB, "kg_path": KG_DB,
                  "identity_exists": os.path.exists(IDENTITY_FILE)}
        col = _get_collection()
        if col:
            result["total_memories"] = col.count()
            wings = {}
            halls = {}
            try:
                all_meta = col.get(include=["metadatas"])["metadatas"]
                for m in all_meta:
                    w = m.get("wing", "unknown")
                    h = m.get("hall", "unknown")
                    wings[w] = wings.get(w, 0) + 1
                    halls[h] = halls.get(h, 0) + 1
            except Exception:
                pass
            result["wings"] = wings
            result["halls"] = halls
        else:
            result["total_memories"] = 0
        return result


# ── Migration: Import existing .md memories ───────────────────────────────────


def _parse_memory_frontmatter(content: str):
    """Parse YAML-like frontmatter from memory .md files."""
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            frontmatter[key.strip()] = val.strip()

    body = parts[2].strip()
    return frontmatter, body


def _infer_wing_from_path(path: str) -> str:
    """Infer project wing from memory file path."""
    path = path.replace("\\", "/")
    # Pattern: ~/.claude/projects/PROJECT_KEY/memory/...
    match = re.search(r"projects/([^/]+)/memory", path)
    if match:
        key = match.group(1)
        # Map known project keys to clean wing names
        mappings = {
            "C--ai": "small-towns-ai",
            "C--Claude-Projects1-Optimization": "optimization",
            "C--Claude-Projects1-ADHDReminders": "adhd-reminders",
            "C--Claude-Projects1-MusicManagement": "music-management",
            "C--Claude-Projects1-SemanticSearch": "semantic-search",
            "C--Claude-Projects1-SideHustle": "side-hustle",
            "C--Claude-Projects1-TicketMachine": "ticket-machine",
            "C--Claude-Projects1-RoadQualityTool": "road-quality",
        }
        return canonicalize_wing(mappings.get(key, key.replace("c--", "").replace("claude-projects1-", "")))

    # Memory-sync paths
    match = re.search(r"memory-sync/projects/([^/]+)", path)
    if match:
        key = match.group(1)
        return canonicalize_wing(key.replace("c--", "").replace("e--", "").replace("claude-", "").replace("projects1-", ""))

    # Global memory
    if "memory-sync/global" in path or "/.claude/memory/" in path:
        return "global"

    return "unknown"


def _infer_room_from_filename(filename: str) -> str:
    """Infer room name from memory filename."""
    name = Path(filename).stem  # Remove .md
    if name == "MEMORY":
        return "index"
    # Remove type prefixes (user_, feedback_, project_, reference_, research_, task_)
    for prefix in ("user_", "feedback_", "project_", "reference_", "research_", "task_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", "-")


def migrate_existing_memories(dry_run: bool = False):
    """Import all existing .md memory files into the palace."""
    claude_dir = os.path.expanduser("~/.claude")
    memory_files = []

    # Find all memory .md files
    for root, dirs, files in os.walk(claude_dir):
        # Skip backups, cache, plugins
        skip = any(s in root for s in ["backups", "cache", "plugins", "plans"])
        if skip:
            continue
        for f in files:
            if f.endswith(".md"):
                full = os.path.join(root, f)
                if "/memory/" in full.replace("\\", "/") or "memory-sync" in full.replace("\\", "/"):
                    memory_files.append(full)

    results = {"found": len(memory_files), "imported": 0, "skipped": 0, "errors": 0}

    for fpath in memory_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()

            if not content.strip():
                results["skipped"] += 1
                continue

            frontmatter, body = _parse_memory_frontmatter(content)
            wing = _infer_wing_from_path(fpath)
            hall = frontmatter.get("type", "project")
            room = _infer_room_from_filename(fpath)

            if hall not in HALLS:
                hall = "project"

            if dry_run:
                print(f"  [DRY] {wing}/{hall}/{room} <- {Path(fpath).name}")
                results["imported"] += 1
                continue

            # Use body if frontmatter was parsed, otherwise full content
            text = body if body else content
            result = add_memory(wing, hall, room, text, source_file=fpath, added_by="migration")
            if result.get("success"):
                results["imported"] += 1
            else:
                results["skipped"] += 1

        except Exception as e:
            print(f"  Error importing {fpath}: {e}")
            results["errors"] += 1

    return results


# ── Project Auto-Onboarding ───────────────────────────────────────────────────


def onboard_project(project_dir: str, wing: str = None, dry_run: bool = False):
    """Scan a project directory and create a palace wing with architecture info.

    Detects: package.json, pyproject.toml, Cargo.toml, go.mod, CLAUDE.md,
    directory structure, key file patterns.
    """
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        return {"error": f"Not a directory: {project_dir}"}

    wing = wing or Path(project_dir).name.lower().replace(" ", "-")

    # Check if wing already has substantial content
    col = _get_collection()
    if col:
        try:
            existing = col.get(where={"wing": wing}, include=[])
            if len(existing.get("ids", [])) >= 3:
                return {"skipped": True, "wing": wing,
                        "reason": f"Wing '{wing}' already has {len(existing['ids'])} memories"}
        except Exception:
            pass

    findings = []

    # Detect project type and deps
    manifest_files = {
        "package.json": "Node.js/JavaScript",
        "pyproject.toml": "Python",
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "composer.json": "PHP",
        "Gemfile": "Ruby",
        "pom.xml": "Java/Maven",
        "build.gradle": "Java/Gradle",
    }
    for fname, lang in manifest_files.items():
        fpath = os.path.join(project_dir, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(4000)
                findings.append({
                    "room": "stack",
                    "content": f"Project type: {lang}\nManifest ({fname}):\n{content[:3000]}",
                })
            except Exception:
                findings.append({"room": "stack", "content": f"Project type: {lang} ({fname} found)"})
            break

    # Scan directory structure (top 2 levels)
    dir_tree = []
    for root, dirs, files in os.walk(project_dir):
        depth = root.replace(project_dir, "").count(os.sep)
        if depth > 1:
            dirs.clear()
            continue
        # Skip noise dirs
        dirs[:] = [d for d in dirs if d not in (
            ".git", "node_modules", "__pycache__", ".next", "venv",
            ".venv", "dist", "build", ".cache", ".tox", "target",
        )]
        indent = "  " * depth
        dir_tree.append(f"{indent}{os.path.basename(root)}/")
        for f in sorted(files)[:20]:
            dir_tree.append(f"{indent}  {f}")

    if dir_tree:
        tree_str = "\n".join(dir_tree[:80])
        findings.append({"room": "structure", "content": f"Directory structure:\n{tree_str}"})

    # Read CLAUDE.md if present
    for claude_md in ("CLAUDE.md", ".claude/CLAUDE.md"):
        cpath = os.path.join(project_dir, claude_md)
        if os.path.exists(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    content = f.read(5000)
                findings.append({"room": "conventions", "content": f"Project CLAUDE.md:\n{content[:4000]}"})
            except Exception:
                pass
            break

    # Read README if present
    for readme in ("README.md", "README.rst", "README.txt", "README"):
        rpath = os.path.join(project_dir, readme)
        if os.path.exists(rpath):
            try:
                with open(rpath, "r", encoding="utf-8") as f:
                    content = f.read(3000)
                findings.append({"room": "overview", "content": f"README:\n{content[:2500]}"})
            except Exception:
                pass
            break

    # Git info
    git_dir = os.path.join(project_dir, ".git")
    if os.path.isdir(git_dir):
        import subprocess
        try:
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=project_dir, capture_output=True, text=True, timeout=5
            )
            log = subprocess.run(
                ["git", "log", "--oneline", "-10", "--no-color"],
                cwd=project_dir, capture_output=True, text=True, timeout=5
            )
            git_info = ""
            if remote.returncode == 0:
                git_info += f"Remote: {remote.stdout.strip()}\n"
            if log.returncode == 0:
                git_info += f"Recent commits:\n{log.stdout.strip()}"
            if git_info:
                findings.append({"room": "git", "content": git_info})
        except Exception:
            pass

    if not findings:
        return {"skipped": True, "wing": wing, "reason": "No recognizable project files found"}

    results = {"wing": wing, "stored": 0, "findings": len(findings)}

    if dry_run:
        for f in findings:
            print(f"  [DRY] {wing}/project/{f['room']}: {f['content'][:80]}...")
        return results

    for f in findings:
        result = add_memory(
            wing=wing, hall="project", room=f["room"],
            content=f["content"], source_file=project_dir, added_by="auto-onboard",
        )
        if result.get("success"):
            results["stored"] += 1

    return results


# ── Cross-Project Pattern Detection ──────────────────────────────────────────


def detect_cross_project_patterns(threshold: float = 0.85, min_wings: int = 2):
    """Find memories that appear semantically similar across different wings.
    Promotes recurring patterns to global wing.
    """
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}

    # Get all memories
    results = col.get(include=["documents", "metadatas"])
    docs = results.get("documents", [])
    metas = results.get("metadatas", [])

    if len(docs) < 10:
        return {"skipped": True, "reason": "Too few memories to detect patterns"}

    # Group by wing
    by_wing = defaultdict(list)
    for doc, meta in zip(docs, metas):
        wing = meta.get("wing", "unknown")
        if wing != "global":  # Don't compare global to itself
            by_wing[wing].append((doc, meta))

    wings = list(by_wing.keys())
    if len(wings) < 2:
        return {"skipped": True, "reason": "Need at least 2 non-global wings"}

    # For each memory, search across OTHER wings for similar content
    patterns_found = []
    seen = set()

    for wing in wings:
        for doc, meta in by_wing[wing][:20]:  # Cap per wing to avoid O(n^2) explosion
            # Search all memories (unfiltered) for similar content
            try:
                search_results = col.query(
                    query_texts=[doc[:500]],  # Cap query length
                    n_results=5,
                    include=["metadatas", "distances"],
                )
            except Exception:
                continue

            if not search_results["metadatas"][0]:
                continue

            # Check if matches span multiple wings
            match_wings = set()
            match_wings.add(wing)
            for smeta, dist in zip(search_results["metadatas"][0], search_results["distances"][0]):
                sim = 1 - dist
                if sim >= threshold and smeta.get("wing") != wing:
                    match_wings.add(smeta.get("wing", "?"))

            if len(match_wings) >= min_wings:
                pattern_key = doc[:100]
                if pattern_key not in seen:
                    seen.add(pattern_key)
                    patterns_found.append({
                        "content": doc[:300],
                        "wings": list(match_wings),
                        "room": meta.get("room", "general"),
                        "source_wing": wing,
                    })

    # Promote patterns to global
    promoted = 0
    for pattern in patterns_found:
        wing_list = ", ".join(pattern["wings"])
        content = f"CROSS-PROJECT PATTERN (found in: {wing_list}):\n{pattern['content']}"
        result = add_memory(
            wing="global", hall="feedback", room="cross-project-pattern",
            content=content, added_by="pattern-detector",
        )
        if result.get("success"):
            promoted += 1

    return {"patterns_found": len(patterns_found), "promoted": promoted, "details": patterns_found}


# ── Palace Consolidation ─────────────────────────────────────────────────────


def consolidate_wing(wing: str, similarity_threshold: float = 0.92):
    """Find duplicate or near-duplicate memories within a wing and report them."""
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}

    try:
        results = col.get(
            where={"wing": wing},
            include=["documents", "metadatas"],
        )
    except Exception as e:
        return {"error": str(e)}

    ids = results.get("ids", [])
    docs = results.get("documents", [])
    metas = results.get("metadatas", [])

    if len(docs) < 2:
        return {"wing": wing, "duplicates": [], "message": "Not enough memories to consolidate"}

    duplicates = []
    checked = set()

    for i, (doc, meta) in enumerate(zip(docs, metas)):
        if ids[i] in checked:
            continue

        try:
            search_results = col.query(
                query_texts=[doc[:500]],
                n_results=5,
                where={"wing": wing},
                include=["metadatas", "distances"],
            )
        except Exception:
            continue

        for j, (smeta, dist, sid) in enumerate(zip(
            search_results["metadatas"][0],
            search_results["distances"][0],
            search_results["ids"][0],
        )):
            sim = 1 - dist
            if sim >= similarity_threshold and sid != ids[i] and sid not in checked:
                duplicates.append({
                    "memory_a": ids[i],
                    "memory_b": sid,
                    "similarity": round(sim, 3),
                    "room_a": meta.get("room", "?"),
                    "room_b": smeta.get("room", "?"),
                })
                checked.add(sid)

    return {"wing": wing, "total_memories": len(docs), "duplicates": duplicates}


def consolidate_all(auto_delete: bool = False, similarity_threshold: float = 0.95):
    """Run consolidation across all wings. Optionally auto-delete near-exact dupes."""
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}

    # Get all wings
    all_meta = col.get(include=["metadatas"])["metadatas"]
    wings = list(set(m.get("wing", "unknown") for m in all_meta))

    total_dupes = 0
    deleted = 0
    wing_reports = {}

    for wing in wings:
        report = consolidate_wing(wing, similarity_threshold)
        dupes = report.get("duplicates", [])
        wing_reports[wing] = {"total": report.get("total_memories", 0), "dupes": len(dupes)}
        total_dupes += len(dupes)

        if auto_delete and dupes:
            for dupe in dupes:
                # Delete the second one (keep the first)
                try:
                    col.delete(ids=[dupe["memory_b"]])
                    deleted += 1
                except Exception:
                    pass

    return {
        "wings_checked": len(wings),
        "total_duplicates": total_dupes,
        "auto_deleted": deleted,
        "by_wing": wing_reports,
    }


# ── Wing Merge Migration ─────────────────────────────────────────────────────


def merge_wings(dry_run: bool = True):
    """Collapse wing-name fragments to their canonical form.

    Rewrites ChromaDB memory metadata only — embeddings and documents are
    untouched (col.update with metadatas alone). Dry-run by default: returns
    the merge plan without writing anything.
    """
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}

    data = col.get(include=["metadatas"])
    ids = data.get("ids", [])
    metas = data.get("metadatas", [])

    plan = defaultdict(lambda: defaultdict(int))  # canonical -> {original: count}
    update_ids = []
    update_metas = []
    for mid, meta in zip(ids, metas):
        orig = meta.get("wing", "unknown")
        canon = canonicalize_wing(orig)
        plan[canon][orig] += 1
        if canon != orig:
            new_meta = dict(meta)
            new_meta["wing"] = canon
            update_ids.append(mid)
            update_metas.append(new_meta)

    merges = {
        canon: dict(origs)
        for canon, origs in sorted(plan.items())
        if len(origs) > 1 or any(o != canon for o in origs)
    }

    report = {
        "total_memories": len(ids),
        "memories_to_rewrite": len(update_ids),
        "merges": merges,
        "applied": False,
    }

    if dry_run or not update_ids:
        return report

    batch = 200
    for i in range(0, len(update_ids), batch):
        col.update(ids=update_ids[i:i + batch], metadatas=update_metas[i:i + batch])
    report["applied"] = True
    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python palace.py migrate [--dry-run]      Import existing .md memories")
        print("  python palace.py search <query>           Semantic search")
        print("  python palace.py status                   Palace overview")
        print("  python palace.py wake-up [wing]           L0+L1 context")
        print("  python palace.py onboard <dir> [wing]     Auto-scan a project")
        print("  python palace.py patterns                 Detect cross-project patterns")
        print("  python palace.py consolidate [--auto]     Find/remove duplicates")
        print("  python palace.py merge-wings [--apply]    Collapse wing-name fragments")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "migrate":
        dry = "--dry-run" in sys.argv
        print("Migrating existing memories into palace...")
        r = migrate_existing_memories(dry_run=dry)
        print(f"\nDone: {r['imported']}/{r['found']} imported, {r['skipped']} skipped, {r['errors']} errors")

    elif cmd == "search":
        query = " ".join(sys.argv[2:])
        if not query:
            print("Usage: python palace.py search <query>")
            sys.exit(1)
        stack = MemoryStack()
        print(stack.search(query))

    elif cmd == "status":
        stack = MemoryStack()
        print(json.dumps(stack.status(), indent=2))

    elif cmd in ("wake-up", "wakeup"):
        wing = sys.argv[2] if len(sys.argv) > 2 else None
        stack = MemoryStack()
        print(stack.wake_up(wing=wing))

    elif cmd == "onboard":
        if len(sys.argv) < 3:
            print("Usage: python palace.py onboard <dir> [wing] [--dry-run]")
            sys.exit(1)
        project_dir = sys.argv[2]
        wing = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("-") else None
        dry = "--dry-run" in sys.argv
        r = onboard_project(project_dir, wing=wing, dry_run=dry)
        print(json.dumps(r, indent=2))

    elif cmd == "patterns":
        print("Detecting cross-project patterns...")
        r = detect_cross_project_patterns()
        print(json.dumps(r, indent=2, default=str))

    elif cmd == "consolidate":
        auto = "--auto" in sys.argv
        print(f"Consolidating palace{' (auto-delete)' if auto else ' (report only)'}...")
        r = consolidate_all(auto_delete=auto)
        print(json.dumps(r, indent=2))

    elif cmd in ("merge-wings", "merge_wings"):
        apply = "--apply" in sys.argv
        print(f"Merging wing fragments{' (APPLYING)' if apply else ' (dry run)'}...")
        r = merge_wings(dry_run=not apply)
        print(json.dumps(r, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
