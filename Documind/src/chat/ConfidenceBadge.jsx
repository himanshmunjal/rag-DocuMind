/**
 * ConfidenceBadge.jsx
 *
 * Standalone confidence indicator used in ChatWindow, SourcePanel,
 * and anywhere else a query response is displayed.
 *
 * Props
 * ─────
 *   level     "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT"  (required)
 *   score     number 0–1   optional — shows numeric score inside the badge
 *   size      "sm" | "md" | "lg"                           default "md"
 *   showLabel boolean                                       default true
 *   tooltip   boolean  — show explanation on hover          default true
 *   animated  boolean  — pulse the dot when LOW/INSUF       default true
 *
 * Usage
 * ─────
 *   import ConfidenceBadge from "@/components/chat/ConfidenceBadge";
 *
 *   <ConfidenceBadge level="HIGH" />
 *   <ConfidenceBadge level="LOW"  score={0.38} size="sm" />
 *   <ConfidenceBadge level="INSUFFICIENT" showLabel={false} />
 */

import { useState } from "react";

// ── data ──────────────────────────────────────────────────────────────────────

const CONFIG = {
  HIGH: {
    label:       "High confidence",
    shortLabel:  "High",
    description: "All retrieved chunks scored above the similarity threshold. Answer is well-grounded.",
    color:       "#4ade80",
    dimColor:    "#4ade8028",
    borderColor: "#4ade8042",
    dotColor:    "#16a34a",
    icon:        "◆",
  },
  MEDIUM: {
    label:       "Medium confidence",
    shortLabel:  "Medium",
    description: "Some retrieved chunks are relevant. Answer may be partially grounded — verify key claims.",
    color:       "#fbbf24",
    dimColor:    "#fbbf2428",
    borderColor: "#fbbf2442",
    dotColor:    "#d97706",
    icon:        "◈",
  },
  LOW: {
    label:       "Low confidence",
    shortLabel:  "Low",
    description: "Retrieval similarity is weak. Answer is generated from marginal context — treat with caution.",
    color:       "#f97316",
    dimColor:    "#f9731628",
    borderColor: "#f9731642",
    dotColor:    "#c2410c",
    icon:        "◇",
  },
  INSUFFICIENT: {
    label:       "Insufficient data",
    shortLabel:  "None",
    description: "No relevant content found in the uploaded documents. The model did not generate an answer.",
    color:       "#64748b",
    dimColor:    "#64748b28",
    borderColor: "#64748b42",
    dotColor:    "#475569",
    icon:        "○",
  },
};

// ── size tokens ───────────────────────────────────────────────────────────────

const SIZE = {
  sm: { fontSize: "10px", dotSize: "5px", padding: "1px 6px",  gap: "4px" },
  md: { fontSize: "11px", dotSize: "6px", padding: "2px 8px",  gap: "5px" },
  lg: { fontSize: "13px", dotSize: "8px", padding: "4px 11px", gap: "6px" },
};

// ── tooltip ───────────────────────────────────────────────────────────────────

function Tooltip({ text, color }) {
  return (
    <div style={{
      position:     "absolute",
      bottom:       "calc(100% + 7px)",
      left:         "50%",
      transform:    "translateX(-50%)",
      background:   "#111",
      border:       `1px solid ${color}40`,
      borderRadius: "6px",
      padding:      "7px 10px",
      width:        "210px",
      fontSize:     "11px",
      fontFamily:   "'DM Mono', monospace",
      color:        "#b0a090",
      lineHeight:   "1.55",
      zIndex:       100,
      pointerEvents: "none",
      boxShadow:    "0 4px 20px #00000080",
      animation:    "tooltipIn 0.12s ease-out",
      whiteSpace:   "normal",
    }}>
      {text}
      {/* Arrow */}
      <div style={{
        position:   "absolute",
        bottom:     "-5px",
        left:       "50%",
        transform:  "translateX(-50%) rotate(45deg)",
        width:      "8px",
        height:     "8px",
        background: "#111",
        border:     `1px solid ${color}40`,
        borderTop:  "none",
        borderLeft: "none",
      }} />
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function ConfidenceBadge({
  level     = "INSUFFICIENT",
  score     = null,
  size      = "md",
  showLabel = true,
  tooltip   = true,
  animated  = true,
}) {
  const [hovered, setHovered] = useState(false);

  const cfg  = CONFIG[level] || CONFIG.INSUFFICIENT;
  const sz   = SIZE[size]    || SIZE.md;

  // Pulse on states that warrant user attention
  const shouldPulse = animated && (level === "LOW" || level === "INSUFFICIENT");

  return (
    <>
      <style>{`
        @keyframes tooltipIn {
          from { opacity: 0; transform: translateX(-50%) translateY(3px); }
          to   { opacity: 1; transform: translateX(-50%) translateY(0);   }
        }
        @keyframes dotPulse {
          0%, 100% { opacity: 1;   transform: scale(1);   }
          50%       { opacity: 0.4; transform: scale(0.75); }
        }
      `}</style>

      <span
        onMouseEnter={() => tooltip && setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        style={{
          position:      "relative",
          display:       "inline-flex",
          alignItems:    "center",
          gap:           sz.gap,
          fontSize:      sz.fontSize,
          fontFamily:    "'DM Mono', monospace",
          letterSpacing: "0.06em",
          color:         cfg.color,
          background:    cfg.dimColor,
          border:        `1px solid ${cfg.borderColor}`,
          borderRadius:  "4px",
          padding:       sz.padding,
          cursor:        tooltip ? "default" : "inherit",
          userSelect:    "none",
          whiteSpace:    "nowrap",
          transition:    "border-color 0.2s",
          ...(hovered && { borderColor: cfg.color + "80" }),
        }}
      >
        {/* Animated dot */}
        <span style={{
          width:        sz.dotSize,
          height:       sz.dotSize,
          borderRadius: "50%",
          background:   cfg.dotColor,
          flexShrink:   0,
          animation:    shouldPulse ? "dotPulse 1.8s ease-in-out infinite" : "none",
        }} />

        {/* Label */}
        {showLabel && (
          <span>
            {size === "sm" ? cfg.shortLabel : cfg.label}
          </span>
        )}

        {/* Optional numeric score */}
        {score !== null && (
          <span style={{
            opacity:     0.7,
            borderLeft:  `1px solid ${cfg.color}40`,
            paddingLeft: sz.gap,
            marginLeft:  "1px",
          }}>
            {Math.round(score * 100)}%
          </span>
        )}

        {/* Tooltip */}
        {tooltip && hovered && (
          <Tooltip text={cfg.description} color={cfg.color} />
        )}
      </span>
    </>
  );
}