import { describe, expect, it } from "vitest";
import { managerControlsUrl } from "./controls";

describe("managerControlsUrl", () => {
  it("links to owner controls without forwarding query parameters", () => {
    expect(managerControlsUrl("https://worker.test/home?x=private")).toBe(
      "https://worker.test/manage",
    );
  });

  it.each([
    undefined,
    "",
    "invalid",
    "javascript:alert(1)",
    "https://name:password@worker.test/home",
  ])("rejects unavailable or credential-bearing URLs: %s", (url) =>
    expect(managerControlsUrl(url)).toBeNull(),
  );
});
