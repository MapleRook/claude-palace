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
import time
from collections import defaultdict
from contextlib import contextmanager
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

# [[room-name]] tokens in memory bodies are wiki-style cross-links. They
# get resolved to memory ids at write time and stored as bidirectional
# linked_to triples in the KG. search_memories(expand_links=True) then
# post-fetches them so co-relevant context surfaces together.
_LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_-]*)\]\]")

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


# ── Cross-process chroma safety ───────────────────────────────────────────────
# chromadb's PersistentClient is NOT multi-process safe; concurrent writers
# corrupt the HNSW index (incident 2026-05-16, see rebuild_chroma.py). Two
# defenses: cache one client per path (stop per-call client churn), and an
# advisory cross-process lock around every mutation.

_LOCK_FILE = os.path.join(PALACE_DIR, ".chroma.lock")
_LOCK_TIMEOUT = 30   # max seconds to wait for the lock
_LOCK_STALE = 120    # an orphaned lock older than this is stolen
_lock_depth = 0      # in-process reentrancy guard


@contextmanager
def _chroma_lock():
    """Best-effort exclusive lock around chroma mutations. Reentrant within
    a process. After timeout it proceeds anyway and logs — the MCP server
    must never deadlock on this."""
    global _lock_depth
    if _lock_depth > 0:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    _ensure_dirs()
    acquired = False
    deadline = time.time() + _LOCK_TIMEOUT
    while time.time() < deadline:
        try:
            fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_LOCK_FILE) > _LOCK_STALE:
                    os.unlink(_LOCK_FILE)
                    continue
            except OSError:
                pass
            time.sleep(0.25)
    if not acquired:
        print("palace: chroma lock timeout — proceeding without it",
              file=sys.stderr)
    _lock_depth += 1
    try:
        yield
    finally:
        _lock_depth -= 1
        if acquired:
            try:
                os.unlink(_LOCK_FILE)
            except OSError:
                pass


_CHROMA_CLIENTS = {}  # path -> PersistentClient (keyed so tests can repoint)


def _chroma_client():
    _ensure_dirs()
    c = _CHROMA_CLIENTS.get(PALACE_DB)
    if c is None:
        c = chromadb.PersistentClient(path=PALACE_DB)
        _CHROMA_CLIENTS[PALACE_DB] = c
    return c


_DEFAULT_KG = {}  # KG_DB -> KnowledgeGraph (avoid re-running _init_db per call)


def _default_kg():
    kg = _DEFAULT_KG.get(KG_DB)
    if kg is None:
        kg = KnowledgeGraph()
        _DEFAULT_KG[KG_DB] = kg
    return kg


def _get_collection(create=False):
    client = _chroma_client()
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
        # Backfill created_at once. Guard so we don't open a write
        # transaction on every construction (this runs per add_memory via
        # facts-first). Wrapped: a transient lock here must never crash the
        # importer/MCP server — the schema already exists, retry next time.
        try:
            if conn.execute(
                "SELECT 1 FROM triples WHERE created_at IS NULL LIMIT 1"
            ).fetchone():
                conn.execute(
                    "UPDATE triples SET created_at = "
                    "COALESCE(extracted_at, CURRENT_TIMESTAMP) WHERE created_at IS NULL"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_invalidated "
                         "ON triples(invalidated_at)")
            conn.commit()
        except Exception as e:
            print(f"palace KG: deferred migration ({e})", file=sys.stderr)
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        # WAL = concurrent readers + one writer (vs default: writers block
        # everything). busy_timeout retries instead of instantly raising
        # "database is locked". Both reduce multi-process fragility.
        try:
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

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


def _derive_confidence(added_by: str = "", room: str = ""):
    """Map provenance to (confidence, quarantined). Quarantined memories
    are stored but kept out of default retrieval until promoted.

    - regex digest extracts        -> 0.3, quarantined (lowest trust)
    - speculative custodian fill   -> 0.5, quarantined (the audit's
      main dilution concern: expander/structurer/pattern-detector)
    - contradiction flags          -> 0.6, visible (you want to see these)
    - claude/migration/onboard/etc -> 1.0, trusted
    """
    ab, rm = (added_by or "").lower(), (room or "").lower()
    if rm == "digest-unverified" or ab == "digest-hook":
        return 0.3, True
    if ab in ("expander", "structurer", "pattern-detector"):
        return 0.5, True
    if ab == "contradiction-detector":
        return 0.6, False
    return 1.0, False


def _resolve_link_targets(target_rooms, prefer_wing=None):
    """Resolve [[room-name]] tokens to currently-believed memory ids.

    Preference order: same wing first (typed within the same project),
    then any wing. Returns a list of ids (may be empty if no match).
    Silent on lookup errors — binding is best-effort, not load-bearing.
    """
    col = _get_collection()
    if not col or not target_rooms:
        return []
    found = []
    seen = set()
    for room in target_rooms:
        ids = []
        if prefer_wing:
            try:
                res = col.get(
                    where={"$and": [
                        {"room": {"$eq": room}},
                        {"wing": {"$eq": prefer_wing}},
                        {"current": {"$eq": True}},
                    ]}
                )
                ids = list(res.get("ids") or [])
            except Exception:
                ids = []
        if not ids:
            try:
                res = col.get(
                    where={"$and": [
                        {"room": {"$eq": room}},
                        {"current": {"$eq": True}},
                    ]}
                )
                ids = list(res.get("ids") or [])
            except Exception:
                ids = []
        for mid in ids:
            if mid not in seen:
                seen.add(mid)
                found.append(mid)
    return found


