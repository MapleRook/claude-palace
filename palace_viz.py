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
    """Return prose memories keyed by drawer_id (string), not chroma's
    internal integer id. The drawer_id is what KG entities reference, so
    using it as `id` lets the spiderweb link directly to triples.
    """
    if not CHROMA_DB.exists():
        return []
    conn = sqlite3.connect(str(CHROMA_DB))
    # Map chroma's int id -> drawer_id (string).
    drawer_by_int = {r[0]: r[1] for r in conn.execute("SELECT id, embedding_id FROM embeddings")}
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
        drawer_id = drawer_by_int.get(mid) or f"chid:{mid}"
        doc = meta.get("chroma:document") or ""
        title = doc.split("\n", 1)[0][:140].strip() or f"(empty memory {drawer_id})"
        out.append({
            "kind": "prose",
            "id": drawer_id,
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


def load_audit_candidates(path):
    """Read candidates.jsonl (output of palace_crosswing_audit.py) into a
    map keyed by memory_id: {signal, confidence, evidence, suggested_action}.
    Multiple signals per memory get combined. Returns {} if file missing.
    """
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            mid = row.get("memory_id")
            if not mid:
                continue
            existing = out.setdefault(mid, {
                "signals": [],
                "max_confidence": 0.0,
                "evidence": [],
                "suggested_action": row.get("suggested_action"),
            })
            existing["signals"].append(row.get("signal"))
            existing["max_confidence"] = max(existing["max_confidence"],
                                            row.get("confidence", 0.0))
            existing["evidence"].append({
                "signal": row.get("signal"),
                "detail": row.get("evidence"),
            })
    return out


def load_linked_to():
    """Return {drawer_id: [linked drawer_ids]} from KG linked_to triples.

    The bidirectional binding writes pairs (A->B and B->A) so this is
    symmetric in practice. We dedup per-source.
    """
    if not KG_DB.exists():
        return {}
    conn = sqlite3.connect(str(KG_DB))
    rows = conn.execute(
        "SELECT subject, object FROM triples "
        "WHERE predicate='linked_to' AND invalidated_at IS NULL "
        "  AND (valid_to IS NULL OR valid_to = '')"
    ).fetchall()
    conn.close()
    # KG entity ids are the lowercased drawer_id (per palace.KnowledgeGraph._eid).
    # Drawer ids are already lowercase ASCII, so no transformation needed.
    out = {}
    for subj, obj in rows:
        if subj == obj:
            continue
        bucket = out.setdefault(subj, [])
        if obj not in bucket:
            bucket.append(obj)
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
  .audit-badge { display: inline-block; padding: 0 4px; margin-left: 6px;
                 background: #4a3a1a; color: #ffcb6b; border-radius: 2px;
                 font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px; }
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
  .detail .back { background: none; color: #88aaff; border: none;
                  cursor: pointer; font-size: 11px; padding: 0 4px 8px; }
  .detail .back:hover { color: #aabbff; }
  .detail .back:disabled { color: #444; cursor: default; }
  .detail .crumbs { color: #555; font-size: 10px; padding-bottom: 8px;
                    border-bottom: 1px solid #333; margin-bottom: 8px;
                    word-break: break-all; }
  .detail .links-section { margin-top: 12px; padding-top: 8px;
                           border-top: 1px solid #333; }
  .detail .links-section h4 { margin: 0 0 6px; font-size: 11px;
                              color: #888; font-weight: normal;
                              text-transform: uppercase; letter-spacing: 0.5px; }
  .detail .link-item { display: block; padding: 4px 0; font-size: 11px;
                       color: #88aaff; cursor: pointer;
                       border-bottom: 1px dotted #333; }
  .detail .link-item:hover { color: #aaccff; }
  .detail .link-item .lw { color: #777; font-size: 10px; }
  .detail .link-item .lt { color: #ccc; }
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
    <label>audit
      <select id="f-audit">
        <option value="">(any)</option>
        <option value="any">flagged only</option>
        <option value="wing-named-in-other-wing">wing-named-in-other-wing</option>
        <option value="wing-orphaned-infra">wing-orphaned-infra</option>
        <option value="friction-flag">friction-flag</option>
        <option value="cross-source-behavioral-pattern">cross-source-behavioral-pattern</option>
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
  <button class="back" id="detail-back" disabled>&larr; back</button>
  <div class="crumbs" id="detail-crumbs"></div>
  <div id="detail-body"></div>
</div>
<script>
const DATA = __PAYLOAD__;
const LINKS = __LINKS__;
// id -> item lookup for O(1) navigation
const BY_ID = {};
for (const item of DATA) {
  if (item.id) BY_ID[item.id] = item;
}
// Breadcrumb stack for back-button navigation
let detailHistory = [];

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
    const fAudit = document.getElementById("f-audit").value;
    if (fAudit) {
      const sigs = item.audit_signals || [];
      if (fAudit === "any" && !sigs.length) return false;
      if (fAudit !== "any" && !sigs.includes(fAudit)) return false;
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
    let audit = "";
    if (item.audit_signals && item.audit_signals.length) {
      audit = '<span class="audit-badge" title="' + esc((item.audit_action || "")) + '">' +
              esc(item.audit_signals[0]) +
              (item.audit_signals.length > 1 ? " +" + (item.audit_signals.length - 1) : "") +
              '</span>';
    }
    body = '<div class="body">' +
      '<span class="glyph ' + cur + '"></span>' +
      '<span class="title">' + esc(item.title) + '</span>' +
      '<span class="pred">  ' + esc((item.hall || "") + "/" + (item.room || "")) + '</span>' +
      audit +
      '</div>';
  }
  row.innerHTML = dateCell + wingCell + body;
  row.addEventListener("click", () => showDetail(item));
  return row;
}

function renderDetailBody(item) {
  const cur = currencyOf(item);
  let html = '<h3>' + item.kind + ' <span class="glyph ' + cur + '"></span>' + cur + '</h3>';
  if (item.kind === "triple") {
    html += '<div class="meta-line"><b>subject:</b> ' + esc(item.subject) + '</div>';
    html += '<div class="meta-line"><b>predicate:</b> ' + esc(item.predicate) + '</div>';
    html += '<div class="meta-line"><b>object:</b> ' + esc(item.object) + '</div>';
    html += '<div class="meta-line"><b>valid_from:</b> ' + (fmtDate(item.valid_from) || "(undated)") + '</div>';
    html += '<div class="meta-line"><b>valid_to:</b> ' + (fmtDate(item.valid_to) || "&mdash;") + '</div>';
    html += '<div class="meta-line"><b>extracted_at:</b> ' + fmtDate(item.extracted_at) + '</div>';
    html += '<div class="meta-line"><b>last_verified:</b> ' + (fmtDate(item.last_verified) || "&mdash;") + '</div>';
    html += '<div class="meta-line"><b>invalidated_at:</b> ' + (fmtDate(item.invalidated_at) || "&mdash;") + '</div>';
    html += '<div class="meta-line"><b>confidence:</b> ' + esc(item.confidence) + '</div>';
    html += '<div class="meta-line"><b>source:</b> ' + (esc(item.source) || "&mdash;") + '</div>';
  } else {
    html += '<div class="meta-line"><b>wing/hall/room:</b> ' + esc(item.wing) + '/' + esc(item.hall) + '/' + esc(item.room) + '</div>';
    html += '<div class="meta-line"><b>id:</b> ' + esc(item.id) + '</div>';
    html += '<div class="meta-line"><b>date:</b> ' + fmtDate(item.date) + '</div>';
    html += '<div class="meta-line"><b>filed_at:</b> ' + fmtDate(item.filed_at) + '</div>';
    if (item.retired_at) {
      html += '<div class="meta-line"><b>retired_at:</b> ' + fmtDate(item.retired_at) + ' (' + esc(item.retired_reason || "") + ')</div>';
      if (item.superseded_by) {
        const sb = BY_ID[item.superseded_by];
        if (sb) {
          html += '<div class="meta-line"><b>superseded_by:</b> <span class="link-item" data-link-id="' + esc(item.superseded_by) + '">' + esc(sb.title || sb.id) + '</span></div>';
        } else {
          html += '<div class="meta-line"><b>superseded_by:</b> ' + esc(item.superseded_by) + '</div>';
        }
      }
    }
    html += '<div class="meta-line"><b>confidence:</b> ' + esc(item.confidence) + '</div>';
    html += '<pre>' + esc(item.doc || item.title) + '</pre>';

    // Spiderweb section: clickable neighbors via KG linked_to.
    const linkedIds = (LINKS[item.id] || []).slice();
    if (linkedIds.length) {
      html += '<div class="links-section">';
      html += '<h4>Linked memories (' + linkedIds.length + ')</h4>';
      for (const lid of linkedIds) {
        const target = BY_ID[lid];
        if (target) {
          const tw = (target.wing || "?") + "/" + (target.room || "?");
          const tt = (target.title || target.id || "(no title)").slice(0, 80);
          html += '<span class="link-item" data-link-id="' + esc(lid) + '">' +
                  '<span class="lw">' + esc(tw) + '</span> &middot; ' +
                  '<span class="lt">' + esc(tt) + '</span></span>';
        } else {
          html += '<span class="link-item" style="color:#666" title="not in current view">' +
                  esc(lid) + ' (not loaded)</span>';
        }
      }
      html += '</div>';
    }
  }
  return html;
}

function showDetail(item, pushHistory = true) {
  if (!item) return;
  // History: pushing only when navigating forward (not from back button).
  if (pushHistory) {
    const current = detailHistory[detailHistory.length - 1];
    if (!current || current.id !== item.id) {
      detailHistory.push(item);
    }
  }
  document.getElementById("detail-body").innerHTML = renderDetailBody(item);
  renderCrumbs();
  document.getElementById("detail").classList.add("open");

  // Wire all .link-item[data-link-id] children to navigation.
  for (const el of document.querySelectorAll("#detail-body .link-item[data-link-id]")) {
    el.addEventListener("click", () => {
      const target = BY_ID[el.getAttribute("data-link-id")];
      if (target) showDetail(target);
    });
  }
}

function renderCrumbs() {
  const back = document.getElementById("detail-back");
  back.disabled = detailHistory.length <= 1;
  const crumbs = document.getElementById("detail-crumbs");
  if (detailHistory.length <= 1) { crumbs.textContent = ""; return; }
  const labels = detailHistory.map(it => {
    if (it.kind === "triple") return (it.predicate || "?") + ":" + (it.object || "?").slice(0, 20);
    return (it.room || it.title || it.id || "?").slice(0, 30);
  });
  crumbs.textContent = labels.join("  >  ");
}

document.getElementById("detail-back").addEventListener("click", () => {
  if (detailHistory.length > 1) {
    detailHistory.pop();
    const prev = detailHistory[detailHistory.length - 1];
    showDetail(prev, false);
  }
});

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
  ["f-wing","f-kind","f-state","f-q","f-audit"].forEach(id =>
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
    ap.add_argument("--audit-jsonl", default="candidates.jsonl",
                    help="palace_crosswing_audit.py output to overlay (default: candidates.jsonl if present)")
    args = ap.parse_args()

    triples = load_kg_triples()
    prose = load_prose()
    links = load_linked_to()
    audit = load_audit_candidates(args.audit_jsonl)
    # Attach audit info to matching prose memories.
    for p in prose:
        a = audit.get(p["id"])
        if a:
            p["audit_signals"] = a["signals"]
            p["audit_confidence"] = round(a["max_confidence"], 2)
            p["audit_action"] = a["suggested_action"]
    payload = triples + prose
    n_linked = sum(1 for v in links.values() if v)
    n_audit = sum(1 for p in prose if p.get("audit_signals"))
    print(f"Loaded {len(triples)} triples + {len(prose)} prose memories = {len(payload)} items")
    print(f"Loaded {len(links)} link buckets ({n_linked} memories with outgoing links)")
    if audit:
        print(f"Loaded {len(audit)} audit candidates ({n_audit} prose memories flagged)")

    # </script> in any memory text would break the HTML if injected raw.
    payload_json = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    links_json = json.dumps(links, ensure_ascii=False).replace("</", "<\\/")
    html = HTML.replace("__PAYLOAD__", payload_json).replace("__LINKS__", links_json)

    out_path = Path(args.out).resolve()
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}")

    if not args.no_open:
        webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
