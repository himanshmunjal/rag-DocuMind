import { useState, useRef, useEffect } from "react";

const API = "http://localhost:8000/api/v1";

// ── helpers ──────────────────────────────────────────────────────────────────

const CONFIDENCE_META = {
  HIGH:         { label: "High confidence",   color: "#4ade80", dot: "#16a34a" },
  MEDIUM:       { label: "Medium confidence", color: "#fbbf24", dot: "#d97706" },
  LOW:          { label: "Low confidence",    color: "#f97316", dot: "#c2410c" },
  INSUFFICIENT: { label: "Insufficient data", color: "#94a3b8", dot: "#64748b" },
};

function ConfidenceBadge({ level }) {
  const meta = CONFIDENCE_META[level] || CONFIDENCE_META.INSUFFICIENT;
  return (
    <span style={{
      display:       "inline-flex",
      alignItems:    "center",
      gap:           "5px",
      fontSize:      "11px",
      fontFamily:    "'DM Mono', monospace",
      letterSpacing: "0.06em",
      color:         meta.color,
      background:    meta.color + "18",
      border:        `1px solid ${meta.color}38`,
      borderRadius:  "4px",
      padding:       "2px 8px",
    }}>
      <span style={{
        width: "6px", height: "6px",
        borderRadius: "50%",
        background: meta.dot,
        flexShrink: 0,
      }} />
      {meta.label}
    </span>
  );
}

function CitationChip({ citation, index, onClick, active }) {
  return (
    <button
      onClick={() => onClick(citation)}
      title={`${citation.source} — page ${citation.page}`}
      style={{
        display:       "inline-flex",
        alignItems:    "center",
        gap:           "4px",
        fontSize:      "10px",
        fontFamily:    "'DM Mono', monospace",
        padding:       "2px 7px",
        borderRadius:  "3px",
        border:        active ? "1px solid #d4a853" : "1px solid #2a2a2a",
        background:    active ? "#d4a85318" : "#141414",
        color:         active ? "#d4a853" : "#666",
        cursor:        "pointer",
        transition:    "all 0.15s",
        whiteSpace:    "nowrap",
      }}
    >
      <span style={{ opacity: 0.6 }}>↗</span>
      {citation.source.replace(".pdf", "")} p.{citation.page}
    </button>
  );
}

