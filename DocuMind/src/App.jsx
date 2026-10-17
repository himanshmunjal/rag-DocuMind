// import { useState, useCallback } from "react";
// import ChatWindow from "./components/chat/ChatWindow";
// import CompanySearch from "./components/documents/CompanySearch";
// import SourcePanel from "./components/chat/SourcePanel";

// // ── layout constants ──────────────────────────────────────────────────────────

// const LEFT_W = "260px";
// const RIGHT_W = "300px";

// // ── branding strip (top of left sidebar) ─────────────────────────────────────

// function Brand() {
//   return (
//     <div
//       style={{
//         padding: "22px 20px 18px",
//         borderBottom: "1px solid #1a1a1a",
//       }}
//     >
//       <div style={{ display: "flex", alignItems: "center", gap: "9px" }}>
//         {/* Logo mark */}
//         <div
//           style={{
//             width: "28px",
//             height: "28px",
//             borderRadius: "6px",
//             background: "linear-gradient(135deg, #d4a853 0%, #a07830 100%)",
//             display: "flex",
//             alignItems: "center",
//             justifyContent: "center",
//             flexShrink: 0,
//             fontSize: "13px",
//           }}
//         >
//           ◆
//         </div>
//         <div>
//           <div
//             style={{
//               fontFamily: "'Playfair Display', Georgia, serif",
//               fontSize: "15px",
//               fontWeight: "600",
//               color: "#e2d5c3",
//               letterSpacing: "0.01em",
//               lineHeight: "1",
//             }}
//           >
//             DocuMind
//           </div>
//           <div
//             style={{
//               fontFamily: "'DM Mono', monospace",
//               fontSize: "10px",
//               color: "#444",
//               marginTop: "3px",
//               letterSpacing: "0.06em",
//             }}
//           >
//             Document intelligence
//           </div>
//         </div>
//       </div>
//     </div>
//   );
// }

// // ── collapse button (mobile toggle) ──────────────────────────────────────────

// function CollapseBtn({ label, onClick, side }) {
//   return (
//     <button
//       onClick={onClick}
//       title={label}
//       style={{
//         position: "fixed",
//         bottom: "24px",
//         [side]: "12px",
//         zIndex: 200,
//         background: "#111",
//         border: "1px solid #2a2a2a",
//         borderRadius: "8px",
//         color: "#d4a853",
//         fontFamily: "'DM Mono', monospace",
//         fontSize: "11px",
//         padding: "7px 12px",
//         cursor: "pointer",
//         display: "none", // shown via media query class
//       }}
//       className="mobile-toggle"
//     >
//       {label}
//     </button>
//   );
// }

// // ── empty-state hint shown in chat before any documents are indexed ───────────

// function EmptyHint() {
//   return (
//     <div
//       style={{
//         position: "absolute",
//         inset: 0,
//         display: "flex",
//         flexDirection: "column",
//         alignItems: "center",
//         justifyContent: "center",
//         pointerEvents: "none",
//         padding: "40px",
//         gap: "12px",
//         opacity: 0.35,
//       }}
//     >
//       <div style={{ fontSize: "36px" }}>📄</div>
//       <p
//         style={{
//           fontFamily: "'DM Mono', monospace",
//           fontSize: "12px",
//           color: "#888",
//           textAlign: "center",
//           lineHeight: "1.7",
//           margin: 0,
//           maxWidth: "260px",
//         }}
//       >
//         Search for a company on the left,
//         <br />
//         ingest a filing, then ask a question below.
//       </p>
//     </div>
//   );
// }

// // ── root ──────────────────────────────────────────────────────────────────────

// export default function App() {
//   const [activeCitation, setActiveCitation] = useState(null);
//   const [selectedSources, setSelectedSources] = useState([]);
//   const [refreshTrigger, setRefreshTrigger] = useState(0);
//   const [hasMessages, setHasMessages] = useState(false);

//   // Left sidebar visible on mobile
//   const [leftOpen, setLeftOpen] = useState(false);
//   // Right panel visible on mobile
//   const [rightOpen, setRightOpen] = useState(false);

//   // ── handlers ──────────────────────────────────────────────────────────────

//   const handleIngestComplete = useCallback(() => {
//     setRefreshTrigger((n) => n + 1);
//   }, []);

//   const handleSourceToggle = useCallback((source) => {
//     setSelectedSources((prev) =>
//       prev.includes(source)
//         ? prev.filter((s) => s !== source)
//         : [...prev, source]
//     );
//   }, []);

//   const handleCitationSelect = useCallback((citation) => {
//     setActiveCitation((prev) => (prev === citation ? null : citation));
//     // Open right panel on mobile when a citation is clicked
//     setRightOpen(true);
//   }, []);

//   // ── render ─────────────────────────────────────────────────────────────────

//   return (
//     <>
//       <style>{`
//         @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora:ital,wght@0,400;0,500;1,400&family=Playfair+Display:wght@600&display=swap');

//         *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

//         html, body, #root {
//           height:     100%;
//           background: #0a0a0a;
//           color:      #e2d5c3;
//         }

