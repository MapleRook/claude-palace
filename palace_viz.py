"""palace_viz.py — bitemporal palace timeline.

Reads ~/.claude/palace/kg.sqlite3 and ~/.claude/palace/chroma/chroma.sqlite3
directly (sqlite3 stdlib, no chromadb dep). Emits a single self-contained
HTML file with embedded JSON + JS. Opens in your browser.

Two axes:
  - valid-time:    when the fact was true in the world (valid_from / date)
  - believed-time: when palace stored / retired it (extracted_at / filed_at)

Currency glyphs: current / retired / superseded / quarantined.

Usage:
    python palace_viz.py                 # write palace_timeline.html + open
    python palace_viz.py --out path.html
    python palace_viz.py --no-open
"""
import argparse
import json
import os
import sqlite3
import webbrowser
from pathlib import Path

PALACE_DIR = Path(os.path.expanduser("~")) / ".claude" / "palace"
KG_DB = PALACE_DIR / "kg.sqlite3"
CHROMA_DB = PALACE_DIR / "chroma" / "chroma.sqlite3"


def load_kg_triples():
    if not KG_DB.exists():
        return []
    conn = sqlite3.connect(str(KG_DB))
    rows = list(conn.execute("""
        SELECT t.subject, e1.name,
               t.predicate,
               t.object, e2.name,
               t.valid_from, t.valid_to,
               t.confidence, t.source,
               t.extracted_at, t.last_verified, t.invalidated_at
        FROM triples t
        LEFT JOIN entities e1 ON e1.id = t.subject
        LEFT JOIN entities e2 ON e2.id = t.object
    """))
    conn.close()
    return [{
        "kind": "triple",
        "subject_id": r[0], "subject": r[1] or r[0],
        "predicate": r[2],
        "object_id": r[3], "object": r[4] or r[3],
        "valid_from": r[5], "valid_to": r[6],
        "confidence": r[7], "source": r[8],
        "extracted_at": r[9], "last_verified": r[10], "invalidated_at": r[11],
    } for r in rows]


def load_prose():
    if not CHROMA_DB.exists():
        return []
    conn = sqlite3.connect(str(CHROMA_DB))
    by_id = {}
    for mid, key, sv, iv, fv, bv in conn.execute(
        "SELECT id, key, string_value, int_value, float_value, bool_value "
        "FROM embedding_metadata"
    ):
        val = sv if sv is not None else (iv if iv is not None else (fv if fv is not None else bv))
        by_id.setdefault(mid, {})[key] = val
    conn.close()
    out = []
    for mid, meta in by_id.items():
        doc = meta.get("chroma:document") or ""
        title = doc.split("\n", 1)[0][:140].strip() or f"(empty memory #{mid})"
        out.append({
            "kind": "prose",
            "id": mid,
            "title": title,
            "doc": doc,
            "wing": meta.get("wing"),
            "hall": meta.get("hall"),
            "room": meta.get("room"),
            "date": meta.get("date"),
            "filed_at": meta.get("filed_at"),
            "current": meta.get("current"),
            "retired_at": meta.get("retired_at"),
            "retired_reason": meta.get("retired_reason"),
            "superseded_by": meta.get("superseded_by"),
            "quarantined": meta.get("quarantined"),
            "confidence": meta.get("confidence"),
        })
    return out