def add_memory(wing: str, hall: str, room: str, content: str,
               source_file: str = None, added_by: str = "claude",
               drawer: str = None, confidence: float = None,
               extract: bool = True):
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

    if confidence is None:
        confidence, quarantined = _derive_confidence(added_by, room)
    else:
        quarantined = confidence < 0.5

    drawer_id = f"mem_{wing}_{room}_{hashlib.md5((content[:100] + datetime.now().isoformat()).encode()).hexdigest()[:16]}"
    meta = {
        "wing": wing, "hall": hall, "room": room,
        "source_file": source_file or "",
        "added_by": added_by,
        "filed_at": datetime.now().isoformat(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "confidence": confidence,
        "quarantined": quarantined,
        # Currency filter key. Always present so Chroma `where` can match
        # it (absent keys are unmatchable). True = current understanding;
        # the arbiter flips it to False on retire/supersede. Default reads
        # gate on {"current": True} once that filter ships.
        "current": True,
    }
    if drawer:
        meta["drawer"] = drawer
    with _chroma_lock():
        col.add(ids=[drawer_id], documents=[content], metadatas=[meta])

    # Facts-first: mirror discrete facts into the KG, sourced back to this
    # memory (the "citation"). Best-effort and gated — never mine the
    # quarantined regex/speculative tier, and never let KG failure break
    # the memory write (prose storage is primary).
    extracted = 0
    if extract and not quarantined:
        try:
            kg = _default_kg()
            for f in extract_facts(content, default_subject=wing):
                kg.add_triple(f["subject"], f["predicate"], f["object"],
                              confidence=0.8, source=f"mem:{drawer_id}")
                extracted += 1
        except Exception:
            pass

    # Wiki-link binding: parse [[room-name]] tokens, resolve to existing
    # memory ids, write bidirectional linked_to KG triples. Best-effort —
    # binding failure must not break the primary write.
    links_created = []
    try:
        target_rooms = list(set(_LINK_RE.findall(content)))
        if target_rooms:
            target_ids = _resolve_link_targets(target_rooms, prefer_wing=wing)
            if target_ids:
                kg = _default_kg()
                for tid in target_ids:
                    if tid == drawer_id:
                        continue
                    kg.add_triple(drawer_id, "linked_to", tid,
                                  confidence=1.0, source=f"link:{room}")
                    kg.add_triple(tid, "linked_to", drawer_id,
                                  confidence=1.0, source=f"link:{room}")
                    links_created.append(tid)
    except Exception:
        pass

    return {"success": True, "id": drawer_id, "wing": wing, "hall": hall,
            "room": room, "confidence": confidence, "quarantined": quarantined,
            "facts_extracted": extracted,
            "links_created": links_created,
            **({"drawer": drawer} if drawer else {})}


def delete_memory(memory_id: str):
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    existing = col.get(ids=[memory_id])
    if not existing["ids"]:
        return {"success": False, "error": f"Not found: {memory_id}"}
    with _chroma_lock():
        col.delete(ids=[memory_id])
    return {"success": True, "id": memory_id}


def _believed_as_of(meta: dict, as_of: str) -> bool:
    """Transaction-time predicate for prose: was this memory part of
    current understanding on date `as_of` (YYYY-MM-DD)? True if filed
    on/before that date and not retired until after it. Mirrors the KG's
    as_believed axis. Absent/empty retired_at = never retired. Date
    granularity (first 10 chars) — filed_at is an ISO datetime, as_of a
    date; comparing full strings would mis-order same-day records."""
    filed = (meta.get("filed_at") or "")[:10]
    if filed and filed > as_of:
        return False
    ret = (meta.get("retired_at") or "")[:10]
    if ret and ret <= as_of:
        return False
    return True


def _linked_ids_for(direct_ids):
    """Return {memory_id: [linked_memory_ids]} from the KG for the given
    memory ids. One query covers all of them. Used by expand_links to
    co-surface wiki-linked memories alongside direct semantic hits."""
    if not direct_ids:
        return {}
    try:
        kg = _default_kg()
        conn = kg._conn()
        # The KG stores entity ids (sha1 of name) under subject/object;
        # for memory→memory triples written by add_memory, the names ARE
        # the drawer ids, so the entity id is sha1(drawer_id). Use the
        # same _eid hashing the KG uses.
        subj_eids = {kg._eid(mid): mid for mid in direct_ids}
        if not subj_eids:
            return {}
        placeholders = ",".join("?" * len(subj_eids))
        rows = conn.execute(
            f"""SELECT t.subject, e.name
                FROM triples t
                JOIN entities e ON e.id = t.object
                WHERE t.subject IN ({placeholders})
                  AND t.predicate = 'linked_to'
                  AND t.invalidated_at IS NULL
                  AND t.valid_to IS NULL""",
            tuple(subj_eids.keys()),
        ).fetchall()
        conn.close()
        out = {}
        for subj_eid, obj_name in rows:
            mid = subj_eids.get(subj_eid)
            if mid:
                out.setdefault(mid, []).append(obj_name)
        return out
    except Exception:
        return {}


def search_memories(query: str, wing: str = None, hall: str = None,
                    room: str = None, drawer: str = None, n_results: int = 5,
                    include_quarantined: bool = False,
                    include_historical: bool = False, as_of: str = None,
                    expand_links: bool = True):
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
    if not include_quarantined:
        where_clauses.append({"quarantined": False})
    # Currency gate. Default: only current understanding. as_of does
    # transaction-time travel (post-filter, since Chroma `where` can't
    # express "retired_at absent OR > D"), so it must NOT also pin
    # current=True or it would hide records that were current then but
    # are retired now. include_historical bypasses the gate entirely.
    if as_of is None and not include_historical:
        where_clauses.append({"current": True})

    where = None
    if len(where_clauses) > 1:
        where = {"$and": where_clauses}
    elif len(where_clauses) == 1:
        where = where_clauses[0]

    # as_of post-filters in Python, so over-fetch to refill what the
    # belief filter drops (semantic rank is approximate either way).
    fetch_n = n_results if as_of is None else min(max(n_results * 5, n_results), 100)
    kwargs = {"query_texts": [query], "n_results": fetch_n,
              "include": ["documents", "metadatas", "distances"]}
    if where:
        kwargs["where"] = where

    try:
        results = col.query(**kwargs)
    except Exception as e:
        return {"error": str(e), "results": []}

    hits = []
    for mid, doc, meta, dist in zip(results["ids"][0], results["documents"][0],
                                    results["metadatas"][0], results["distances"][0]):
        if as_of is not None and not _believed_as_of(meta, as_of):
            continue
        hits.append({
            "id": mid,
            "text": doc,
            "wing": meta.get("wing", "?"), "hall": meta.get("hall", "?"),
            "room": meta.get("room", "?"),
            "source_file": Path(meta.get("source_file", "")).name if meta.get("source_file") else "",
            "similarity": round(1 - dist, 3),
            "date": meta.get("date", ""),
            "confidence": meta.get("confidence", 1.0),
            "current": meta.get("current", True),
        })
        if len(hits) >= n_results:
            break

    # Wiki-link expansion: bring in [[linked]] memories that didn't make
    # the top-k cut. Tag each with `linked_via` so the caller (Claude or
    # a viz) can see why it's there. Disabled by passing expand_links=False.
    if expand_links and hits:
        try:
            direct_ids = [h["id"] for h in hits]
            already = set(direct_ids)
            linked = _linked_ids_for(direct_ids)
            extra_ids = []
            extra_sources = {}  # extra_id -> first source direct id that brought it in
            for src, lst in linked.items():
                for lid in lst:
                    if lid in already:
                        continue
                    if lid not in extra_sources:
                        extra_sources[lid] = src
                        extra_ids.append(lid)
                    already.add(lid)
            if extra_ids:
                fetched = col.get(ids=extra_ids, include=["documents", "metadatas"])
                for fid, fdoc, fmeta in zip(fetched.get("ids") or [],
                                            fetched.get("documents") or [],
                                            fetched.get("metadatas") or []):
                    # Honor the same filters the main query used.
                    if not include_quarantined and fmeta.get("quarantined"):
                        continue
                    if (as_of is None and not include_historical
                            and not fmeta.get("current", True)):
                        continue
                    if as_of is not None and not _believed_as_of(fmeta, as_of):
                        continue
                    hits.append({
                        "id": fid,
                        "text": fdoc,
                        "wing": fmeta.get("wing", "?"), "hall": fmeta.get("hall", "?"),
                        "room": fmeta.get("room", "?"),
                        "source_file": Path(fmeta.get("source_file", "")).name if fmeta.get("source_file") else "",
                        "similarity": None,  # not a similarity hit
                        "date": fmeta.get("date", ""),
                        "confidence": fmeta.get("confidence", 1.0),
                        "current": fmeta.get("current", True),
                        "linked_via": extra_sources[fid],
                    })
        except Exception:
            pass

    return {"query": query, "results": hits}


# ── Memory Layers ─────────────────────────────────────────────────────────────


class MemoryStack:
    def __init__(self):
        self.palace_path = PALACE_DB

    def wake_up(self, wing: str = None, include_quarantined: bool = False,
                include_historical: bool = False, as_of: str = None) -> str:
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
            wcl = []
            if wing:
                wcl.append({"wing": canonicalize_wing(wing)})
            if not include_quarantined:
                wcl.append({"quarantined": False})
            if as_of is None and not include_historical:
                wcl.append({"current": True})
            if len(wcl) > 1:
                kwargs["where"] = {"$and": wcl}
            elif wcl:
                kwargs["where"] = wcl[0]
            try:
                results = col.get(**kwargs)
                docs = results.get("documents", [])
                metas = results.get("metadatas", [])
                if as_of is not None:
                    kept = [(d, m) for d, m in zip(docs, metas)
                            if _believed_as_of(m, as_of)]
                    docs = [d for d, _ in kept]
                    metas = [m for _, m in kept]
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

    def recall(self, wing: str = None, room: str = None, n_results: int = 10,
               include_quarantined: bool = False,
               include_historical: bool = False, as_of: str = None) -> str:
        """L2: On-demand retrieval by wing/room."""
        col = _get_collection()
        if not col:
            return "No palace found."

        where_clauses = []
        if wing:
            where_clauses.append({"wing": canonicalize_wing(wing)})
        if room:
            where_clauses.append({"room": room})
        if not include_quarantined:
            where_clauses.append({"quarantined": False})
        if as_of is None and not include_historical:
            where_clauses.append({"current": True})

        where = None
        if len(where_clauses) > 1:
            where = {"$and": where_clauses}
        elif len(where_clauses) == 1:
            where = where_clauses[0]

        # as_of post-filters in Python; fetch unbounded so the limit is
        # applied AFTER the belief filter (else we'd cap before filtering
        # and under-return). Default path keeps the cheap get-side limit.
        kwargs = {"include": ["documents", "metadatas"]}
        if as_of is None:
            kwargs["limit"] = n_results
        if where:
            kwargs["where"] = where

        try:
            results = col.get(**kwargs)
        except Exception as e:
            return f"Error: {e}"

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])
        if as_of is not None:
            kept = [(d, m) for d, m in zip(docs, metas)
                    if _believed_as_of(m, as_of)][:n_results]
            docs = [d for d, _ in kept]
            metas = [m for _, m in kept]
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

    def search(self, query: str, wing: str = None, room: str = None,
               n_results: int = 5, include_quarantined: bool = False,
               include_historical: bool = False, as_of: str = None) -> str:
        """L3: Deep semantic search."""
        result = search_memories(query, wing=wing, room=room, n_results=n_results,
                                 include_quarantined=include_quarantined,
                                 include_historical=include_historical,
                                 as_of=as_of)
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
            with _chroma_lock():
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
    with _chroma_lock():
        for i in range(0, len(update_ids), batch):
            col.update(ids=update_ids[i:i + batch], metadatas=update_metas[i:i + batch])
    report["applied"] = True
    return report


