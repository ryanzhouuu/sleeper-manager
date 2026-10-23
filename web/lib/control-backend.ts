/** Server pages and fixed route handlers are the only callers of the private Worker. */
import { parseControlDocument } from "./control-document";
import type { ControlDocument } from "./control-types";

type WorkerPath =
  | "/manage"
  | "/manage/login"
  | "/manage/save"
  | "/manage/replan"
  | "/manage/logout"
  | "/ack";
const RESPONSE_HEADERS = {
  "cache-control": "no-store",
  "referrer-policy": "same-origin",
};
export const UNAVAILABLE: ControlDocument = {
  status: "message",
  title: "Controls unavailable",
  message: "Could not reach your league. Try again shortly.",
};

/** Derive a fixed origin from HOME_URL without forwarding arbitrary browser destinations. */
function backendUrl(path: WorkerPath, query = ""): URL {
  const home = new URL(process.env.HOME_URL ?? "");
  if (
    !["http:", "https:"].includes(home.protocol) ||
    home.username ||
    home.password
  )
    throw new Error("Invalid backend");
  const url = new URL(path, home.origin);
  url.search = query;
  return url;
}

/** The browser's unrelated cookies are never sent to the Worker. */
function sessionCookie(cookie: string): string {
  return (
    cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => /^manager_session=[a-zA-Z0-9.]+$/.test(part)) ?? ""
  );
}

/** Keep upstream redirects manual so a login cookie cannot follow another host. */
async function fetchControl(
  path: WorkerPath,
  options: { cookie?: string; query?: string; body?: string } = {},
): Promise<Response> {
  const url = backendUrl(path, options.query);
  const headers = new Headers({ accept: "application/json" });
  const cookie = sessionCookie(options.cookie ?? "");
  if (cookie) headers.set("cookie", cookie);
  if (options.body !== undefined) {
    headers.set("content-type", "application/x-www-form-urlencoded");
    headers.set("origin", url.origin);
  }
  return fetch(url, {
    method: options.body === undefined ? "GET" : "POST",
    headers,
    body: options.body,
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(20_000),
  });
}

/** Server-rendered GETs cannot consume a confirmation or mutate owner decisions. */
export async function loadControl(
  path: "/manage" | "/ack",
  cookie = "",
  query = "",
): Promise<ControlDocument> {
  try {
    const response = await fetchControl(path, { cookie, query });
    return parseControlDocument(await response.json());
  } catch {
    return UNAVAILABLE;
  }
}

/** Bound the actual stream as well as Content-Length before forwarding forms. */
async function formBody(request: Request): Promise<string> {
  if (
    request.headers.get("content-type")?.split(";")[0] !==
    "application/x-www-form-urlencoded"
  )
    throw new Error("Invalid form");
  const reader = request.body?.getReader();
  if (!reader) throw new Error("Invalid form");
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let size = 0;
  let body = "";
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) return body + decoder.decode();
      size += part.value.byteLength;
      if (size > 4096) {
        await reader.cancel();
        throw new Error("Form too large");
      }
      body += decoder.decode(part.value, { stream: true });
    }
  } finally {
    reader.releaseLock();
  }
}

/** Validate the Vercel browser origin before translating it to the Worker's origin. */
export async function forwardControl(
  request: Request,
  path: WorkerPath,
): Promise<Response> {
  const post = request.method === "POST";
  if (!post && (request.method !== "GET" || path !== "/manage"))
    return Response.json(
      { status: "message", title: "Method not allowed" },
      { status: 405, headers: RESPONSE_HEADERS },
    );
  if (post && request.headers.get("origin") !== new URL(request.url).origin)
    return Response.json(
      { status: "message", title: "Request rejected" },
      { status: 403, headers: RESPONSE_HEADERS },
    );
  let body: string | undefined;
  try {
    if (post) body = await formBody(request);
  } catch {
    return Response.json(
      { status: "message", title: "Invalid form" },
      { status: 400, headers: RESPONSE_HEADERS },
    );
  }
  try {
    const upstream = await fetchControl(path, {
      cookie: request.headers.get("cookie") ?? "",
      body,
    });
    const headers = new Headers(RESPONSE_HEADERS);
    const cookie = upstream.headers.get("set-cookie");
    if (cookie?.startsWith("manager_session="))
      headers.set("set-cookie", cookie);
    if (
      upstream.status === 303 &&
      upstream.headers.get("location") === "/manage"
    )
      return Response.json({ status: "saved" }, { headers });
    const data = parseControlDocument(await upstream.json());
    return Response.json(data, { status: upstream.status, headers });
  } catch {
    return Response.json(UNAVAILABLE, {
      status: 502,
      headers: RESPONSE_HEADERS,
    });
  }
}
