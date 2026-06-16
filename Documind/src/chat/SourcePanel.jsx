import { useState, useEffect } from "react";

const API = "http://localhost:8000/api/v1";

// ── Score bar ─────────────────────────────────────────────────────────────────

function ScoreBar({ score }) {
  const pct   = Math.round(score * 100);
  const color = score >= 0.7 ? "#4ade80" : score >= 0.45 ? "#fbbf24" : "#f87171";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
      <div style={{
        flex:         1,
        height:       "3px",
        borderRadius: "2px",
        background:   "#1e1e1e",
        overflow:     "hidden",
      }}>
        <div style={{
          width:      `${pct}%`,
          height:     "100%",
          background: color,
          borderRadius: "2px",
          transition: "width 0.4s ease-out",
        }} />
      </div>
      <span style={{
        fontSize:   "10px",
        fontFamily: "'DM Mono', monospace",
        color,
        flexShrink: 0,
        minWidth:   "28px",
      }}>
        {pct}%
      </span>
    </div>
  );
}

// ── Citation detail card ───────────────────────────────────────────────────────

function CitationCard({ citation }) {
  const [expanded, setExpanded] = useState(false);
  const maxLen = 180;
  const long   = citation.excerpt.length > maxLen;

  return (
    <div style={{
      background:   "#0f0f0f",
      border:       "1px solid #1e1e1e",
      borderRadius: "8px",
      padding:      "14px",
      animation:    "fadeIn 0.2s ease-out",
    }}>
      {/* Source + page */}
      <div style={{
        display:        "flex",
        justifyContent: "space-between",
        alignItems:     "center",
        marginBottom:   "8px",
      }}>
        <span style={{
          fontSize:   "11px",
          fontFamily: "'DM Mono', monospace",
          color:      "#d4a853",
          fontWeight: "500",
        }}>
          {citation.source}
        </span>
        <span style={{
          fontSize:     "10px",
          fontFamily:   "'DM Mono', monospace",
          color:        "#444",
          background:   "#141414",
          border:       "1px solid #222",
          borderRadius: "3px",
          padding:      "1px 6px",
        }}>
          p. {citation.page}
        </span>
      </div>

      {/* Excerpt */}
      <p style={{
        margin:     "0 0 10px",
        fontSize:   "13px",
        fontFamily: "'Lora', Georgia, serif",
        color:      "#b0a090",
        lineHeight: "1.65",
        fontStyle:  "italic",
      }}>
        "
        {expanded || !long
          ? citation.excerpt
          : citation.excerpt.slice(0, maxLen) + "…"}
        "
        {long && (
          <button
            onClick={() => setExpanded(e => !e)}
            style={{
              background:  "transparent",
              border:      "none",
              color:       "#d4a85390",
              fontSize:    "11px",
              fontFamily:  "'DM Mono', monospace",
              cursor:      "pointer",
              marginLeft:  "4px",
              padding:     "0",
            }}
          >
            {expanded ? "less" : "more"}
          </button>
        )}
      </p>

      {/* Score + chunk type */}
      <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
        <ScoreBar score={citation.score} />
        <div style={{ display: "flex", gap: "6px", marginTop: "4px" }}>
          <span style={{
            fontSize:     "10px",
            fontFamily:   "'DM Mono', monospace",
            color:        "#444",
            background:   "#141414",
            border:       "1px solid #1e1e1e",
            borderRadius: "3px",
            padding:      "1px 6px",
          }}>
            {citation.chunk_type}
          </span>
          <span style={{
            fontSize:     "10px",
            fontFamily:   "'DM Mono', monospace",
            color:        "#444",
            background:   "#141414",
            border:       "1px solid #1e1e1e",
            borderRadius: "3px",
            padding:      "1px 6px",
          }}>
            chunk #{citation.chunk_index}
          </span>
        </div>
      </div>
    </div>
  );
}

// ── Document row in the index list ────────────────────────────────────────────

