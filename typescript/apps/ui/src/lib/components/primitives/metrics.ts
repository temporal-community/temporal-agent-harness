import type { Component } from "svelte";

export interface Metric {
  label: string;
  value: string;
  /**
   * `pending` is a figure still on its way, drawn muted so it never reads as a measurement.
   * `quiet` is a real measurement too small to compete with its peers.
   */
  tone?: "neutral" | "strong" | "cost" | "pending" | "quiet";
  /** Lucide icon, for strips that identify a kind rather than a total. */
  icon?: Component<{ size?: number | string }>;
  /** CSS variable name that colors the icon, e.g. "--model". */
  hue?: string;
  /** A CSS color drawn as a square key, when the figure names a series a chart draws in it. */
  swatch?: string;
  /** Hover text, for a value that needs explaining (an uncomputable cost). */
  note?: string;
}
