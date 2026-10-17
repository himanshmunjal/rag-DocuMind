import { useState, useEffect } from "react";

// ── Score bar ─────────────────────────────────────────────────────────────────

function ScoreBar({ score }) {
  const pct = Math.round((score ?? 0) * 100);
  const color = score >= 0.7 ? "text-[#4ade80]" : score >= 0.45 ? "text-[#fbbf24]" : "text-[#f87171]";
  const barColor = score >= 0.7 ? "bg-[#4ade80]" : score >= 0.45 ? "bg-[#fbbf24]" : "bg-[#f87171]";

  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-[3px] rounded-sm bg-[#1e1e1e] overflow-hidden">
        <div className={`h-full rounded-sm transition-[width] duration-400 ease-out ${barColor}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[10px] font-['DM_Mono',monospace] flex-shrink-0 min-w-[28px] ${color}`}>{pct}%</span>
    </div>
  );
}

// ── Citation detail card ───────────────────────────────────────────────────────
//
// NOTE: filings don't have real page numbers, and the Citation model
// (per citation_builder_patch.py) carries item/item_title/cik/company_name/
// accession_number instead of the old generic page/chunk_type/chunk_index
// shape this card was originally built for. Reading with fallbacks below —
// if your actual Citation fields differ, tell me the exact field names and
// I'll tighten this up (no more guessing needed once confirmed).

