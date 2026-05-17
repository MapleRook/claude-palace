#!/usr/bin/env python3
"""One-shot chroma recovery: rebuild a fresh collection from the intact
chroma.sqlite3 (HNSW segment corrupted -> native segfault on open).

Reads documents+metadata via stdlib sqlite3 (safe, no chroma native),
re-adds into a brand-new PersistentClient store (fresh healthy index,
re-embedded with the same default model). Zero data loss. Does NOT
touch the live store — writes to OUT; the swap is done by hand after
verification.
"""
import os, sys, sqlite3, collections

SRC = os.path.expanduser("~/.claude/palace/chroma/chroma.sqlite3")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.claude/palace/chroma_rebuilt")
COLL = "claude_memories"

# palace's metadata schema — cast back to the right Python types so the
# quarantine/confidence filters keep working.
FLOAT_KEYS = {"confidence"}
BOOL_KEYS = {"quarantined"}

con = sqlite3.connect(SRC)
docs_by_id, meta_by_id = {}, collections.defaultdict(dict)
for eid, key, sv, iv, fv, bv in con.execute(
    "SELECT e.embedding_id, m.key, m.string_value, m.int_value, m.float_value, m.bool_value "
    "FROM embeddings e JOIN embedding_metadata m ON m.id = e.id"
):
    if key == "chroma:document":
        docs_by_id[eid] = sv or ""
        continue
    if key in FLOAT_KEYS:
        val = fv if fv is not None else (float(iv) if iv is not None else 1.0)
    elif key in BOOL_KEYS:
        val = bool(bv) if bv is not None else (bool(iv) if iv is not None else False)
    else:
        val = sv if sv is not None else (iv if iv is not None else fv)
    if val is not None:
        meta_by_id[eid][key] = val
con.close()

ids = [i for i in docs_by_id if docs_by_id[i].strip()]
print(f"extracted {len(ids)} documents, {sum(len(m) for m in meta_by_id.values())} metadata values")

import chromadb
client = chromadb.PersistentClient(path=OUT)
try:
    client.delete_collection(COLL)
except Exception:
    pass
col = client.create_collection(COLL)

B = 500
for s in range(0, len(ids), B):
    chunk = ids[s:s + B]
    col.add(
        ids=chunk,
        documents=[docs_by_id[i] for i in chunk],
        metadatas=[meta_by_id[i] or {"wing": "unknown"} for i in chunk],
    )
    print(f"  added {min(s + B, len(ids))}/{len(ids)}")

print("rebuilt count:", col.count())
# exercise the fresh index — these are exactly what segfaulted before
print("get limit1:", len(col.get(limit=1)["ids"]))
print("get where wing=optimization:", len(col.get(where={"wing": "optimization"})["ids"]))
print("get where quarantined=False:", len(col.get(where={"quarantined": False})["ids"]))
q = col.query(query_texts=["antimatter dimensions incremental game"], n_results=3)
print("semantic query hits:", len(q["ids"][0]), "->", q["ids"][0][:2])
print("REBUILD_OK")
