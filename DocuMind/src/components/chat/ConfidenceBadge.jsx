import { useState } from "react";

const CONFIG = {
  HIGH: {
    label: "High confidence",
    shortLabel: "High",
    description: "All retrieved chunks scored above the similarity threshold. Answer is well-grounded.",
    color: "#4ade80",
    dimColor: "#4ade8028",
    borderColor: "#4ade8042",
    dotColor: "#16a34a",
  },
  MEDIUM: {
    label: "Medium confidence",
    shortLabel: "Medium",
    description: "Some retrieved chunks are relevant. Answer may be partially grounded — verify key claims.",
    color: "#fbbf24",
    dimColor: "#fbbf2428",
    borderColor: "#fbbf2442",
    dotColor: "#d97706",
  },
  LOW: {
    label: "Low confidence",
    shortLabel: "Low",
    description: "Retrieval similarity is weak. Answer is generated from marginal context — treat with caution.",
    color: "#f97316",
    dimColor: "#f9731628",
    borderColor: "#f9731642",
    dotColor: "#c2410c",
  },
  INSUFFICIENT: {
    label: "Insufficient data",
    shortLabel: "None",
    description: "No relevant content found in the uploaded documents. The model did not generate an answer.",
    color: "#64748b",
    dimColor: "#64748b28",
    borderColor: "#64748b42",
    dotColor: "#475569",
  },
};

const SIZE = {
  sm: { text: "text-[10px]", dot: "w-[5px] h-[5px]", pad: "px-1.5 py-px", gap: "gap-1" },
  md: { text: "text-[11px]", dot: "w-1.5 h-1.5", pad: "px-2 py-0.5", gap: "gap-[5px]" },
  lg: { text: "text-[13px]", dot: "w-2 h-2", pad: "px-[11px] py-1", gap: "gap-1.5" },
};

/**
 * ConfidenceBadge
 *
 * Props:
 *   - level: "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT"  (matches the
 *     ConfidenceLevel enum your backend returns as a string in QueryResponse)
 *   - score: optional numeric 0-1, shown as a "72%" suffix if provided
 *   - size: "sm" | "md" | "lg"
 *   - showLabel, tooltip, animated: as before
 */
export default function ConfidenceBadge({
  level = "INSUFFICIENT",
  score = null,
  size = "md",
  showLabel = true,
  tooltip = true,
  animated = true,
}) {
  const [hovered, setHovered] = useState(false);

  const cfg = CONFIG[level] || CONFIG.INSUFFICIENT;
  const sz = SIZE[size] || SIZE.md;
  const shouldPulse = animated && (level === "LOW" || level === "INSUFFICIENT");

  return (
    <>
      <style>{`
        @keyframes tooltipIn {
          from { opacity: 0; transform: translateX(-50%) translateY(3px); }
          to   { opacity: 1; transform: translateX(-50%) translateY(0);   }
        }
        @keyframes dotPulse {
          0%, 100% { opacity: 1;    transform: scale(1);    }
          50%      { opacity: 0.4;  transform: scale(0.75); }
        }
      `}</style>

      <span
        onMouseEnter={() => tooltip && setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        style={{
          "--badge-color": cfg.color,
          "--badge-dim": cfg.dimColor,
          "--badge-border": hovered ? cfg.color + "80" : cfg.borderColor,
        }}
        className={`relative inline-flex items-center ${sz.gap} ${sz.text} font-['DM_Mono',monospace]
                    tracking-[0.06em] rounded select-none whitespace-nowrap transition-colors duration-200
                    ${sz.pad} text-[color:var(--badge-color)] bg-[var(--badge-dim)]
                    border border-[color:var(--badge-border)]`}
      >
        <span
          className={`${sz.dot} rounded-full flex-shrink-0 ${shouldPulse ? "animate-[dotPulse_1.8s_ease-in-out_infinite]" : ""}`}
          style={{ background: cfg.dotColor }}
        />

        {showLabel && <span>{size === "sm" ? cfg.shortLabel : cfg.label}</span>}

        {score !== null && (
          <span
            className="opacity-70 pl-[var(--gap,5px)] ml-px border-l"
            style={{ borderColor: cfg.color + "40" }}
          >
            {Math.round(score * 100)}%
          </span>
        )}

        {tooltip && hovered && (
          <div
            className="absolute bottom-[calc(100%+7px)] left-1/2 -translate-x-1/2 bg-[#111]
                       border rounded-md px-2.5 py-[7px] w-[210px] text-[11px] font-['DM_Mono',monospace]
                       text-[#b0a090] leading-[1.55] z-[100] pointer-events-none shadow-[0_4px_20px_#00000080]
                       animate-[tooltipIn_0.12s_ease-out] whitespace-normal"
            style={{ borderColor: cfg.color + "40" }}
          >
            {cfg.description}
            <div
              className="absolute -bottom-[5px] left-1/2 -translate-x-1/2 rotate-45 w-2 h-2 bg-[#111] border-t-0 border-l-0"
              style={{ borderColor: cfg.color + "40" }}
            />
          </div>
        )}
      </span>
    </>
  );
}