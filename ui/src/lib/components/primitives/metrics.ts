export interface Metric {
  label: string;
  value: string;
  tone?: "neutral" | "strong" | "cost";
}
