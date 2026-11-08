/** Validate Worker owner documents before using their CSRF proofs. */
import type { ControlDocument } from "./control-types";

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid document");
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid text");
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Invalid flag");
  return value;
}

export function parseControlDocument(value: unknown): ControlDocument {
  const row = record(value);
  switch (row.status) {
    case "saved":
      return { status: "saved" };
    case "login":
      return { status: "login", rejected: boolean(row.rejected) };
    case "message":
      return {
        status: "message",
        title: text(row.title),
        ...(row.message === undefined ? {} : { message: text(row.message) }),
      };
    case "session": {
      const signedIn = boolean(row.signed_in);
      const csrf = row.csrf === null ? null : text(row.csrf);
      if (signedIn !== (csrf !== null)) throw new Error("Invalid session");
      return { status: "session", signed_in: signedIn, csrf };
    }
    default:
      throw new Error("Unknown document");
  }
}
