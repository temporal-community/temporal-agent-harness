import type { Session } from "$lib/api/types";

export function sessionTitle(session: Session): string {
  return session.initial_user_message?.trim().replace(/\s+/g, " ") || session.label?.trim() || "Untitled session";
}

const dayFormat = new Intl.DateTimeFormat([], { month: "short", day: "numeric", year: "numeric" });

export function sessionDay(timestamp: number, now = new Date()): string {
  if (!timestamp) return "Unknown date";
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "Unknown date";
  if (date.toDateString() === now.toDateString()) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return dayFormat.format(date);
}
