// Python's repr() of JSON-native values, which is what the Python harness shows a model.

/** Python's repr() for JSON-native values: what the model sees as the script's result. */
export function pyRepr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "string") {
    const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
    const escaped = value.replace(
      /[\\\n\r\t]/g,
      (c) => ({ "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t" })[c]!,
    );
    return `${quote}${escaped.replaceAll(quote, `\\${quote}`)}${quote}`;
  }
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(", ")}]`;
  if (typeof value === "object")
    return `{${Object.entries(value)
      .map(([k, v]) => `${pyRepr(k)}: ${pyRepr(v)}`)
      .join(", ")}}`;
  return String(value);
}