# ── Contradiction Detection ──────────────────────────────────────────────────

# Predicates that are typically single-valued — >1 simultaneously-current
# object for the same subject is very likely a real contradiction rather
# than legitimate multi-valued truth (e.g. "uses" can have many objects).
_FUNCTIONAL_PREDICATES = {
    "status", "state", "phase", "tier", "plan", "version", "current_version",
    "uses_stack", "stack", "model", "default_model", "location", "located_at",
    "owner", "owned_by", "price", "pricing", "decided",
    "chose", "selected", "primary", "deployed_at", "runs_on", "hosts",
    "replaced_by", "is_a", "type", "role",
}


def detect_contradictions(entity: str = None, auto_flag: bool = False,
                           include_review: bool = False, limit: int = 50):
    """Find (subject, predicate) pairs the KG currently believes more than
    one distinct value for — two simultaneously-true answers to the same
    question.

    Operates on triples, not prose: precise by construction, zero false
    positives. (A regex pass over palace's long multi-topic memories was
    prototyped and dropped — it could not reach usable precision, which is
    itself the Sibyl 'don't fake confidence from a weak signal' lesson.)

    Reports — never deletes. Resolve real single-valued ones with
    palace_kg_supersede; multi-valued predicates surface as 'review'.
    """
    kg = _default_kg()
    conn = kg._conn()
    q = """
        SELECT s.name, t.predicate, o.name, t.valid_from, t.created_at,
               t.source, t.id
        FROM triples t
        JOIN entities s ON t.subject = s.id
        JOIN entities o ON t.object  = o.id
        WHERE t.invalidated_at IS NULL AND t.valid_to IS NULL
    """
    params = []
    if entity:
        eid = kg._eid(entity)
        q += " AND (s.id = ? OR o.id = ?)"
        params += [eid, eid]
    q += " ORDER BY s.name, t.predicate, t.created_at"
    rows = conn.execute(q, params).fetchall()
    conn.close()

    groups = defaultdict(list)
    for subj, pred, obj, vf, cre, src, tid in rows:
        groups[(subj, pred)].append({
            "object": obj, "valid_from": vf, "created_at": cre,
            "source": src, "triple_id": tid,
        })

    found = []
    for (subj, pred), vals in groups.items():
        if len({v["object"] for v in vals}) < 2:
            continue
        vals.sort(key=lambda v: v["created_at"] or "")
        objs = sorted({v["object"] for v in vals})
        # Compact by design: the full per-triple `values` arrays were ~63%
        # of the payload and blew the MCP token limit on the live KG. Keep
        # the actionable bits (distinct values, ends, a few triple_ids for
        # supersede) and drop the firehose.
        found.append({
            "subject": subj, "predicate": pred,
            "confidence": "high" if pred in _FUNCTIONAL_PREDICATES else "review",
            "distinct_values": objs[:8],
            "value_count": len(objs),
            "oldest": vals[0]["object"], "newest": vals[-1]["object"],
            "triple_ids": [v["triple_id"] for v in vals][:6],
        })
    found.sort(key=lambda x: 0 if x["confidence"] == "high" else 1)

    high = [c for c in found if c["confidence"] == "high"]
    review = [c for c in found if c["confidence"] == "review"]
    shown = high + (review if include_review else [])

    flagged = 0
    if auto_flag:
        for c in high:
            content = (
                f"KG CONTRADICTION ({c['subject']} -> {c['predicate']}): "
                f"{c['value_count']} values believed true at once: "
                f"{c['distinct_values']}. Oldest '{c['oldest']}', newest "
                f"'{c['newest']}'. If single-valued, resolve via "
                f"palace_kg_supersede(subject='{c['subject']}', predicate="
                f"'{c['predicate']}', old_object=<stale>, new_object="
                f"'{c['newest']}'). No triple was deleted."
            )
            if add_memory(wing="global", hall="feedback",
                          room="kg-contradictions", content=content,
                          added_by="contradiction-detector").get("success"):
                flagged += 1

    return {
        "groups_checked": len(groups),
        "summary": {"high": len(high), "review": len(review)},
        "contradictions": shown[:limit],
        "truncated": len(shown) > limit,
        "review_hidden": (not include_review),
        "flagged": flagged,
        "auto_flag": auto_flag,
    }