function CitationCard({ citation }) {
  const [expanded, setExpanded] = useState(false);
  const excerptText = citation.excerpt ?? citation.text ?? "";
  const maxLen = 180;
  const long = excerptText.length > maxLen;

  const label = citation.company_name ?? citation.source ?? citation.accession_number ?? "source";
  const itemLabel = citation.item_title ?? (citation.item ? `Item ${citation.item}` : null);

  return (
    <div className="bg-[#0f0f0f] border border-[#1e1e1e] rounded-lg p-3.5 animate-[fadeIn_0.2s_ease-out]">
      <div className="flex justify-between items-center mb-2 gap-2">
        <span className="text-[11px] font-['DM_Mono',monospace] text-[#d4a853] font-medium truncate">{label}</span>
        {itemLabel && (
          <span className="text-[10px] font-['DM_Mono',monospace] text-[#444] bg-[#141414] border border-[#222] rounded px-1.5 py-px flex-shrink-0">
            {itemLabel}
          </span>
        )}
      </div>

      {excerptText && (
        <p className="mb-2.5 text-[13px] font-['Lora',Georgia,serif] text-[#b0a090] leading-[1.65] italic">
          "{expanded || !long ? excerptText : excerptText.slice(0, maxLen) + "…"}"
          {long && (
            <button
              onClick={() => setExpanded((e) => !e)}
              className="bg-transparent border-none text-[#d4a85390] text-[11px] font-['DM_Mono',monospace] cursor-pointer ml-1 p-0"
            >
              {expanded ? "less" : "more"}
            </button>
          )}
        </p>
      )}

      <div className="flex flex-col gap-1">
        {typeof citation.score === "number" && <ScoreBar score={citation.score} />}
        <div className="flex gap-1.5 mt-1 flex-wrap">
          {citation.form_type && (
            <span className="text-[10px] font-['DM_Mono',monospace] text-[#444] bg-[#141414] border border-[#1e1e1e] rounded px-1.5 py-px">
              {citation.form_type}
            </span>
          )}
          {citation.accession_number && (
            <span className="text-[10px] font-['DM_Mono',monospace] text-[#444] bg-[#141414] border border-[#1e1e1e] rounded px-1.5 py-px">
              {citation.accession_number}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Document row in the index list ────────────────────────────────────────────

function DocumentRow({ doc, selected, onToggle, onDelete }) {
  const [deleting, setDeleting] = useState(false);
  const [confirm, setConfirm] = useState(false);

  async function handleDelete(e) {
    e.stopPropagation();
    if (!confirm) {
      setConfirm(true);
      return;
    }
    setDeleting(true);
    await onDelete(doc.source);
  }

  return (
    <div
      onClick={() => onToggle(doc.source)}
      className={`flex items-center gap-2.5 px-3 py-2.5 rounded-md cursor-pointer border transition-colors hover:bg-[#0f0f0f] ${
        selected ? "border-[#d4a85340] bg-[#d4a85308]" : "border-transparent"
      }`}
    >
      <div className={`w-[7px] h-[7px] rounded-full flex-shrink-0 ${selected ? "bg-[#d4a853]" : "bg-[#2a2a2a] border border-[#333]"}`} />

      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-['Lora',Georgia,serif] text-[#e2d5c3] overflow-hidden text-ellipsis whitespace-nowrap">
          {doc.company_name || doc.source}
        </div>
        <div className="text-[10px] font-['DM_Mono',monospace] text-[#444] mt-0.5">
          {doc.form_type ? `${doc.form_type} · ` : ""}
          {doc.chunk_count} chunks
        </div>
      </div>

      <button
        onClick={handleDelete}
        disabled={deleting}
        title={confirm ? "Click again to confirm" : "Delete document"}
        className={`bg-transparent border-none text-xs cursor-pointer px-1 py-0.5 flex-shrink-0 font-['DM_Mono',monospace] transition-colors ${
          confirm ? "text-[#f87171]" : "text-[#333]"
        }`}
      >
        {deleting ? "…" : confirm ? "sure?" : "✕"}
      </button>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

/**
 * SourcePanel
 *
 * Props:
 *   - documents: [{ source, cik, company_name, form_type, chunk_count }, ...]
 *     Now passed down from App.jsx (single fetch, shared with ChatWindow for
 *     cik derivation) instead of this component owning its own fetch.
 *   - documentsLoading: bool
 *   - onDeleteDocument(source): async fn — App.jsx performs the DELETE call
 *     and updates shared state; this component no longer calls fetch directly.
 *   - activeCitation, selectedSources, onSourceToggle — as before.
 */
export default function SourcePanel({
  documents = [],
  documentsLoading = false,
  onDeleteDocument,
  activeCitation,
  selectedSources,
  onSourceToggle,
}) {
  const [activeTab, setActiveTab] = useState("sources"); // "sources" | "citation"

  useEffect(() => {
    if (activeCitation) setActiveTab("citation");
  }, [activeCitation]);

  const tabClass = (active) =>
    `flex-1 py-2 bg-transparent border-none text-[11px] font-['DM_Mono',monospace] tracking-[0.08em] uppercase cursor-pointer transition-colors border-b-2 ${
      active ? "border-[#d4a853] text-[#d4a853]" : "border-transparent text-[#444]"
    }`;

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora&family=Playfair+Display:wght@600&display=swap');
        @keyframes fadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }
      `}</style>

      <div className="flex flex-col h-full bg-[#080808] border-l border-[#1a1a1a]">
        {/* Tabs */}
        <div className="flex border-b border-[#1a1a1a] px-2">
          <button className={tabClass(activeTab === "sources")} onClick={() => setActiveTab("sources")}>
            Index
          </button>
          <button className={tabClass(activeTab === "citation")} onClick={() => setActiveTab("citation")}>
            Source
          </button>
        </div>

        {/* Tab content */}
        <div className="flex-1 overflow-y-auto p-4 px-3">
          {/* ── Sources tab ── */}
          {activeTab === "sources" && (
            <div>
              <p className="mb-3 text-[11px] font-['DM_Mono',monospace] text-[#333]">
                {selectedSources?.length > 0
                  ? `${selectedSources.length} of ${documents.length} selected`
                  : "All documents · click to filter"}
              </p>

              {documentsLoading ? (
                <p className="text-[#333] text-xs font-['DM_Mono',monospace]">Loading…</p>
              ) : documents.length === 0 ? (
                <p className="text-[#333] text-xs font-['DM_Mono',monospace]">No documents indexed yet.</p>
              ) : (
                <div className="flex flex-col gap-0.5">
                  {documents.map((doc) => (
                    <DocumentRow
                      key={doc.source}
                      doc={doc}
                      selected={selectedSources?.includes(doc.source)}
                      onToggle={onSourceToggle}
                      onDelete={onDeleteDocument}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ── Citation tab ── */}
          {activeTab === "citation" && (
            <div>
              {!activeCitation ? (
                <div className="text-center py-8 px-4">
                  <div className="text-[28px] opacity-20 mb-3">↗</div>
                  <p className="text-[#333] text-xs font-['DM_Mono',monospace] leading-[1.6] m-0">
                    Click a citation chip on any answer to inspect the source excerpt
                  </p>
                </div>
              ) : (
                <div>
                  <p className="mb-2.5 text-[11px] font-['DM_Mono',monospace] text-[#444] tracking-[0.06em]">
                    ACTIVE SOURCE
                  </p>
                  <CitationCard citation={activeCitation} />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}