HTML = r"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>palace timeline</title>
<style>
  body { font: 13px/1.4 -apple-system, Segoe UI, Helvetica, sans-serif;
         background: #1a1a1a; color: #e0e0e0; margin: 0; }
  header { padding: 12px 20px; background: #222; border-bottom: 1px solid #333;
           position: sticky; top: 0; z-index: 10; }
  h1 { margin: 0 0 8px; font-size: 16px; font-weight: 600; }
  .meta { color: #888; font-size: 11px; }
  .controls { display: flex; gap: 12px; margin-top: 10px; flex-wrap: wrap; align-items: center; }
  .controls label { font-size: 11px; color: #aaa; }
  .controls input, .controls select { background: #1a1a1a; color: #e0e0e0;
        border: 1px solid #444; padding: 4px 8px; border-radius: 3px; font: inherit; }
  .axis-toggle button { background: #2a2a2a; color: #aaa; border: 1px solid #444;
        padding: 4px 10px; cursor: pointer; font: inherit; }
  .axis-toggle button.active { background: #444; color: #fff; border-color: #666; }
  main { padding: 20px; max-width: 1100px; margin: 0 auto; padding-right: 420px; }
  .row { display: grid; grid-template-columns: 100px 120px 1fr; gap: 12px;
         padding: 6px 8px; border-bottom: 1px solid #2a2a2a; cursor: pointer; }
  .row:hover { background: #252525; }
  .row.retired { opacity: 0.55; }
  .date { color: #888; font-size: 11px; font-family: ui-monospace, Consolas, monospace; }
  .wing { font-size: 10px; color: #88aaff; text-transform: lowercase; }
  .body { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .glyph { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           margin-right: 6px; vertical-align: middle; }
  .glyph.current { background: #6c6; }
  .glyph.retired { background: #c66; }
  .glyph.superseded { background: #cc6; }
  .glyph.quarantined { background: #c8a; }
  .pred { color: #b88; font-style: italic; }
  .kind-prose .title { color: #ddd; }
  .day-sep { padding: 18px 0 4px; color: #555; font-size: 11px;
             font-family: ui-monospace, Consolas, monospace;
             border-bottom: 1px solid #333; margin-top: 4px; }
  .detail { position: fixed; right: 20px; top: 140px; width: 380px; max-height: calc(100vh - 160px);
            overflow-y: auto; background: #2a2a2a; border: 1px solid #444;
            border-radius: 4px; padding: 14px; display: none; font-size: 12px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.5); z-index: 20; }
  .detail.open { display: block; }
  .detail h3 { margin: 0 0 8px; font-size: 13px; }
  .detail pre { white-space: pre-wrap; word-wrap: break-word; font-size: 11px;
                background: #1a1a1a; padding: 8px; border-radius: 3px;
                max-height: 40vh; overflow-y: auto; }
  .detail .meta-line { color: #888; margin: 2px 0; }
  .detail .meta-line b { color: #ccc; font-weight: normal; }
  .detail button.close { position: absolute; top: 8px; right: 8px;
                          background: none; color: #888; border: none;
                          cursor: pointer; font-size: 16px; }
  .empty { color: #555; padding: 40px; text-align: center; }
  .legend { color: #777; font-size: 11px; margin-left: 12px; }
</style>
<body>
<header>
  <h1>palace timeline</h1>
  <div class="meta" id="counts"></div>
  <div class="controls">
    <span class="axis-toggle">
      <button id="ax-valid" class="active">valid-time</button>
      <button id="ax-believed">believed-time</button>
    </span>
    <label>wing
      <select id="f-wing"><option value="">(all)</option></select>
    </label>
    <label>kind
      <select id="f-kind">
        <option value="">(all)</option>
        <option value="prose">prose</option>
        <option value="triple">triple</option>
      </select>
    </label>
    <label>show
      <select id="f-state">
        <option value="">all</option>
        <option value="current">current only</option>
        <option value="retired">retired/superseded only</option>
      </select>
    </label>
    <label>search
      <input type="search" id="f-q" placeholder="text, entity, predicate" style="width:200px">
    </label>
    <span class="legend">
      <span class="glyph current"></span>current
      <span class="glyph retired" style="margin-left:8px"></span>retired
      <span class="glyph superseded" style="margin-left:8px"></span>superseded
      <span class="glyph quarantined" style="margin-left:8px"></span>quarantined
    </span>
  </div>
</header>
<main id="main"></main>
<div class="detail" id="detail">
  <button class="close" onclick="document.getElementById('detail').classList.remove('open')">x</button>
  <div id="detail-body"></div>
</div>
<script>
const DATA = __PAYLOAD__;

function fmtDate(d) { if (!d) return ""; return String(d).slice(0, 10); }

function getAxisDate(item, axis) {
  if (item.kind === "triple") {
    return axis === "valid"
      ? (item.valid_from || item.extracted_at)
      : (item.extracted_at || item.valid_from);
  } else {
    return axis === "valid"
      ? (item.date || item.filed_at)
      : (item.filed_at || item.date);
  }
}

function currencyOf(item) {
  if (item.kind === "triple") {
    if (item.invalidated_at) return "retired";
    if (item.valid_to) return "superseded";
    return "current";
  } else {
    if (item.retired_at) return item.retired_reason === "superseded" ? "superseded" : "retired";
    if (item.quarantined === 1 || item.quarantined === true) return "quarantined";
    return "current";
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
}

let axis = "valid";

function render() {
  const fWing = document.getElementById("f-wing").value;
  const fKind = document.getElementById("f-kind").value;
  const fState = document.getElementById("f-state").value;
  const fQ = document.getElementById("f-q").value.toLowerCase();

  const filtered = DATA.filter(item => {
    if (fKind && item.kind !== fKind) return false;
    if (fWing && (item.wing || "") !== fWing) return false;
    const cur = currencyOf(item);
    if (fState === "current" && cur !== "current") return false;
    if (fState === "retired" && cur === "current") return false;
    if (fQ) {
      const blob = (item.kind === "triple"
        ? (item.subject + " " + item.predicate + " " + item.object)
        : (item.title + " " + (item.doc || ""))).toLowerCase();
      if (!blob.includes(fQ)) return false;
    }
    return true;
  });

  filtered.forEach(it => it._d = getAxisDate(it, axis));
  filtered.sort((a, b) => (b._d || "").localeCompare(a._d || ""));

  const nCurrent = filtered.filter(i => currencyOf(i)==="current").length;
  document.getElementById("counts").textContent =
    filtered.length + " of " + DATA.length + " memories  ·  axis: " + axis +
    "-time  ·  " + nCurrent + " current, " + (filtered.length - nCurrent) + " non-current";

  const main = document.getElementById("main");
  main.innerHTML = "";
  if (!filtered.length) { main.innerHTML = '<div class="empty">no matches</div>'; return; }

  let lastDay = null;
  const frag = document.createDocumentFragment();
  for (const item of filtered) {
    const d = fmtDate(item._d) || "(undated)";
    if (d !== lastDay) {
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.textContent = d;
      frag.appendChild(sep);
      lastDay = d;
    }
    frag.appendChild(buildRow(item));
  }
  main.appendChild(frag);
}

function buildRow(item) {
  const cur = currencyOf(item);
  const row = document.createElement("div");
  row.className = "row kind-" + item.kind + (cur !== "current" ? " retired" : "");
  const dateCell = '<div class="date">' + fmtDate(item._d) + '</div>';
  const wingCell = '<div class="wing">' + esc(item.wing || "") + '</div>';
  let body;
  if (item.kind === "triple") {
    body = '<div class="body">' +
      '<span class="glyph ' + cur + '"></span>' +
      '<span>' + esc(item.subject) + '</span> ' +
      '<span class="pred">' + esc(item.predicate) + '</span> ' +
      '<span>' + esc(item.object) + '</span>' +
      '</div>';
  } else {
    body = '<div class="body">' +
      '<span class="glyph ' + cur + '"></span>' +
      '<span class="title">' + esc(item.title) + '</span>' +
      '<span class="pred">  ' + esc((item.hall || "") + "/" + (item.room || "")) + '</span>' +
      '</div>';
  }
  row.innerHTML = dateCell + wingCell + body;
  row.addEventListener("click", () => showDetail(item));
  return row;
}

function showDetail(item) {
  const cur = currencyOf(item);
  let html = '<h3>' + item.kind + ' <span class="glyph ' + cur + '"></span>' + cur + '</h3>';
  if (item.kind === "triple") {
    html += '<div class="meta-line"><b>subject:</b> ' + esc(item.subject) + '</div>';
    html += '<div class="meta-line"><b>predicate:</b> ' + esc(item.predicate) + '</div>';
    html += '<div class="meta-line"><b>object:</b> ' + esc(item.object) + '</div>';
    html += '<div class="meta-line"><b>valid_from:</b> ' + (fmtDate(item.valid_from) || "(undated)") + '</div>';
    html += '<div class="meta-line"><b>valid_to:</b> ' + (fmtDate(item.valid_to) || "—") + '</div>';
    html += '<div class="meta-line"><b>extracted_at:</b> ' + fmtDate(item.extracted_at) + '</div>';
    html += '<div class="meta-line"><b>last_verified:</b> ' + (fmtDate(item.last_verified) || "—") + '</div>';
    html += '<div class="meta-line"><b>invalidated_at:</b> ' + (fmtDate(item.invalidated_at) || "—") + '</div>';
    html += '<div class="meta-line"><b>confidence:</b> ' + esc(item.confidence) + '</div>';
    html += '<div class="meta-line"><b>source:</b> ' + (esc(item.source) || "—") + '</div>';
  } else {
    html += '<div class="meta-line"><b>wing/hall/room:</b> ' + esc(item.wing) + '/' + esc(item.hall) + '/' + esc(item.room) + '</div>';
    html += '<div class="meta-line"><b>date:</b> ' + fmtDate(item.date) + '</div>';
    html += '<div class="meta-line"><b>filed_at:</b> ' + fmtDate(item.filed_at) + '</div>';
    if (item.retired_at) {
      html += '<div class="meta-line"><b>retired_at:</b> ' + fmtDate(item.retired_at) + ' (' + esc(item.retired_reason || "") + ')</div>';
      if (item.superseded_by) html += '<div class="meta-line"><b>superseded_by:</b> ' + esc(item.superseded_by) + '</div>';
    }
    html += '<div class="meta-line"><b>confidence:</b> ' + esc(item.confidence) + '</div>';
    html += '<pre>' + esc(item.doc || item.title) + '</pre>';
  }
  document.getElementById("detail-body").innerHTML = html;
  document.getElementById("detail").classList.add("open");
}

(function init() {
  const wings = [...new Set(DATA.map(i => i.wing).filter(Boolean))].sort();
  const sel = document.getElementById("f-wing");
  for (const w of wings) {
    const o = document.createElement("option");
    o.value = w; o.textContent = w;
    sel.appendChild(o);
  }
  document.getElementById("ax-valid").onclick = () => {
    axis = "valid";
    document.getElementById("ax-valid").classList.add("active");
    document.getElementById("ax-believed").classList.remove("active");
    render();
  };
  document.getElementById("ax-believed").onclick = () => {
    axis = "believed";
    document.getElementById("ax-believed").classList.add("active");
    document.getElementById("ax-valid").classList.remove("active");
    render();
  };
  ["f-wing","f-kind","f-state","f-q"].forEach(id =>
    document.getElementById(id).addEventListener("input", render));
  render();
})();
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Bitemporal palace timeline viewer")
    ap.add_argument("--out", default="palace_timeline.html", help="Output HTML path")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open in browser")
    args = ap.parse_args()

    triples = load_kg_triples()
    prose = load_prose()
    payload = triples + prose
    print(f"Loaded {len(triples)} triples + {len(prose)} prose memories = {len(payload)} items")

    # </script> in any memory text would break the HTML if injected raw.
    payload_json = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = HTML.replace("__PAYLOAD__", payload_json)

    out_path = Path(args.out).resolve()
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}")

    if not args.no_open:
        webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
