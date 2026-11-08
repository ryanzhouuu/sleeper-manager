import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { submitOverride } from "@/lib/control-client";
import { OverrideControl } from "./override-control";

afterEach(() => vi.unstubAllGlobals());

const GAME = { playerId: "p3", gameId: "g4", version: 3 };

describe("OverrideControl", () => {
  it("offers both tipoff answers", () => {
    const html = renderToStaticMarkup(<OverrideControl {...GAME} />);

    expect(html).toContain("Was starting at tipoff");
    expect(html).toContain("Was benched at tipoff");
    expect(html).not.toContain('type="password"');
  });
});

describe("submitOverride", () => {
  it("asks for the access key when signed out", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({ status: "session", signed_in: false, csrf: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    expect(await submitOverride({ ...GAME, action: "lockable" })).toEqual({
      status: "login",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("posts the answer with the session proof", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({ status: "session", signed_in: true, csrf: "proof" }),
      )
      .mockResolvedValueOnce(Response.json({ status: "saved" }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await submitOverride({ ...GAME, action: "not_lockable" })).toEqual({
      status: "saved",
    });
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/manage/api/override");
    expect(Object.fromEntries(init.body as URLSearchParams)).toEqual({
      csrf: "proof",
      player_id: "p3",
      game_id: "g4",
      version: "3",
      action: "not_lockable",
    });
  });

  it("logs in before retrying when given the access key", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ status: "saved" }))
      .mockResolvedValueOnce(
        Response.json({ status: "session", signed_in: true, csrf: "proof" }),
      )
      .mockResolvedValueOnce(Response.json({ status: "saved" }));
    vi.stubGlobal("fetch", fetchMock);

    expect(
      await submitOverride({ ...GAME, action: "lockable" }, "secret"),
    ).toEqual({ status: "saved" });
    expect(fetchMock.mock.calls[0][0]).toBe("/manage/api/login");
  });

  it("reports a rejected answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          Response.json({ status: "session", signed_in: true, csrf: "proof" }),
        )
        .mockResolvedValueOnce(
          Response.json(
            { status: "message", title: "Game changed" },
            { status: 409 },
          ),
        ),
    );

    expect(await submitOverride({ ...GAME, action: "lockable" })).toEqual({
      status: "error",
      message: "Game changed",
    });
  });
});