//         /* Scrollbars */
//         ::-webkit-scrollbar       { width: 4px; height: 4px; }
//         ::-webkit-scrollbar-track { background: transparent; }
//         ::-webkit-scrollbar-thumb { background: #1e1e1e; border-radius: 2px; }

//         /* Mobile breakpoint */
//         @media (max-width: 768px) {
//           .left-sidebar  { transform: translateX(-100%); transition: transform 0.25s ease; }
//           .right-sidebar { transform: translateX(100%);  transition: transform 0.25s ease; }
//           .left-sidebar.open  { transform: translateX(0); }
//           .right-sidebar.open { transform: translateX(0); }
//           .mobile-toggle { display: block !important; }
//         }

//         /* Thin dividers */
//         .divider-v {
//           width:      1px;
//           background: #1a1a1a;
//           flex-shrink: 0;
//         }
//       `}</style>

//       {/* ── Three-column shell ── */}
//       <div
//         style={{
//           display: "flex",
//           height: "100vh",
//           overflow: "hidden",
//         }}
//       >
//         {/* ── LEFT SIDEBAR ── */}
//         <div
//           className={`left-sidebar${leftOpen ? " open" : ""}`}
//           style={{
//             width: LEFT_W,
//             minWidth: LEFT_W,
//             display: "flex",
//             flexDirection: "column",
//             background: "#080808",
//             borderRight: "1px solid #1a1a1a",
//             overflowY: "auto",
//             position: "relative",
//             zIndex: 50,
//           }}
//         >
//           <Brand />

//           {/* Company search / filing ingestion */}
//           <div style={{ padding: "20px 16px", flex: 1 }}>
//             <CompanySearch onIngestComplete={handleIngestComplete} />
//           </div>

//           {/* Footer */}
//           <div
//             style={{
//               padding: "14px 16px",
//               borderTop: "1px solid #141414",
//             }}
//           >
//             <p
//               style={{
//                 fontSize: "10px",
//                 fontFamily: "'DM Mono', monospace",
//                 color: "#2e2e2e",
//                 lineHeight: "1.6",
//               }}
//             >
//               Answers grounded in your documents only. No external knowledge
//               used.
//             </p>
//           </div>
//         </div>

//         <div className="divider-v" />

//         {/* ── CENTRE: CHAT ── */}
//         <div
//           style={{
//             flex: 1,
//             display: "flex",
//             flexDirection: "column",
//             position: "relative",
//             minWidth: 0,
//           }}
//         >
//           {/* Empty-state overlay — hidden once messages appear */}
//           {!hasMessages && <EmptyHint />}

//           <ChatWindow
//             onCitationSelect={handleCitationSelect}
//             activeCitation={activeCitation}
//             selectedSources={
//               selectedSources.length > 0 ? selectedSources : null
//             }
//             onFirstMessage={() => setHasMessages(true)}
//           />
//         </div>

//         <div className="divider-v" />

//         {/* ── RIGHT PANEL ── */}
//         <div
//           className={`right-sidebar${rightOpen ? " open" : ""}`}
//           style={{
//             width: RIGHT_W,
//             minWidth: RIGHT_W,
//             position: "relative",
//             zIndex: 50,
//           }}
//         >
//           <SourcePanel
//             activeCitation={activeCitation}
//             selectedSources={selectedSources}
//             onSourceToggle={handleSourceToggle}
//             refreshTrigger={refreshTrigger}
//           />
//         </div>
//       </div>

//       {/* ── Mobile toggles ── */}
//       <CollapseBtn
//         label={leftOpen ? "✕ Docs" : "⬆ Docs"}
//         onClick={() => setLeftOpen((o) => !o)}
//         side="left"
//       />
//       <CollapseBtn
//         label={rightOpen ? "✕ Index" : "◆ Index"}
//         onClick={() => setRightOpen((o) => !o)}
//         side="right"
//       />
//     </>
//   );
// }

import { useState, useEffect, useCallback } from "react";
import ChatWindow from "./components/chat/ChatWindow";
import CompanySearch from "./components/documents/CompanySearch";
import SourcePanel from "./components/chat/SourcePanel";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";

// ── branding strip (top of left sidebar) ─────────────────────────────────────

function Brand() {
  return (
    <div className="px-5 pt-[22px] pb-[18px] border-b border-[#1a1a1a]">
      <div className="flex items-center gap-2.5">
        <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 text-[13px] bg-gradient-to-br from-[#d4a853] to-[#a07830]">
          ◆
        </div>
        <div>
          <div className="font-['Playfair_Display',Georgia,serif] text-[15px] font-semibold text-[#e2d5c3] leading-none tracking-[0.01em]">
            DocuMind
          </div>
          <div className="font-['DM_Mono',monospace] text-[10px] text-[#444] mt-[3px] tracking-[0.06em]">
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
      className={`mobile-toggle hidden fixed bottom-6 ${side === "left" ? "left-3" : "right-3"} z-[200]
                  bg-[#111] border border-[#2a2a2a] rounded-lg text-[#d4a853]
                  font-['DM_Mono',monospace] text-[11px] px-3 py-[7px] cursor-pointer`}
    >
      {label}
    </button>
  );
}

// ── empty-state hint shown in chat before any documents are indexed ───────────

function EmptyHint() {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none p-10 gap-3 opacity-35">
      <div className="text-4xl">📄</div>
      <p className="font-['DM_Mono',monospace] text-xs text-[#888] text-center leading-[1.7] max-w-[260px] m-0">
        Search for a company on the left,
        <br />
        ingest a filing, then ask a question below.
      </p>
    </div>
  );
}

