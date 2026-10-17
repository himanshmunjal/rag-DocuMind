import { useState, useRef, useEffect, useMemo } from "react";
import ConfidenceBadge from "./ConfidenceBadge";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";

/**
 * ChatWindow
 *
 * Props:
 *   - documents: [{ source, cik, company_name, form_type, chunk_count }, ...]
 *     Full ingested-document list, lifted up in App.jsx. Used to derive `cik`
 *     when the source filter pins down one company. Without a cik the backend
 *     resolves companies named in the question itself (routes.py: query() →
 *     find_companies), including multi-company comparisons.
 *   - selectedSources: string[]  accession numbers currently filtered in
 *     SourcePanel's "INDEX" tab (empty = search across all ingested filings).
 *   - onCitationSelect(citation), activeCitation, onFirstMessage — as before.
 *
 * cik resolution rule: if the user has filtered down to filing(s) from a
 * single company, we can confidently send that cik. If nothing is filtered
 * and more than one company is ingested, we send cik=null and let the backend
 * pick up company names from the question ("Apple's revenue", "Apple vs
 * Microsoft") rather than guess from the filter. If exactly
 * one company is ingested total, we use it even with no filter — there's no
 * ambiguity to resolve.
 */
function ChatWindow({ documents = [], onCitationSelect, activeCitation, selectedSources, onFirstMessage }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);

  const scrollRef = useRef(null);
  const hasSentFirst = useRef(false);

  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  // ── derive cik + accession_numbers to send from current source filter ────
  const { resolvedCik, scopeLabel } = useMemo(() => {
    const uniqueCiks = [...new Set(documents.map((d) => d.cik))];

    if (selectedSources && selectedSources.length > 0) {
      const selectedDocs = documents.filter((d) => selectedSources.includes(d.source));
      const ciks = [...new Set(selectedDocs.map((d) => d.cik))];
      if (ciks.length === 1) {
        const name = selectedDocs[0]?.company_name || ciks[0];
        return { resolvedCik: ciks[0], scopeLabel: name };
      }
      // multiple companies selected — no single cik to send
      return { resolvedCik: null, scopeLabel: `${ciks.length} companies selected` };
    }

    // no filter applied — only unambiguous if exactly one company is ingested
    if (uniqueCiks.length === 1) {
      return { resolvedCik: uniqueCiks[0], scopeLabel: documents[0]?.company_name || uniqueCiks[0] };
    }
    if (uniqueCiks.length > 1) {
      return { resolvedCik: null, scopeLabel: "All documents — name the company (or companies) in your question" };
    }
    return { resolvedCik: null, scopeLabel: null };
  }, [documents, selectedSources]);

  const handleSend = async () => {
    const question = input.trim();
    if (!question || isLoading) return;

    if (!hasSentFirst.current) {
      hasSentFirst.current = true;
      onFirstMessage?.();
    }

    const userMessage = { role: "user", content: question, id: crypto.randomUUID() };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setIsLoading(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE_URL}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          cik: resolvedCik,
          accession_numbers: selectedSources && selectedSources.length > 0 ? selectedSources : null,
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Request failed (${res.status})`);
      }

      const data = await res.json();

      const assistantMessage = {
        role: "assistant",
        content: data.answer,
        confidence: data.confidence,
        citations: data.citations || [],
        queryType: data.query_type,
        id: crypto.randomUUID(),
      };
      setMessages((prev) => [...prev, assistantMessage]);
    } catch (err) {
      setError(err.message || "Something went wrong while fetching the answer.");
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col h-full font-['DM_Mono',monospace]">
      {/* ── scope indicator ── */}
      {scopeLabel && (
        <div className="px-7 pt-3 text-[10px] text-[#444] tracking-[0.04em]">
          Scope: <span className="text-[#888]">{scopeLabel}</span>
        </div>
      )}

      {/* ── message list ── */}
      <div className="flex-1 overflow-y-auto px-7 py-6 flex flex-col gap-[18px]">
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`max-w-[72%] flex flex-col gap-2 ${msg.role === "user" ? "self-end" : "self-start"}`}
          >
            <div
              className={`rounded-[10px] px-4 py-3 font-['Lora',Georgia,serif] text-sm leading-[1.65] text-[#e2d5c3] whitespace-pre-wrap border ${
                msg.role === "user" ? "bg-[#161310] border-[#242424]" : "bg-[#0d0d0d] border-[#1a1a1a]"
              }`}
            >
              {msg.content}
            </div>

            {msg.role === "assistant" && (
              <div className="flex items-center flex-wrap gap-2">
                {typeof msg.confidence === "string" && (
                  <ConfidenceBadge level={msg.confidence} />
                )}
                {msg.citations?.map((citation, i) => (
                  <button
                    key={citation.id ?? i}
                    onClick={() => onCitationSelect?.(citation)}
                    className={`text-[10px] tracking-[0.03em] rounded-[5px] px-2 py-[3px] border border-[#d4a853] ${
                      activeCitation === citation ? "bg-[#d4a853] text-[#0a0a0a]" : "bg-transparent text-[#d4a853]"
                    }`}
                  >
                    [{i + 1}] {citation.item_title ?? citation.company_name ?? citation.form_type ?? "source"}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}

        {isLoading && (
          <div className="self-start flex gap-1 px-4 py-3">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="w-[5px] h-[5px] rounded-full bg-[#444] animate-pulse"
                style={{ animationDelay: `${i * 0.15}s` }}
              />
            ))}
          </div>
        )}

        {error && (
          <div className="self-start text-[11px] text-[#c9645a] border border-[#c9645a44] rounded-md px-3 py-2">
            {error}
          </div>
        )}

        <div ref={scrollRef} />
      </div>

      {/* ── input bar ── */}
      <div className="flex gap-2.5 px-7 pt-4 pb-5 border-t border-[#1a1a1a]">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="e.g. What was Google's revenue in FY2024?"
          rows={1}
          disabled={isLoading}
          className="flex-1 resize-none bg-[#111] border border-[#242424] rounded-lg px-3.5 py-2.5
                     text-[#e2d5c3] text-[13px] outline-none focus:border-[#d4a853]"
        />
        <button
          onClick={handleSend}
          disabled={isLoading || !input.trim()}
          className={`rounded-lg px-5 text-xs font-semibold tracking-[0.02em] ${
            !input.trim() || isLoading
              ? "bg-[#1a1a1a] text-[#444] cursor-default"
              : "bg-[#d4a853] text-[#0a0a0a] cursor-pointer hover:bg-[#e0b666]"
          }`}
        >
          Send
        </button>
      </div>
    </div>
  );
}

export default ChatWindow;