# ── Facts-First Capture ──────────────────────────────────────────────────────

# Conservative, high-precision only. Free-prose fact mining is a noise trap
# (see the contradiction-detector pivot) — better to miss facts than to
# pollute the KG. Each pattern requires explicit structure; the object is
# cleaned and length-bounded. subj_grp=None means "use the memory's subject
# hint" (the wing), used only for self-referential decision/stack patterns.
# Survivors of full-corpus precision testing. Earlier candidates were
# dropped after measurement, not guesswork:
#   - "X uses Y" / "decided Y"  -> garbage subjects/objects
#   - "X vN" version pattern    -> ~7% precision (matched decomp tokens
#                                   like "His version 026")
# Generic prose fact mining does not reach KG-safe precision over a
# heterogeneous corpus; only cue-guarded structural patterns do. Better
# to capture few correct facts than pollute the graph (the Sibyl lesson
# this feature exists to honor). deployed_to objects are guarded to
# real targets (path/host) by _looks_like_target below.
_FACT_PATTERNS = [
    (re.compile(r"\bdeployed (?:to|at|on)\s+([\w:/\\.~+-]{4,60})", re.I),
     "deployed_to", None, 1),
    (re.compile(r"(?:\brepo\b|\bremote\b|mirror(?:ed)?(?:\s+at)?|\borigin\b|"
                r"hosted (?:at|on))[^\n]{0,30}?github\.com/([\w.-]+/[\w.-]+?)"
                r"(?=[\s.,;:)]|$)", re.I),
     "repo", None, 1),
]
_FACT_OBJ_STOP = {"the", "this", "that", "it", "them", "a", "an", "our",
                  "your", "their", "its", "these", "those", "now"}
