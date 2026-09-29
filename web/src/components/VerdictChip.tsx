import { Check, CircleAlert, CircleHelp, Minus, X, type LucideIcon } from "lucide-react";
import type { Verdict } from "../api";

export type Tone = "ok" | "bad" | "rust" | "neutral";

const META: Record<Verdict, { label: string; tone: Tone; Icon: LucideIcon }> = {
  PROVEN: { label: "Proven", tone: "ok", Icon: Check },
  REFUTED: { label: "Refuted", tone: "bad", Icon: X },
  REGRESSION: { label: "Regression", tone: "rust", Icon: CircleAlert },
  UNPROVEN: { label: "Unproven", tone: "neutral", Icon: CircleHelp },
  NO_CHECKABLE_CLAIM: { label: "No checkable claim", tone: "neutral", Icon: Minus },
};

export const verdictTone = (verdict: Verdict): Tone => (META[verdict] ?? META.UNPROVEN).tone;
export const verdictLabel = (verdict: Verdict): string => (META[verdict] ?? META.UNPROVEN).label;

/** Verdicts always carry a glyph and a word, never color alone. */
export function VerdictChip({ verdict, size = "md" }: { verdict: Verdict; size?: "md" | "lg" }) {
  const { label, tone, Icon } = META[verdict] ?? META.UNPROVEN;
  return (
    <span className={`chip chip--${tone}${size === "lg" ? " chip--lg" : ""}`}>
      <Icon aria-hidden="true" size={size === "lg" ? 16 : 13} strokeWidth={2.5} />
      {label}
    </span>
  );
}