function DocumentRow({ doc, selected, onToggle, onDelete }) {
  const [deleting, setDeleting] = useState(false);
  const [confirm,  setConfirm]  = useState(false);

  async function handleDelete(e) {
    e.stopPropagation();
    if (!confirm) { setConfirm(true); return; }
    setDeleting(true);
    try {
      await fetch(`${API}/documents/${encodeURIComponent(doc.source)}`, { method: "DELETE" });
      onDelete(doc.source);
    } catch {
      setDeleting(false);
      setConfirm(false);
    }
  }

  return (
    <div
      onClick={() => onToggle(doc.source)}
      style={{
        display:    "flex",
        alignItems: "center",
        gap:        "10px",
        padding:    "10px 12px",
        borderRadius: "6px",
        cursor:     "pointer",
        border:     selected ? "1px solid #d4a85340" : "1px solid transparent",
        background: selected ? "#d4a85308" : "transparent",
        transition: "all 0.15s",
      }}
    >
      {/* Selection dot */}
      <div style={{
        width:        "7px",
        height:       "7px",
        borderRadius: "50%",
        background:   selected ? "#d4a853" : "#2a2a2a",
        border:       selected ? "none" : "1px solid #333",
        flexShrink:   0,
        transition:   "all 0.15s",
      }} />

      {/* Name + stats */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize:     "13px",
          fontFamily:   "'Lora', Georgia, serif",
          color:        "#e2d5c3",
          overflow:     "hidden",
          textOverflow: "ellipsis",
          whiteSpace:   "nowrap",
        }}>
          {doc.source}
        </div>
        <div style={{
          fontSize:   "10px",
          fontFamily: "'DM Mono', monospace",
          color:      "#444",
          marginTop:  "2px",
        }}>
          {doc.total_pages} pages · {doc.chunk_count} chunks
        </div>
      </div>

      {/* Delete */}
      <button
        onClick={handleDelete}
        disabled={deleting}
        style={{
          background: "transparent",
          border:     "none",
          color:      confirm ? "#f87171" : "#333",
          fontSize:   "12px",
          cursor:     "pointer",
          padding:    "2px 4px",
          flexShrink: 0,
          fontFamily: "'DM Mono', monospace",
          transition: "color 0.15s",
        }}
        title={confirm ? "Click again to confirm" : "Delete document"}
      >
        {deleting ? "…" : confirm ? "sure?" : "✕"}
      </button>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function SourcePanel({
  activeCitation,          // CitationItem | null  — citation to highlight/expand
  selectedSources,         // string[]             — currently filtered sources
  onSourceToggle,          // (source: string) => void
  refreshTrigger,          // any  — change this value to force a doc list reload
}) {
  const [documents,  setDocuments]  = useState([]);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const [activeTab,  setActiveTab]  = useState("sources"); // "sources" | "citation"

  // Load document list
  useEffect(() => {
    async function load() {
      setLoadingDocs(true);
      try {
        const res  = await fetch(`${API}/documents`);
        const data = await res.json();
        setDocuments(data.documents || []);
      } catch {
        setDocuments([]);
      } finally {
        setLoadingDocs(false);
      }
    }
    load();
  }, [refreshTrigger]);

  // Auto-switch to citation tab when a citation is selected
  useEffect(() => {
    if (activeCitation) setActiveTab("citation");
  }, [activeCitation]);

  function handleDeleteDoc(source) {
    setDocuments(prev => prev.filter(d => d.source !== source));
    // If deleted doc was in selectedSources, remove it
    if (selectedSources?.includes(source)) onSourceToggle(source);
  }

  // ── render ─────────────────────────────────────────────────────────────────

  const tabStyle = active => ({
    flex:          1,
    padding:       "8px",
    background:    "transparent",
    border:        "none",
    borderBottom:  active ? "2px solid #d4a853" : "2px solid transparent",
    color:         active ? "#d4a853" : "#444",
    fontSize:      "11px",
    fontFamily:    "'DM Mono', monospace",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    cursor:        "pointer",
    transition:    "all 0.15s",
  });

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora&family=Playfair+Display:wght@600&display=swap');
        @keyframes fadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }
        .doc-row:hover { background: #0f0f0f !important; }
      `}</style>

      <div style={{
        display:       "flex",
        flexDirection: "column",
        height:        "100%",
        background:    "#080808",
        borderLeft:    "1px solid #1a1a1a",
      }}>

        {/* Tabs */}
        <div style={{
          display:      "flex",
          borderBottom: "1px solid #1a1a1a",
          padding:      "0 8px",
        }}>
          <button style={tabStyle(activeTab === "sources")}  onClick={() => setActiveTab("sources")}>
            Index
          </button>
          <button style={tabStyle(activeTab === "citation")} onClick={() => setActiveTab("citation")}>
            Source
          </button>
        </div>

        {/* Tab content */}
        <div style={{
          flex:      1,
          overflowY: "auto",
          padding:   "16px 12px",
          scrollbarWidth: "thin",
          scrollbarColor: "#1e1e1e transparent",
        }}>

          {/* ── Sources tab ── */}
          {activeTab === "sources" && (
            <div>
              <p style={{
                margin:     "0 0 12px",
                fontSize:   "11px",
                fontFamily: "'DM Mono', monospace",
                color:      "#333",
              }}>
                {selectedSources?.length > 0
                  ? `${selectedSources.length} of ${documents.length} selected`
                  : "All documents · click to filter"}
              </p>

              {loadingDocs ? (
                <p style={{ color: "#333", fontSize: "12px", fontFamily: "'DM Mono', monospace" }}>
                  Loading…
                </p>
              ) : documents.length === 0 ? (
                <p style={{ color: "#333", fontSize: "12px", fontFamily: "'DM Mono', monospace" }}>
                  No documents indexed yet.
                </p>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
                  {documents.map(doc => (
                    <DocumentRow
                      key={doc.source}
                      doc={doc}
                      selected={selectedSources?.includes(doc.source)}
                      onToggle={onSourceToggle}
                      onDelete={handleDeleteDoc}
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
                <div style={{ textAlign: "center", padding: "32px 16px" }}>
                  <div style={{ fontSize: "28px", opacity: 0.2, marginBottom: "12px" }}>↗</div>
                  <p style={{
                    color:      "#333",
                    fontSize:   "12px",
                    fontFamily: "'DM Mono', monospace",
                    lineHeight: "1.6",
                    margin:     0,
                  }}>
                    Click a citation chip on any answer to inspect the source excerpt
                  </p>
                </div>
              ) : (
                <div>
                  <p style={{
                    margin:     "0 0 10px",
                    fontSize:   "11px",
                    fontFamily: "'DM Mono', monospace",
                    color:      "#444",
                    letterSpacing: "0.06em",
                  }}>
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