# Lone common words that are almost always sentence/product fragments
# ("Cloud Run" -> subject "Run"), not real entities.
_FACT_SUBJ_STOP = {"run", "cloud", "the", "this", "it", "node", "app",
                   "api", "web", "data", "test", "prod", "build", "main",
                   "core", "new", "old", "next", "sync", "file", "set"}
# Generic deploy targets that carry no information as an object.
_DEPLOY_OBJ_STOP = {"cloud", "prod", "production", "staging", "local",
                    "localhost", "here", "there", "disk", "memory", "it"}


def extract_facts(content: str, default_subject: str = None):
    """Pull a small set of high-precision (subject, predicate, object)
    triples from memory prose. Deterministic, no LLM. Returns a list of
    dicts; empty when nothing is confidently extractable (the common,
    acceptable case)."""
    facts = []
    seen = set()
    for rx, pred, sgrp, ogrp in _FACT_PATTERNS:
        for m in rx.finditer(content):
            subj = (m.group(sgrp).strip() if sgrp else default_subject)
            obj = m.group(ogrp).strip().strip(".,;:'\"")
            if not subj or not obj:
                continue
            if obj.lower() in _FACT_OBJ_STOP or len(obj) < 2 or len(obj) > 40:
                continue
            if subj.lower() == obj.lower() or subj.lower() in _FACT_SUBJ_STOP:
                continue
            if pred == "deployed_to":
                # Real target = has a path/host delimiter, or is a proper
                # noun (capitalized, not a common word). Rejects prose
                # like "deployed to match/land/a different env".
                looks_target = (any(d in obj for d in "./\\:")
                                or (obj[:1].isupper()
                                    and obj.lower() not in _DEPLOY_OBJ_STOP))
                if obj.lower() in _DEPLOY_OBJ_STOP or not looks_target:
                    continue
            key = (subj.lower(), pred, obj.lower())
            if key in seen:
                continue
            seen.add(key)
            facts.append({"subject": subj, "predicate": pred,
                          "object": obj, "source_pattern": pred})
    return facts