function MessageBubble({ msg, onCitationClick, activeCitation }) {
  const isUser = msg.role === "user";

  return (
    <div style={{
      display:       "flex",
      flexDirection: "column",
      alignItems:    isUser ? "flex-end" : "flex-start",
      marginBottom:  "24px",
      animation:     "fadeSlideIn 0.25s ease-out",
    }}>
      {/* Role label */}
      <span style={{
        fontSize:      "10px",
        fontFamily:    "'DM Mono', monospace",
        letterSpacing: "0.1em",
        color:         isUser ? "#d4a853" : "#555",
        marginBottom:  "6px",
        textTransform: "uppercase",
      }}>
        {isUser ? "You" : "DocuMind"}
      </span>

      {/* Bubble */}
      <div style={{
        maxWidth:     "82%",
        padding:      "14px 18px",
        borderRadius: isUser ? "12px 12px 2px 12px" : "2px 12px 12px 12px",
        background:   isUser ? "#d4a85314" : "#111",
        border:       isUser ? "1px solid #d4a85330" : "1px solid #1e1e1e",
        color:        "#e2d5c3",
        fontSize:     "14px",
        lineHeight:   "1.7",
        fontFamily:   "'Lora', Georgia, serif",
        whiteSpace:   "pre-wrap",
        wordBreak:    "break-word",
      }}>
        {msg.content}
      </div>

      {/* Footer: confidence + citations */}
      {!isUser && msg.confidence && (
        <div style={{
          display:    "flex",
          flexWrap:   "wrap",
          gap:        "6px",
          marginTop:  "8px",
          maxWidth:   "82%",
          alignItems: "center",
        }}>
          <ConfidenceBadge level={msg.confidence} />
          {(msg.citations || []).map((c, i) => (
            <CitationChip
              key={i}
              citation={c}
              index={i}
              onClick={onCitationClick}
              active={activeCitation === c}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ThinkingIndicator() {
  return (
    <div style={{
      display:    "flex",
      alignItems: "flex-start",
      marginBottom: "24px",
      animation: "fadeSlideIn 0.2s ease-out",
    }}>
      <div style={{
        padding:      "12px 18px",
        borderRadius: "2px 12px 12px 12px",
        background:   "#111",
        border:       "1px solid #1e1e1e",
      }}>
        <div style={{ display: "flex", gap: "5px", alignItems: "center" }}>
          {[0, 1, 2].map(i => (
            <span key={i} style={{
              width: "5px", height: "5px",
              borderRadius: "50%",
              background: "#d4a853",
              animation: `pulse 1.2s ease-in-out ${i * 0.2}s infinite`,
            }} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function ChatWindow({ onCitationSelect, activeCitation, selectedSources }) {
  const [messages,    setMessages]    = useState([]);
  const [input,       setInput]       = useState("");
  const [loading,     setLoading]     = useState(false);
  const [error,       setError]       = useState(null);
  const bottomRef  = useRef(null);
  const inputRef   = useRef(null);

  // Welcome message on mount
  useEffect(() => {
    setMessages([{
      role:    "assistant",
      content: "Upload your documents and ask me anything about them. I'll cite every source.",
    }]);
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function sendMessage() {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setError(null);
    setMessages(prev => [...prev, { role: "user", content: text }]);
    setLoading(true);

    try {
      const body = { query: text };
      if (selectedSources?.length) body.sources = selectedSources;

      const res  = await fetch(`${API}/query`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      setMessages(prev => [...prev, {
        role:       "assistant",
        content:    data.answer,
        confidence: data.confidence,
        citations:  data.citations || [],
      }]);
    } catch (err) {
      setError(err.message);
      setMessages(prev => [...prev, {
        role:    "assistant",
        content: `Something went wrong: ${err.message}`,
        confidence: "INSUFFICIENT",
        citations: [],
      }]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  function handleKey(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora:ital,wght@0,400;0,500;1,400&family=Playfair+Display:wght@600&display=swap');

        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(8px); }
          to   { opacity: 1; transform: translateY(0);   }
        }
        @keyframes pulse {
          0%, 100% { opacity: 0.3; transform: scale(0.8); }
          50%       { opacity: 1;   transform: scale(1.1); }
        }

        .chat-input::placeholder { color: #3a3a3a; }
        .chat-input:focus { outline: none; border-color: #d4a85355 !important; }
        .send-btn:hover:not(:disabled) {
          background: #d4a853 !important;
          color: #0a0a0a !important;
        }
        .send-btn:disabled { opacity: 0.35; cursor: not-allowed; }
      `}</style>

      <div style={{
        display:        "flex",
        flexDirection:  "column",
        height:         "100%",
        background:     "#0a0a0a",
        fontFamily:     "'Lora', Georgia, serif",
      }}>

        {/* Header */}
        <div style={{
          padding:      "16px 24px",
          borderBottom: "1px solid #1a1a1a",
          display:      "flex",
          alignItems:   "center",
          justifyContent: "space-between",
        }}>
          <span style={{
            fontFamily:    "'Playfair Display', Georgia, serif",
            fontSize:      "16px",
            fontWeight:    "600",
            color:         "#e2d5c3",
            letterSpacing: "0.02em",
          }}>
            DocuMind
          </span>
          {selectedSources?.length > 0 && (
            <span style={{
              fontSize:   "11px",
              fontFamily: "'DM Mono', monospace",
              color:      "#d4a853",
              opacity:    0.8,
            }}>
              {selectedSources.length} doc{selectedSources.length > 1 ? "s" : ""} active
            </span>
          )}
        </div>

        {/* Messages */}
        <div style={{
          flex:       1,
          overflowY:  "auto",
          padding:    "28px 24px 16px",
          scrollbarWidth: "thin",
          scrollbarColor: "#1e1e1e transparent",
        }}>
          {messages.map((msg, i) => (
            <MessageBubble
              key={i}
              msg={msg}
              onCitationClick={onCitationSelect}
              activeCitation={activeCitation}
            />
          ))}
          {loading && <ThinkingIndicator />}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div style={{
          padding:    "16px 24px 20px",
          borderTop:  "1px solid #1a1a1a",
        }}>
          <div style={{
            display:      "flex",
            gap:          "10px",
            alignItems:   "flex-end",
            background:   "#0f0f0f",
            border:       "1px solid #222",
            borderRadius: "10px",
            padding:      "10px 14px",
            transition:   "border-color 0.2s",
          }}>
            <textarea
              ref={inputRef}
              className="chat-input"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKey}
              placeholder="Ask anything about your documents…"
              rows={1}
              style={{
                flex:       1,
                background: "transparent",
                border:     "none",
                color:      "#e2d5c3",
                fontSize:   "14px",
                fontFamily: "'Lora', Georgia, serif",
                lineHeight: "1.6",
                resize:     "none",
                maxHeight:  "120px",
                overflowY:  "auto",
              }}
            />
            <button
              className="send-btn"
              onClick={sendMessage}
              disabled={!input.trim() || loading}
              style={{
                flexShrink:   0,
                width:        "34px",
                height:       "34px",
                borderRadius: "7px",
                border:       "1px solid #d4a85355",
                background:   "transparent",
                color:        "#d4a853",
                fontSize:     "16px",
                cursor:       "pointer",
                display:      "flex",
                alignItems:   "center",
                justifyContent: "center",
                transition:   "all 0.15s",
              }}
            >
              ↑
            </button>
          </div>
          <p style={{
            margin:     "8px 0 0",
            fontSize:   "11px",
            fontFamily: "'DM Mono', monospace",
            color:      "#2e2e2e",
            textAlign:  "center",
          }}>
            Shift+Enter for new line · answers grounded in your documents only
          </p>
        </div>
      </div>
    </>
  );
}