#!/usr/bin/env python3
"""
palace_sync.py — Export/Import palace for cross-machine sync
=============================================================

ChromaDB's internal storage format isn't portable across machines
(different paths, different embedding model caches). Instead we
export to a portable JSONL format and reimport on the other side.

Usage:
  python palace_sync.py export [output_dir]   Export palace to JSONL + SQLite
  python palace_sync.py import [input_dir]    Import palace from JSONL + SQLite
  python palace_sync.py status                Show export stats

Default dir: ~/.claude/memory-sync/palace/
"""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from palace import (
    PALACE_DB, KG_DB, COLLECTION_NAME,
    _get_collection, _ensure_dirs, add_memory,
)

DEFAULT_SYNC_DIR = os.path.expanduser("~/.claude/memory-sync/palace")


def cmd_export(sync_dir: str = None):
    """Export all ChromaDB memories to JSONL and copy SQLite KG."""
    sync_dir = sync_dir or DEFAULT_SYNC_DIR
    Path(sync_dir).mkdir(parents=True, exist_ok=True)

    col = _get_collection()
    if not col:
        print("No palace to export.")
        return

    # Export ChromaDB memories to JSONL
    results = col.get(include=["documents", "metadatas"])
    ids = results.get("ids", [])
    docs = results.get("documents", [])
    metas = results.get("metadatas", [])

    jsonl_path = os.path.join(sync_dir, "memories.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for doc_id, doc, meta in zip(ids, docs, metas):
            entry = {
                "id": doc_id,
                "content": doc,
                "metadata": meta,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Copy SQLite KG directly (it's small and portable)
    kg_dest = os.path.join(sync_dir, "kg.sqlite3")
    if os.path.exists(KG_DB):
        shutil.copy2(KG_DB, kg_dest)

    # Write export manifest
    manifest = {
        "exported_at": datetime.now().isoformat(),
        "memory_count": len(ids),
        "kg_size_bytes": os.path.getsize(KG_DB) if os.path.exists(KG_DB) else 0,
        "machine": os.environ.get("COMPUTERNAME", "unknown"),
    }
    with open(os.path.join(sync_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Exported {len(ids)} memories + KG to {sync_dir}")
    return len(ids)


def cmd_import(sync_dir: str = None):
    """Import memories from JSONL into ChromaDB, merge KG."""
    sync_dir = sync_dir or DEFAULT_SYNC_DIR
    jsonl_path = os.path.join(sync_dir, "memories.jsonl")
    kg_source = os.path.join(sync_dir, "kg.sqlite3")

    if not os.path.exists(jsonl_path):
        print(f"No export found at {jsonl_path}")
        return

    import chromadb

    _ensure_dirs()
    client = chromadb.PersistentClient(path=PALACE_DB)
    col = client.get_or_create_collection(COLLECTION_NAME)

    # Get existing IDs to avoid duplicates
    existing_ids = set()
    try:
        existing = col.get(include=[])
        existing_ids = set(existing.get("ids", []))
    except Exception:
        pass

    imported = 0
    skipped = 0

    with open(jsonl_path, "r", encoding="utf-8") as f:
        batch_ids = []
        batch_docs = []
        batch_metas = []

        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            doc_id = entry.get("id", "")
            content = entry.get("content", "")
            metadata = entry.get("metadata", {})

            if doc_id in existing_ids:
                skipped += 1
                continue

            batch_ids.append(doc_id)
            batch_docs.append(content)
            batch_metas.append(metadata)
            imported += 1

            # Batch add every 50
            if len(batch_ids) >= 50:
                col.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
                batch_ids, batch_docs, batch_metas = [], [], []

        # Flush remaining
        if batch_ids:
            col.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)

    # Merge KG: import triples that don't exist locally
    if os.path.exists(kg_source):
        import sqlite3

        _ensure_dirs()
        # Ensure local KG exists
        from palace import KnowledgeGraph
        kg = KnowledgeGraph()

        remote_conn = sqlite3.connect(kg_source, timeout=10)
        local_conn = sqlite3.connect(KG_DB, timeout=10)

        # Import entities
        for row in remote_conn.execute("SELECT id, name, type, properties FROM entities").fetchall():
            local_conn.execute(
                "INSERT OR IGNORE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
                row
            )

        # Import triples (skip if identical active triple exists)
        for row in remote_conn.execute(
            "SELECT id, subject, predicate, object, valid_from, valid_to, confidence, source FROM triples"
        ).fetchall():
            existing = local_conn.execute(
                "SELECT id FROM triples WHERE id = ?", (row[0],)
            ).fetchone()
            if not existing:
                local_conn.execute(
                    "INSERT INTO triples (id, subject, predicate, object, valid_from, valid_to, confidence, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    row
                )

        local_conn.commit()

        remote_entities = remote_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        remote_triples = remote_conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        remote_conn.close()
        local_conn.close()

        print(f"KG merged: {remote_entities} entities, {remote_triples} triples from remote")

    print(f"Import complete: {imported} new memories, {skipped} already existed")
    return imported


def cmd_status(sync_dir: str = None):
    """Show export status."""
    sync_dir = sync_dir or DEFAULT_SYNC_DIR
    manifest_path = os.path.join(sync_dir, "manifest.json")

    if not os.path.exists(manifest_path):
        print(f"No export found at {sync_dir}")
        return

    with open(manifest_path) as f:
        manifest = json.load(f)

    print(f"Last export: {manifest.get('exported_at', '?')}")
    print(f"Machine: {manifest.get('machine', '?')}")
    print(f"Memories: {manifest.get('memory_count', 0)}")
    print(f"KG size: {manifest.get('kg_size_bytes', 0)} bytes")

    # Compare with local
    col = _get_collection()
    if col:
        local_count = col.count()
        remote_count = manifest.get("memory_count", 0)
        diff = local_count - remote_count
        print(f"Local: {local_count} memories ({'+' if diff >= 0 else ''}{diff} vs export)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python palace_sync.py export [dir]   Export palace to portable format")
        print("  python palace_sync.py import [dir]   Import palace from portable format")
        print("  python palace_sync.py status [dir]   Show export stats")
        sys.exit(0)

    cmd = sys.argv[1]
    sync_dir = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "export":
        cmd_export(sync_dir)
    elif cmd == "import":
        cmd_import(sync_dir)
    elif cmd == "status":
        cmd_status(sync_dir)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
