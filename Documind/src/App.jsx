import { useState, useCallback } from "react";
import ChatWindow      from "./chat/chatwindow";
import DocumentUploader from "./documents/DocumentUploader";
import SourcePanel     from "./chat/sourcepanel";

// ── layout constants ──────────────────────────────────────────────────────────

const LEFT_W  = "260px";
const RIGHT_W = "300px";

// ── branding strip (top of left sidebar) ─────────────────────────────────────

function Brand() {
  return (
    <div style={{
      padding:      "22px 20px 18px",
      borderBottom: "1px solid #1a1a1a",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "9px" }}>
        {/* Logo mark */}
        <div style={{
          width:        "28px",
          height:       "28px",
          borderRadius: "6px",
          background:   "linear-gradient(135deg, #d4a853 0%, #a07830 100%)",
          display:      "flex",
          alignItems:   "center",
          justifyContent: "center",
          flexShrink:   0,
          fontSize:     "13px",
        }}>
          ◆
        </div>
        <div>
          <div style={{
            fontFamily:    "'Playfair Display', Georgia, serif",
            fontSize:      "15px",
            fontWeight:    "600",
            color:         "#e2d5c3",
            letterSpacing: "0.01em",
            lineHeight:    "1",
          }}>
            DocuMind
          </div>
          <div style={{
            fontFamily:  "'DM Mono', monospace",
            fontSize:    "10px",
            color:       "#444",
            marginTop:   "3px",
            letterSpacing: "0.06em",
          }}>
            Document intelligence
          </div>
        </div>
      </div>
    </div>
  );
}

// ── collapse button (mobile toggle) ──────────────────────────────────────────

function CollapseBtn({ label, onClick, side }) {
  return (
    <button
      onClick={onClick}
      title={label}
      style={{
        position:      "fixed",
        bottom:        "24px",
        [side]:        "12px",
        zIndex:        200,
        background:    "#111",
        border:        "1px solid #2a2a2a",
        borderRadius:  "8px",
        color:         "#d4a853",
        fontFamily:    "'DM Mono', monospace",
        fontSize:      "11px",
        padding:       "7px 12px",
        cursor:        "pointer",
        display:       "none",   // shown via media query class
      }}
      className="mobile-toggle"
    >
      {label}
    </button>
  );
}

// ── empty-state hint shown in chat before any documents are indexed ───────────

function EmptyHint() {
  return (
    <div style={{
      position:       "absolute",
      inset:          0,
      display:        "flex",
      flexDirection:  "column",
      alignItems:     "center",
      justifyContent: "center",
      pointerEvents:  "none",
      padding:        "40px",
      gap:            "12px",
      opacity:        0.35,
    }}>
      <div style={{ fontSize: "36px" }}>📄</div>
      <p style={{
        fontFamily:  "'DM Mono', monospace",
        fontSize:    "12px",
        color:       "#888",
        textAlign:   "center",
        lineHeight:  "1.7",
        margin:      0,
        maxWidth:    "260px",
      }}>
        Upload a PDF on the left,<br />then ask a question below.
      </p>
    </div>
  );
}

// ── root ──────────────────────────────────────────────────────────────────────