# ── Confidence / Quarantine ──────────────────────────────────────────────────


def backfill_confidence(dry_run: bool = True):
    """Stamp confidence + quarantined on memories that predate the field,
    derived from provenance. Required before the quarantine filter works:
    Chroma metadata predicates only match records that HAVE the key, so a
    record with no `quarantined` would be invisible to default retrieval
    until backfilled. Metadata-only rewrite; dry-run by default.
    """
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}

    data = col.get(include=["metadatas"])
    ids, metas = data.get("ids", []), data.get("metadatas", [])
    upd_ids, upd_metas = [], []
    tiers = defaultdict(int)
    for mid, m in zip(ids, metas):
        conf, q = _derive_confidence(m.get("added_by", ""), m.get("room", ""))
        tiers[f"{conf}{'/q' if q else ''}"] += 1
        if m.get("confidence") != conf or m.get("quarantined") != q:
            nm = dict(m)
            nm["confidence"], nm["quarantined"] = conf, q
            upd_ids.append(mid)
            upd_metas.append(nm)

    report = {"total": len(ids), "to_rewrite": len(upd_ids),
              "tier_distribution": dict(tiers), "applied": False}
    if dry_run or not upd_ids:
        return report
    with _chroma_lock():
        for i in range(0, len(upd_ids), 200):
            col.update(ids=upd_ids[i:i + 200], metadatas=upd_metas[i:i + 200])
    report["applied"] = True
    return report


def backfill_currency(dry_run: bool = True):
    """Stamp the currency filter key (`current`) on every memory, and
    opportunistically repair any record missing the trust keys
    (`quarantined`/`confidence`) in the same walk.

    Comprehensiveness is a correctness requirement here, not a nicety:
    Chroma `where` only matches records that HAVE the key, so a record
    missing `current` becomes invisible to default retrieval the moment
    the currency filter ships. Safeguards:
      - FILL missing keys only — never overwrite an existing value. Safe
        to re-run after retire/supersede land (won't revive a retired
        memory) and treats a synced remote's currency state as
        authoritative.
      - Reconcile enumerated vs col.count(); refuse to apply on a partial
        view (never rewrite a fraction and call it done).
      - On apply, re-scan and assert zero keyless records remain. This is
        the gate the read-path currency filter checks before shipping.
    Metadata-only rewrite; dry-run by default.
    """
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}

    total = col.count()
    data = col.get(include=["metadatas"])
    ids, metas = data.get("ids", []), data.get("metadatas", [])

    miss_current = miss_quar = miss_conf = 0
    keyless_by_added_by = defaultdict(int)
    upd_ids, upd_metas = [], []
    for mid, m in zip(ids, metas):
        nm = dict(m)
        changed = False
        if "current" not in nm:
            nm["current"] = True
            miss_current += 1
            keyless_by_added_by[nm.get("added_by", "?")] += 1
            changed = True
        if "confidence" not in nm or "quarantined" not in nm:
            conf, q = _derive_confidence(nm.get("added_by", ""), nm.get("room", ""))
            if "confidence" not in nm:
                nm["confidence"] = conf
                miss_conf += 1
                changed = True
            if "quarantined" not in nm:
                nm["quarantined"] = q
                miss_quar += 1
                changed = True
        if changed:
            upd_ids.append(mid)
            upd_metas.append(nm)

    reconciled = len(ids) == total
    report = {
        "collection_count": total,
        "enumerated": len(ids),
        "reconciled": reconciled,
        "missing_current": miss_current,
        "missing_quarantined": miss_quar,
        "missing_confidence": miss_conf,
        "to_rewrite": len(upd_ids),
        "keyless_by_added_by": dict(keyless_by_added_by),
        "applied": False,
    }
    if not reconciled:
        report["error"] = (
            f"enumeration ({len(ids)}) != collection count ({total}); "
            f"refusing to apply on a partial view"
        )
        return report
    if dry_run or not upd_ids:
        return report

    with _chroma_lock():
        for i in range(0, len(upd_ids), 200):
            col.update(ids=upd_ids[i:i + 200], metadatas=upd_metas[i:i + 200])

    verify = col.get(include=["metadatas"])
    still_keyless = sum(1 for mm in verify.get("metadatas", [])
                        if "current" not in mm)
    report["applied"] = True
    report["post_apply_keyless_current"] = still_keyless
    report["verified_zero_keyless"] = still_keyless == 0
    return report


