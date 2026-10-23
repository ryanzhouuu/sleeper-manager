import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { loadControl } from "@/lib/control-backend";
import AckPage from "./page";

vi.mock("@/lib/control-backend", () => ({ loadControl: vi.fn() }));

describe("notification page", () => {
  beforeEach(() => {
    vi.mocked(loadControl).mockReset();
    vi.mocked(loadControl).mockResolvedValue({
      status: "message",
      title: "Confirmation expired",
    });
  });

  it("previews a valid token without an owner cookie or POST", async () => {
    const html = renderToStaticMarkup(
      await AckPage({
        searchParams: Promise.resolve({
          token: "test-token",
          action: "locked",
          ignored: "private",
        }),
      }),
    );
    expect(loadControl).toHaveBeenCalledWith(
      "/ack",
      "",
      "token=test-token&action=locked",
    );
    expect(html).toContain("Confirmation expired");
  });

  it.each([
    { token: ["one", "two"], action: "locked" },
    { token: "", action: "locked" },
    { token: "x".repeat(257), action: "locked" },
    { token: "test-token", action: "not_locked" },
    { token: "test-token", action: ["locked", "passed"] },
  ])(
    "rejects invalid or duplicated scope before contacting the Worker",
    async (search) => {
      const html = renderToStaticMarkup(
        await AckPage({ searchParams: Promise.resolve(search) }),
      );
      expect(html).toContain("Invalid confirmation");
      expect(loadControl).not.toHaveBeenCalled();
    },
  );
});
