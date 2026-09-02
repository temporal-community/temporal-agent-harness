import type { Component } from "svelte";

export interface Metric {
  label: string;
  value: string;
  tone?: "neutral" | "strong" | "cost";
  /** Lucide icon, for strips that identify a kind rather than a total. */
  icon?: Component<{ size?: number | string }>;
  /** CSS variable name that colors the icon, e.g. "--model". */
  hue?: string;
  /** Hover text, for a value that needs explaining (an uncomputable cost). */
  note?: string;
}
