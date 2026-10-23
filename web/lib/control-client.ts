/** Browser requests stay on the app origin; conflicts refresh evidence without replaying writes. */
import { parseControlDocument } from "./control-document";
import type { ControlDocument, ControlOperation } from "./control-types";

export interface ControlResult {
  code: number;
  data: ControlDocument;
}

/** URL-encoded forms preserve the Worker's duplicate-field and size validation. */
export function encodeControlForm(form: HTMLFormElement): URLSearchParams {
  const body = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (typeof value === "string") body.append(key, value);
  }
  return body;
}

/** Network and schema failures throw so a failed read cannot replace unsaved choices. */
export async function requestControl(
  path: string,
  body?: URLSearchParams,
  signal?: AbortSignal,
): Promise<ControlResult> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    body,
    cache: "no-store",
    credentials: "same-origin",
    signal: signal ?? AbortSignal.timeout(25_000),
  });
  return {
    code: response.status,
    data: parseControlDocument(await response.json()),
  };
}

/** An explicit submission is issued once; only the following read may refresh the editor. */
export async function updateManager(
  operation: ControlOperation,
  body: URLSearchParams,
  progress: (label: string) => void,
): Promise<{ data?: ControlDocument; notice: string }> {
  const result = await requestControl(`/manage/api/${operation}`, body);
  if (result.data.status === "saved" || result.code === 409) {
    progress("Refreshing decisions…");
    const fresh = await requestControl("/manage/api");
    return {
      data: fresh.data,
      notice:
        result.code === 409
          ? "Your changes weren’t saved because the decisions changed. Review the latest status and save again."
          : operation === "save"
            ? "Decisions saved."
            : operation === "replan"
              ? "Plan update requested."
              : "",
    };
  }
  if (result.data.status === "login")
    return {
      data: result.data,
      notice: result.data.rejected
        ? "Access key not accepted."
        : "Sign in to continue.",
    };
  return {
    notice:
      result.data.status === "message"
        ? [result.data.title, result.data.message].filter(Boolean).join(". ")
        : "Could not save. Try again.",
  };
}

/** Poll for at most a minute; aborting prevents a background response from replacing edited forms. */
export async function pollManager(
  signal: AbortSignal,
  receive: (data: ControlDocument) => void,
): Promise<"ready" | "paused"> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    await new Promise<void>((resolve, reject) => {
      const aborted = () => {
        clearTimeout(timer);
        signal.removeEventListener("abort", aborted);
        reject(new DOMException("Aborted", "AbortError"));
      };
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", aborted);
        resolve();
      }, 3000);
      signal.addEventListener("abort", aborted, { once: true });
      if (signal.aborted) aborted();
    });
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    const result = await requestControl(
      "/manage/api",
      undefined,
      AbortSignal.any([signal, AbortSignal.timeout(remaining)]),
    );
    if (signal.aborted) return "paused";
    if (result.code >= 500 || result.data.status === "message") return "paused";
    receive(result.data);
    if (result.data.status !== "ready" || !result.data.pending) return "ready";
  }
  return "paused";
}
