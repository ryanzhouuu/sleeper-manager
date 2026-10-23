import { describe, expect, it } from "vitest";
import { managerControlsUrl } from "./controls";

describe("managerControlsUrl", () => {
  it("keeps owner controls on the app origin without forwarding query parameters", () => {
    expect(managerControlsUrl("https://worker.test/home?x=private")).toBe(
      "/manage",
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
