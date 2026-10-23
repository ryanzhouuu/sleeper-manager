import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/controls.json";
import { POST as ownerPost } from "@/app/manage/api/[operation]/route";
import { forwardControl, loadControl } from "./control-backend";

const fetchMock = vi.fn<typeof fetch>();

function post(
  origin = "https://app.test",
  body: BodyInit = "csrf=test&action=locked",
) {
  return new Request("https://app.test/manage/api/save", {
    method: "POST",
    body,
    headers: {
      origin,
      "content-type": "application/x-www-form-urlencoded",
      cookie: "unrelated=private; manager_session=123.abc.def",
    },
  });
}

describe("server-to-Worker controls", () => {
  beforeEach(() => {
    vi.stubEnv("HOME_URL", "https://worker.test/home");
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("loads uncached private documents with only the owner cookie", async () => {
    fetchMock.mockResolvedValue(Response.json(fixture));
    expect(
      await loadControl(
        "/manage",
        "analytics=private; manager_session=123.abc.def",
      ),
    ).toEqual(fixture);
    const [url, options] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("https://worker.test/manage");
    expect(options).toMatchObject({
      cache: "no-store",
      redirect: "manual",
      method: "GET",
    });
    const headers = new Headers(options?.headers);
    expect(headers.get("cookie")).toBe("manager_session=123.abc.def");
    expect(headers.get("accept")).toBe("application/json");
    expect(options?.body).toBeUndefined();
  });

  it.each(["https://evil.test", "null", ""])(
    "rejects an untrusted browser origin before fetching: %s",
    async (origin) => {
      expect((await forwardControl(post(origin), "/manage/save")).status).toBe(
        403,
      );
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("forwards same-origin forms and installs the Worker cookie on the app origin", async () => {
    const cookie =
      "manager_session=123.abc.def; Path=/manage; HttpOnly; Secure; SameSite=Strict";
    fetchMock.mockResolvedValue(
      new Response(null, {
        status: 303,
        headers: { location: "/manage", "set-cookie": cookie },
      }),
    );
    const response = await forwardControl(post(), "/manage/login");
    expect(await response.json()).toEqual({ status: "saved" });
    expect(response.headers.get("set-cookie")).toBe(cookie);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const [url, options] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("https://worker.test/manage/login");
    expect(new Headers(options?.headers).get("origin")).toBe(
      "https://worker.test",
    );
    expect(options?.body).toBe("csrf=test&action=locked");
  });

  it("validates the public host when Next.js constructs an internal request URL", async () => {
    fetchMock.mockResolvedValue(Response.json({ status: "saved" }));
    const request = new Request("http://internal:3000/manage/api/save", {
      method: "POST",
      body: "csrf=proof",
      headers: {
        host: "app.test",
        "x-forwarded-host": "app.test",
        "x-forwarded-proto": "https",
        origin: "https://app.test",
        "content-type": "application/x-www-form-urlencoded",
      },
    });
    expect((await forwardControl(request, "/manage/save")).status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("still rejects a different host behind a reverse proxy", async () => {
    const request = post("https://evil.test");
    request.headers.set("x-forwarded-host", "app.test");
    request.headers.set("x-forwarded-proto", "https");
    expect((await forwardControl(request, "/manage/save")).status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves stale save status for the UI without retrying the write", async () => {
    fetchMock.mockResolvedValue(
      Response.json(
        { status: "message", title: "Decision changed" },
        { status: 409 },
      ),
    );
    const response = await forwardControl(post(), "/manage/save");
    expect(response.status).toBe(409);
    expect((await response.json()).title).toBe("Decision changed");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["x".repeat(4097), new Uint8Array([255])])(
    "rejects oversized or invalid UTF-8 form streams",
    async (body) => {
      expect(
        (await forwardControl(post("https://app.test", body), "/manage/save"))
          .status,
      ).toBe(400);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("never follows unexpected upstream redirects or returns upstream HTML", async () => {
    fetchMock.mockResolvedValue(
      new Response(null, {
        status: 303,
        headers: { location: "https://evil.test" },
      }),
    );
    expect((await forwardControl(post(), "/manage/save")).status).toBe(502);
    fetchMock.mockResolvedValue(new Response("<html>Login</html>"));
    expect((await forwardControl(post(), "/manage/save")).status).toBe(502);
    expect((await loadControl("/manage")).status).toBe("message");
  });

  it("uses GET for confirmation previews and sends only their scoped query", async () => {
    fetchMock.mockResolvedValue(
      Response.json(
        { status: "message", title: "Confirmation expired" },
        { status: 410 },
      ),
    );
    expect(
      await loadControl("/ack", "", "token=test-token&action=locked"),
    ).toEqual({ status: "message", title: "Confirmation expired" });
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "https://worker.test/ack?token=test-token&action=locked",
    );
    expect(fetchMock.mock.calls[0][1]?.method).toBe("GET");
  });

  it("rejects unknown owner endpoints and unavailable configuration", async () => {
    expect(
      (
        await ownerPost(post(), {
          params: Promise.resolve({ operation: "delete" }),
        })
      ).status,
    ).toBe(404);
    vi.stubEnv("HOME_URL", "https://user:password@worker.test/home");
    expect((await loadControl("/manage")).status).toBe("message");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
