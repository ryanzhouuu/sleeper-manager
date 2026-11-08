/** Browser requests stay on the app origin and send only the owner's tipoff answer. */
import { parseControlDocument } from "./control-document";
import type { ControlDocument, OverrideAction } from "./control-types";

export interface OverrideRequest {
  playerId: string;
  gameId: string;
  version: number;
  action: OverrideAction;
}

export type OverrideResult =
  | { status: "saved" }
  | { status: "login"; rejected?: boolean }
  | { status: "error"; message: string };

async function requestControl(
  path: string,
  body?: URLSearchParams,
): Promise<ControlDocument> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    body,
    cache: "no-store",
    credentials: "same-origin",
    signal: AbortSignal.timeout(25_000),
  });
  return parseControlDocument(await response.json());
}

function failure(document: ControlDocument): OverrideResult {
  return {
    status: "error",
    message:
      document.status === "message"
        ? [document.title, document.message].filter(Boolean).join(". ")
        : "Could not save. Try again.",
  };
}

/** Sign in first when given the access key; a missing session asks for it instead. */
export async function submitOverride(
  request: OverrideRequest,
  password?: string,
): Promise<OverrideResult> {
  if (password !== undefined) {
    const login = await requestControl(
      "/manage/api/login",
      new URLSearchParams({ password }),
    );
    if (login.status === "login") return { status: "login", rejected: true };
    if (login.status !== "saved") return failure(login);
  }
  const session = await requestControl("/manage/api");
  if (session.status !== "session") return failure(session);
  if (!session.signed_in || session.csrf === null) return { status: "login" };
  const result = await requestControl(
    "/manage/api/override",
    new URLSearchParams({
      csrf: session.csrf,
      player_id: request.playerId,
      game_id: request.gameId,
      version: String(request.version),
      action: request.action,
    }),
  );
  return result.status === "saved" ? { status: "saved" } : failure(result);
}
