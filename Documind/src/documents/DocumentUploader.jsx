import { useState, useRef, useCallback } from "react";

const API = "http://localhost:8000/api/v1";

// ── sub-components ────────────────────────────────────────────────────────────

function FileRow({ file, status }) {
  const isUploading = status?.state === "uploading";
  const isDone      = status?.state === "done";
  const isSkipped   = status?.state === "skipped";
  const isError     = status?.state === "error";

  const stateColor = {
    uploading: "#fbbf24",
    done:      "#4ade80",
    skipped:   "#60a5fa",
    error:     "#f87171",
  }[status?.state] || "#444";

  return (
    <div style={{
      display:      "flex",
      alignItems:   "center",
      gap:          "10px",
      padding:      "10px 14px",
      borderRadius: "6px",
      background:   "#0f0f0f",
      border:       `1px solid ${stateColor}28`,
      animation:    "fadeIn 0.2s ease-out",
    }}>
      {/* PDF icon */}
      <span style={{ fontSize: "18px", flexShrink: 0 }}>📄</span>

      {/* Name + status */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize:     "13px",
          fontFamily:   "'Lora', Georgia, serif",
          color:        "#e2d5c3",
          overflow:     "hidden",
          textOverflow: "ellipsis",
          whiteSpace:   "nowrap",
        }}>
          {file.name}
        </div>
        <div style={{
          fontSize:   "11px",
          fontFamily: "'DM Mono', monospace",
          color:      stateColor,
          marginTop:  "2px",
        }}>
          {isUploading && "Indexing…"}
          {isDone      && `✓ ${status.chunks} chunks indexed`}
          {isSkipped   && "Already indexed"}
          {isError     && `✗ ${status.message}`}
          {!status     && `${(file.size / 1024).toFixed(0)} KB`}
        </div>
      </div>

      {/* Spinner or checkmark */}
      {isUploading && (
        <div style={{
          width:        "16px",
          height:       "16px",
          borderRadius: "50%",
          border:       "2px solid #fbbf2440",
          borderTopColor: "#fbbf24",
          animation:    "spin 0.7s linear infinite",
          flexShrink:   0,
        }} />
      )}
      {(isDone || isSkipped) && (
        <span style={{ color: stateColor, fontSize: "14px", flexShrink: 0 }}>✓</span>
      )}
      {isError && (
        <span style={{ color: stateColor, fontSize: "14px", flexShrink: 0 }}>✗</span>
      )}
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function DocumentUploader({ onUploadComplete }) {
  const [dragging,  setDragging]  = useState(false);
  const [queue,     setQueue]     = useState([]);   // { file, status }[]
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef(null);

  // ── drag handlers ──────────────────────────────────────────────────────────

  const handleDragOver  = useCallback(e => { e.preventDefault(); setDragging(true);  }, []);
  const handleDragLeave = useCallback(e => { e.preventDefault(); setDragging(false); }, []);

  const handleDrop = useCallback(e => {
    e.preventDefault();
    setDragging(false);
    const files = Array.from(e.dataTransfer.files).filter(f => f.type === "application/pdf");
    if (files.length) processFiles(files);
  }, []);

  const handleInputChange = e => {
    const files = Array.from(e.target.files).filter(f => f.type === "application/pdf");
    if (files.length) processFiles(files);
    e.target.value = "";
  };

  // ── upload logic ───────────────────────────────────────────────────────────

  function processFiles(files) {
    const newEntries = files.map(f => ({ file: f, status: null }));
    setQueue(prev => [...prev, ...newEntries]);
    uploadAll(files);
  }

  async function uploadAll(files) {
    setUploading(true);
    for (const file of files) {
      await uploadOne(file);
    }
    setUploading(false);
    onUploadComplete?.();
  }

  async function uploadOne(file) {
    // Mark as uploading
    setQueue(prev => prev.map(e =>
      e.file === file ? { ...e, status: { state: "uploading" } } : e
    ));

    try {
      const form = new FormData();
      form.append("file", file);
      form.append("force", "false");

      const res  = await fetch(`${API}/upload`, { method: "POST", body: form });
      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}`);
      }

      setQueue(prev => prev.map(e =>
        e.file === file
          ? { ...e, status: data.skipped
              ? { state: "skipped" }
              : { state: "done", chunks: data.chunks_indexed }
            }
          : e
      ));
    } catch (err) {
      setQueue(prev => prev.map(e =>
        e.file === file
          ? { ...e, status: { state: "error", message: err.message } }
          : e
      ));
    }
  }

  const clearDone = () =>
    setQueue(prev => prev.filter(e => !e.status || e.status.state === "uploading"));

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lora&family=Playfair+Display:wght@600&display=swap');

        @keyframes fadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }
        @keyframes spin   { to   { transform: rotate(360deg); } }

        .drop-zone:hover { border-color: #d4a85360 !important; background: #d4a85306 !important; }
        .drop-zone.drag  { border-color: #d4a853 !important;   background: #d4a85310 !important; }
        .clear-btn:hover { color: #e2d5c3 !important; }
        .browse-link:hover { text-decoration: underline; cursor: pointer; }
      `}</style>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>

        {/* Header */}
        <div style={{ marginBottom: "4px" }}>
          <h2 style={{
            margin:        0,
            fontFamily:    "'Playfair Display', Georgia, serif",
            fontSize:      "15px",
            fontWeight:    "600",
            color:         "#e2d5c3",
            letterSpacing: "0.01em",
          }}>
            Documents
          </h2>
          <p style={{
            margin:     "4px 0 0",
            fontSize:   "11px",
            fontFamily: "'DM Mono', monospace",
            color:      "#444",
          }}>
            Upload PDFs to index
          </p>
        </div>

        {/* Drop zone */}
        <div
          className={`drop-zone${dragging ? " drag" : ""}`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
          style={{
            border:        "1px dashed #2a2a2a",
            borderRadius:  "8px",
            padding:       "24px 16px",
            textAlign:     "center",
            cursor:        "pointer",
            transition:    "all 0.2s",
            background:    "#0a0a0a",
          }}
        >
          <div style={{ fontSize: "24px", marginBottom: "8px", opacity: 0.5 }}>⬆</div>
          <p style={{
            margin:     0,
            fontSize:   "13px",
            fontFamily: "'Lora', Georgia, serif",
            color:      "#555",
            lineHeight: "1.5",
          }}>
            Drop PDFs here or{" "}
            <span className="browse-link" style={{ color: "#d4a853" }}>browse</span>
          </p>
          <p style={{
            margin:     "4px 0 0",
            fontSize:   "11px",
            fontFamily: "'DM Mono', monospace",
            color:      "#333",
          }}>
            PDF · max 50 MB
          </p>
          <input
            ref={inputRef}
            type="file"
            accept=".pdf,application/pdf"
            multiple
            onChange={handleInputChange}
            style={{ display: "none" }}
          />
        </div>

        {/* File queue */}
        {queue.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
            {queue.map(({ file, status }, i) => (
              <FileRow key={i} file={file} status={status} />
            ))}

            {/* Clear finished */}
            {queue.some(e => e.status && e.status.state !== "uploading") && (
              <button
                className="clear-btn"
                onClick={clearDone}
                style={{
                  marginTop:    "4px",
                  background:   "transparent",
                  border:       "none",
                  color:        "#444",
                  fontSize:     "11px",
                  fontFamily:   "'DM Mono', monospace",
                  cursor:       "pointer",
                  textAlign:    "left",
                  padding:      "0",
                  transition:   "color 0.15s",
                }}
              >
                Clear finished
              </button>
            )}
          </div>
        )}
      </div>
    </>
  );
}