def backfill_facts(wing: str = None, dry_run: bool = True):
    """Run the fact extractor over existing non-quarantined memories and
    mirror discoveries into the KG, sourced to each memory. Dry-run by
    default; scope by wing to bound volume. Idempotent — add_triple
    dedups identical currently-believed triples."""
    col = _get_collection()
    if not col:
        return {"error": "No palace found"}
    where = {"quarantined": False}
    if wing:
        where = {"$and": [{"wing": canonicalize_wing(wing)}, where]}
    got = col.get(where=where, include=["documents", "metadatas"])
    ids = got.get("ids", [])
    plan = []
    for mid, doc, m in zip(ids, got.get("documents", []), got.get("metadatas", [])):
        for f in extract_facts(doc, default_subject=m.get("wing")):
            plan.append({**f, "memory_id": mid})

    report = {"scanned": len(ids), "facts_found": len(plan),
              "sample": plan[:15], "applied": False}
    if dry_run or not plan:
        return report
    kg = _default_kg()
    for f in plan:
        kg.add_triple(f["subject"], f["predicate"], f["object"],
                      confidence=0.8, source=f"mem:{f['memory_id']}")
    report["applied"] = True
    report["added"] = len(plan)
    return report


def promote_memory(memory_id: str, confidence: float = 1.0):
    """Lift a quarantined memory into first-class retrieval (verified
    good). The reverse of quarantine — the explicit promotion path."""
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    got = col.get(ids=[memory_id], include=["metadatas"])
    if not got.get("ids"):
        return {"success": False, "error": f"Not found: {memory_id}"}
    meta = dict(got["metadatas"][0])
    was = {"confidence": meta.get("confidence"), "quarantined": meta.get("quarantined")}
    meta["confidence"] = confidence
    meta["quarantined"] = confidence < 0.5
    prev = meta.get("added_by", "")
    if "promoted" not in prev:
        meta["added_by"] = (prev + "+promoted").lstrip("+")
    with _chroma_lock():
        col.update(ids=[memory_id], metadatas=[meta])
    return {"success": True, "id": memory_id, "was": was,
            "now": {"confidence": confidence, "quarantined": meta["quarantined"]}}


# ── Prose Currency ───────────────────────────────────────────────────────────
# The prose analogue of the KG's invalidate/supersede. Retire is the
# transaction-time retraction: the memory stops being current understanding
# but is never deleted — reachable via include_historical / as_of, exactly
# the "keep it clean for the working AI, don't lose the archive" split.
# All metadata-only and reversible (Chroma cannot delete keys, so restore
# blanks the retire fields; they are descriptive payload, never filter
# keys, so an empty string is semantically clean).


def retire_memory(memory_id: str, reason: str = "stale",
                   superseded_by: str = None):
    """Drop a memory out of current understanding. Sets current=False so
    the default read paths stop returning it; the record stays in the
    store for include_historical / as_of."""
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    got = col.get(ids=[memory_id], include=["metadatas"])
    if not got.get("ids"):
        return {"success": False, "error": f"Not found: {memory_id}"}
    meta = dict(got["metadatas"][0])
    was_current = meta.get("current", True)
    meta["current"] = False
    meta["retired_at"] = datetime.now().isoformat()
    meta["retired_reason"] = reason
    if superseded_by:
        meta["superseded_by"] = superseded_by
    with _chroma_lock():
        col.update(ids=[memory_id], metadatas=[meta])
    return {"success": True, "id": memory_id, "was_current": was_current,
            "retired_reason": reason, "superseded_by": superseded_by or "",
            "note": "excluded from default retrieval; reachable via "
                    "include_historical / as_of"}


def restore_memory(memory_id: str):
    """Inverse of retire — return a memory to current understanding."""
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    got = col.get(ids=[memory_id], include=["metadatas"])
    if not got.get("ids"):
        return {"success": False, "error": f"Not found: {memory_id}"}
    meta = dict(got["metadatas"][0])
    was = {"current": meta.get("current", True),
           "retired_reason": meta.get("retired_reason", "")}
    meta["current"] = True
    meta["retired_at"] = ""
    meta["retired_reason"] = ""
    meta["superseded_by"] = ""
    with _chroma_lock():
        col.update(ids=[memory_id], metadatas=[meta])
    return {"success": True, "id": memory_id, "was": was}


def supersede_memory(old_id: str, new_id: str):
    """Mark old_id as replaced by new_id. Same-wing scope guard: prose
    supersession lineage never crosses a wing — the structural antidote to
    flat current/archive scope loss (a memory true in another wing is not
    a stale version of this one)."""
    if old_id == new_id:
        return {"success": False, "error": "old_id and new_id are the same"}
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    got = col.get(ids=[old_id, new_id], include=["metadatas"])
    found = dict(zip(got.get("ids", []), got.get("metadatas", [])))
    if old_id not in found:
        return {"success": False, "error": f"old_id not found: {old_id}"}
    if new_id not in found:
        return {"success": False, "error": f"new_id not found: {new_id}"}
    ow = canonicalize_wing(found[old_id].get("wing", ""))
    nw = canonicalize_wing(found[new_id].get("wing", ""))
    if ow != nw:
        return {"success": False,
                "error": f"scope violation: old wing '{ow}' != new wing "
                         f"'{nw}'; supersession may not cross wings"}
    r = retire_memory(old_id, reason="superseded", superseded_by=new_id)
    if not r.get("success"):
        return r
    return {"success": True, "superseded": old_id, "now": new_id, "wing": ow}


