import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/controls.json";
import { pollManager, updateManager } from "./control-client";

const fetchMock = vi.fn<typeof fetch>();

describe("owner submissions", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("submits once then refreshes latest evidence after a conflict", async () => {
    fetchMock
      .mockResolvedValueOnce(
        Response.json(
          { status: "message", title: "Decision changed" },
          { status: 409 },
        ),
      )
      .mockResolvedValueOnce(Response.json(fixture));
    const progress = vi.fn();
    const result = await updateManager(
      "save",
      new URLSearchParams({ csrf: "proof" }),
      progress,
    );
    expect(result.data).toEqual(fixture);
    expect(result.notice).toContain("weren’t saved");
    expect(progress).toHaveBeenCalledWith("Refreshing decisions…");
    expect(
      fetchMock.mock.calls.map(([url, options]) => [url, options?.method]),
    ).toEqual([
      ["/manage/api/save", "POST"],
      ["/manage/api", "GET"],
    ]);
  });

  it("refreshes after a successful save, including a pending plan", async () => {
    fetchMock
      .mockResolvedValueOnce(Response.json({ status: "saved" }))
      .mockResolvedValueOnce(
        Response.json({ ...fixture, pending: true, plan: null }),
      );
    const result = await updateManager("save", new URLSearchParams(), vi.fn());
    expect(result.data).toMatchObject({ pending: true, plan: null });
    expect(result.notice).toBe("Decisions saved.");
  });

  it("retains the current document on validation errors", async () => {
    fetchMock.mockResolvedValue(
      Response.json(
        {
          status: "message",
          title: "Decision not saved",
          message: "Game is no longer eligible",
        },
        { status: 400 },
      ),
    );
    const result = await updateManager("save", new URLSearchParams(), vi.fn());
    expect(result.data).toBeUndefined();
    expect(result.notice).toContain("Game is no longer eligible");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns to owner login when the session expires", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ status: "login", rejected: false }),
    );
    expect(
      (await updateManager("save", new URLSearchParams(), vi.fn())).data,
    ).toEqual({ status: "login", rejected: false });
  });

  it("never retries a write whose network result is unknown", async () => {
    fetchMock.mockRejectedValue(new TypeError("Offline"));
    await expect(
      updateManager("save", new URLSearchParams(), vi.fn()),
    ).rejects.toThrow("Offline");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("plan refresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("stops as soon as a current plan is available", async () => {
    fetchMock.mockResolvedValue(Response.json(fixture));
    const receive = vi.fn();
    const result = pollManager(new AbortController().signal, receive);
    await vi.advanceTimersByTimeAsync(3000);
    await expect(result).resolves.toBe("ready");
    expect(receive).toHaveBeenCalledWith(fixture);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("ends after a minute when planning remains pending", async () => {
    fetchMock.mockImplementation(async () =>
      Response.json({ ...fixture, pending: true, plan: null }),
    );
    const result = pollManager(new AbortController().signal, vi.fn());
    await vi.advanceTimersByTimeAsync(60_000);
    await expect(result).resolves.toBe("paused");
    expect(fetchMock).toHaveBeenCalledTimes(19);
  });

  it("cancels the next poll when the user begins editing", async () => {
    const controller = new AbortController();
    const result = pollManager(controller.signal, vi.fn());
    const rejected = expect(result).rejects.toMatchObject({
      name: "AbortError",
    });
    controller.abort();
    await rejected;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not replace the form with an unavailable response", async () => {
    fetchMock.mockResolvedValue(
      Response.json(
        { status: "message", title: "Unavailable" },
        { status: 502 },
      ),
    );
    const receive = vi.fn();
    const result = pollManager(new AbortController().signal, receive);
    await vi.advanceTimersByTimeAsync(3000);
    await expect(result).resolves.toBe("paused");
    expect(receive).not.toHaveBeenCalled();
  });
});