// ── root ──────────────────────────────────────────────────────────────────────

export default function App() {
  const [activeCitation, setActiveCitation] = useState(null);
  const [selectedSources, setSelectedSources] = useState([]);
  const [hasMessages, setHasMessages] = useState(false);

  // Document list — single source of truth, shared by SourcePanel (renders it)
  // and ChatWindow (needs it to derive `cik` for numeric/computed queries).
  const [documents, setDocuments] = useState([]);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);

  // ── load document index ──────────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setDocumentsLoading(true);
      try {
        const res = await fetch(`${API_BASE}/documents`);
        const data = await res.json();
        if (!cancelled) setDocuments(data.documents || []);
      } catch {
        if (!cancelled) setDocuments([]);
      } finally {
        if (!cancelled) setDocumentsLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [refreshTrigger]);

  // ── handlers ──────────────────────────────────────────────────────────────

  const handleIngestComplete = useCallback(() => {
    setRefreshTrigger((n) => n + 1);
  }, []);

  const handleDeleteDocument = useCallback(async (source) => {
    try {
      await fetch(`${API_BASE}/documents/${encodeURIComponent(source)}`, { method: "DELETE" });
    } catch {
      // best-effort — still drop it from local state below so the UI doesn't get stuck
    }
    setDocuments((prev) => prev.filter((d) => d.source !== source));
    setSelectedSources((prev) => prev.filter((s) => s !== source));
  }, []);

  const handleSourceToggle = useCallback((source) => {
    setSelectedSources((prev) =>
      prev.includes(source) ? prev.filter((s) => s !== source) : [...prev, source]
    );
  }, []);

  const handleCitationSelect = useCallback((citation) => {
    setActiveCitation((prev) => (prev === citation ? null : citation));
    setRightOpen(true);
  }, []);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora:ital,wght@0,400;0,500;1,400&family=Playfair+Display:wght@600&display=swap');
        html, body, #root { height: 100%; background: #0a0a0a; color: #e2d5c3; }
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #1e1e1e; border-radius: 2px; }
        @media (max-width: 768px) {
          .left-sidebar  { transform: translateX(-100%); transition: transform 0.25s ease; }
          .right-sidebar { transform: translateX(100%);  transition: transform 0.25s ease; }
          .left-sidebar.open  { transform: translateX(0); }
          .right-sidebar.open { transform: translateX(0); }
          .mobile-toggle { display: block !important; }
        }
      `}</style>

      <div className="flex h-screen overflow-hidden">
        {/* ── LEFT SIDEBAR ── */}
        <div
          className={`left-sidebar${leftOpen ? " open" : ""} w-[260px] min-w-[260px] flex flex-col
                      bg-[#080808] border-r border-[#1a1a1a] overflow-y-auto relative z-50`}
        >
          <Brand />

          <div className="px-4 py-5 flex-1">
            <CompanySearch onIngestComplete={handleIngestComplete} />
          </div>

          <div className="px-4 py-3.5 border-t border-[#141414]">
            <p className="text-[10px] font-['DM_Mono',monospace] text-[#2e2e2e] leading-[1.6]">
              Answers grounded in your documents only. No external knowledge used.
            </p>
          </div>
        </div>

        <div className="w-px bg-[#1a1a1a] flex-shrink-0" />

        {/* ── CENTRE: CHAT ── */}
        <div className="flex-1 flex flex-col relative min-w-0">
          {!hasMessages && <EmptyHint />}

          <ChatWindow
            documents={documents}
            onCitationSelect={handleCitationSelect}
            activeCitation={activeCitation}
            selectedSources={selectedSources}
            onFirstMessage={() => setHasMessages(true)}
          />
        </div>

        <div className="w-px bg-[#1a1a1a] flex-shrink-0" />

        {/* ── RIGHT PANEL ── */}
        <div className={`right-sidebar${rightOpen ? " open" : ""} w-[300px] min-w-[300px] relative z-50`}>
          <SourcePanel
            documents={documents}
            documentsLoading={documentsLoading}
            onDeleteDocument={handleDeleteDocument}
            activeCitation={activeCitation}
            selectedSources={selectedSources}
            onSourceToggle={handleSourceToggle}
          />
        </div>
      </div>

      <CollapseBtn label={leftOpen ? "✕ Docs" : "⬆ Docs"} onClick={() => setLeftOpen((o) => !o)} side="left" />
      <CollapseBtn label={rightOpen ? "✕ Index" : "◆ Index"} onClick={() => setRightOpen((o) => !o)} side="right" />
    </>
  );
}