def supersede_with(old_id: str, new_content: str, wing: str = None,
                   hall: str = None, room: str = None, drawer: str = None,
                   added_by: str = "claude"):
    """Convenience: mint a replacement memory (inheriting old's scope
    unless overridden), then supersede. Wrapper over add_memory +
    supersede_memory."""
    col = _get_collection()
    if not col:
        return {"success": False, "error": "No palace found"}
    got = col.get(ids=[old_id], include=["metadatas"])
    if not got.get("ids"):
        return {"success": False, "error": f"old_id not found: {old_id}"}
    om = got["metadatas"][0]
    res = add_memory(
        wing=wing or om.get("wing", "unknown"),
        hall=hall or om.get("hall", "project"),
        room=room or om.get("room", "general"),
        content=new_content, added_by=added_by,
        drawer=drawer or om.get("drawer"),
    )
    if not res.get("success"):
        return {"success": False,
                "error": f"new memory not created: "
                         f"{res.get('reason', res.get('error', '?'))}",
                "hint": "if duplicate, call supersede_memory(old_id, "
                        "<existing id>) directly"}
    sup = supersede_memory(old_id, res["id"])
    return {**sup, "new_id": res.get("id")}


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
        print("  python palace.py contradictions [--flag] [entity]  KG conflicting facts")
        print("  python palace.py backfill-confidence [--apply] Stamp confidence/quarantine")
        print("  python palace.py backfill-currency [--apply]  Stamp `current` + repair keyless")
        print("  python palace.py retire <memory_id> [reason]  Drop from current understanding")
        print("  python palace.py restore <memory_id>          Return a memory to current")
        print("  python palace.py supersede <old_id> <new_id>  old replaced by new (same wing)")
        print("  python palace.py promote <memory_id> [conf]   Un-quarantine a memory")
        print("  python palace.py backfill-facts [--apply] [wing]  Mirror facts to KG")
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

    elif cmd == "contradictions":
        flag = "--flag" in sys.argv
        ent = next((a for a in sys.argv[2:] if not a.startswith("-")), None)
        print(f"Scanning KG for contradictions{f' on {ent}' if ent else ''}"
              f"{' (flagging)' if flag else ''}...")
        r = detect_contradictions(entity=ent, auto_flag=flag,
                                  include_review="--review" in sys.argv)
        print(json.dumps(r, indent=2))

    elif cmd in ("backfill-confidence", "backfill_confidence"):
        apply = "--apply" in sys.argv
        print(f"Backfilling confidence{' (APPLYING)' if apply else ' (dry run)'}...")
        print(json.dumps(backfill_confidence(dry_run=not apply), indent=2))

    elif cmd in ("backfill-currency", "backfill_currency"):
        apply = "--apply" in sys.argv
        print(f"Backfilling currency{' (APPLYING)' if apply else ' (dry run)'}...")
        print(json.dumps(backfill_currency(dry_run=not apply), indent=2))

    elif cmd == "retire":
        if len(sys.argv) < 3:
            print("Usage: python palace.py retire <memory_id> [reason]")
            sys.exit(1)
        reason = sys.argv[3] if len(sys.argv) > 3 else "stale"
        print(json.dumps(retire_memory(sys.argv[2], reason=reason), indent=2))

    elif cmd == "restore":
        if len(sys.argv) < 3:
            print("Usage: python palace.py restore <memory_id>")
            sys.exit(1)
        print(json.dumps(restore_memory(sys.argv[2]), indent=2))

    elif cmd == "supersede":
        if len(sys.argv) < 4:
            print("Usage: python palace.py supersede <old_id> <new_id>")
            sys.exit(1)
        print(json.dumps(supersede_memory(sys.argv[2], sys.argv[3]), indent=2))

    elif cmd == "promote":
        if len(sys.argv) < 3:
            print("Usage: python palace.py promote <memory_id> [confidence]")
            sys.exit(1)
        mid = sys.argv[2]
        conf = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        print(json.dumps(promote_memory(mid, confidence=conf), indent=2))

    elif cmd in ("backfill-facts", "backfill_facts"):
        apply = "--apply" in sys.argv
        w = next((a for a in sys.argv[2:] if not a.startswith("-")), None)
        print(f"Backfilling facts{f' for {w}' if w else ''}"
              f"{' (APPLYING)' if apply else ' (dry run)'}...")
        print(json.dumps(backfill_facts(wing=w, dry_run=not apply), indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