export default function App() {
  const [activeCitation,  setActiveCitation]  = useState(null);
  const [selectedSources, setSelectedSources] = useState([]);
  const [refreshTrigger,  setRefreshTrigger]  = useState(0);
  const [hasMessages,     setHasMessages]     = useState(false);

  // Left sidebar visible on mobile
  const [leftOpen,  setLeftOpen]  = useState(false);
  // Right panel visible on mobile
  const [rightOpen, setRightOpen] = useState(false);

  // ── handlers ──────────────────────────────────────────────────────────────

  const handleUploadComplete = useCallback(() => {
    setRefreshTrigger(n => n + 1);
  }, []);

  const handleSourceToggle = useCallback((source) => {
    setSelectedSources(prev =>
      prev.includes(source)
        ? prev.filter(s => s !== source)
        : [...prev, source]
    );
  }, []);

  const handleCitationSelect = useCallback((citation) => {
    setActiveCitation(prev => prev === citation ? null : citation);
    // Open right panel on mobile when a citation is clicked
    setRightOpen(true);
  }, []);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora:ital,wght@0,400;0,500;1,400&family=Playfair+Display:wght@600&display=swap');

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        html, body, #root {
          height:     100%;
          background: #0a0a0a;
          color:      #e2d5c3;
        }

        /* Scrollbars */
        ::-webkit-scrollbar       { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #1e1e1e; border-radius: 2px; }

        /* Mobile breakpoint */
        @media (max-width: 768px) {
          .left-sidebar  { transform: translateX(-100%); transition: transform 0.25s ease; }
          .right-sidebar { transform: translateX(100%);  transition: transform 0.25s ease; }
          .left-sidebar.open  { transform: translateX(0); }
          .right-sidebar.open { transform: translateX(0); }
          .mobile-toggle { display: block !important; }
        }

        /* Thin dividers */
        .divider-v {
          width:      1px;
          background: #1a1a1a;
          flex-shrink: 0;
        }
      `}</style>

      {/* ── Three-column shell ── */}
      <div style={{
        display:  "flex",
        height:   "100vh",
        overflow: "hidden",
      }}>

        {/* ── LEFT SIDEBAR ── */}
        <div
          className={`left-sidebar${leftOpen ? " open" : ""}`}
          style={{
            width:          LEFT_W,
            minWidth:       LEFT_W,
            display:        "flex",
            flexDirection:  "column",
            background:     "#080808",
            borderRight:    "1px solid #1a1a1a",
            overflowY:      "auto",
            position:       "relative",
            zIndex:         50,
          }}
        >
          <Brand />

          {/* Uploader */}
          <div style={{ padding: "20px 16px", flex: 1 }}>
            <DocumentUploader onUploadComplete={handleUploadComplete} />
          </div>

          {/* Footer */}
          <div style={{
            padding:    "14px 16px",
            borderTop:  "1px solid #141414",
          }}>
            <p style={{
              fontSize:    "10px",
              fontFamily:  "'DM Mono', monospace",
              color:       "#2e2e2e",
              lineHeight:  "1.6",
            }}>
              Answers grounded in your documents only.
              No external knowledge used.
            </p>
          </div>
        </div>

        <div className="divider-v" />

        {/* ── CENTRE: CHAT ── */}
        <div style={{
          flex:     1,
          display:  "flex",
          flexDirection: "column",
          position: "relative",
          minWidth: 0,
        }}>
          {/* Empty-state overlay — hidden once messages appear */}
          {!hasMessages && <EmptyHint />}

          <ChatWindow
            onCitationSelect={handleCitationSelect}
            activeCitation={activeCitation}
            selectedSources={selectedSources.length > 0 ? selectedSources : null}
            onFirstMessage={() => setHasMessages(true)}
          />
        </div>

        <div className="divider-v" />

        {/* ── RIGHT PANEL ── */}
        <div
          className={`right-sidebar${rightOpen ? " open" : ""}`}
          style={{
            width:    RIGHT_W,
            minWidth: RIGHT_W,
            position: "relative",
            zIndex:   50,
          }}
        >
          <SourcePanel
            activeCitation={activeCitation}
            selectedSources={selectedSources}
            onSourceToggle={handleSourceToggle}
            refreshTrigger={refreshTrigger}
          />
        </div>
      </div>

      {/* ── Mobile toggles ── */}
      <CollapseBtn
        label={leftOpen  ? "✕ Docs"  : "⬆ Docs"}
        onClick={() => setLeftOpen(o => !o)}
        side="left"
      />
      <CollapseBtn
        label={rightOpen ? "✕ Index" : "◆ Index"}
        onClick={() => setRightOpen(o => !o)}
        side="right"
      />
    </>
  );
}