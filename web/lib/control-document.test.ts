import { describe, expect, it } from "vitest";
import fixture from "@/test/fixtures/controls.json";
import { parseControlDocument } from "./control-document";

describe("control documents", () => {
  it("keeps suggested Pass separate from the recorded Wait and editable version", () => {
    const data = parseControlDocument(fixture);
    expect(data.status).toBe("ready");
    if (data.status !== "ready") throw new Error("Missing editor");
    expect(data.active[0]).toMatchObject({
      action: "not_locked",
      advisor: "Pass",
      saved: "Wait",
      version: 0,
    });
  });

  it.each([
    { week: 0 },
    { timezone: "bad/timezone" },
    { csrf: undefined },
    { pending: "false" },
    { active: [{ ...fixture.active[0], version: -1 }] },
    { active: [{ ...fixture.active[0], score: Number.NaN }] },
    { active: [{ ...fixture.active[0], deadline: "invalid" }] },
    { active: [{ ...fixture.active[0], action: "lock" }] },
    {
      active: [
        {
          ...fixture.active[0],
          portrait_url: "https://untrusted.test/photo.png",
        },
      ],
    },
    {
      active: [
        {
          ...fixture.active[0],
          portrait_url: "https://user:pass@a.espncdn.com/photo.png",
        },
      ],
    },
  ])("rejects invalid private editor data: %j", (changes) => {
    expect(() => parseControlDocument({ ...fixture, ...changes })).toThrow();
  });

  it("accepts only explicit lock/pass confirmations and bounded tokens", () => {
    const preview = {
      status: "confirmation",
      heading: "Confirm",
      title: "Player",
      message: "Advice",
      instruction: "Confirm your action",
      label: "Confirm",
      token: "test-token",
      action: "locked",
    };
    expect(parseControlDocument(preview)).toEqual(preview);
    for (const changes of [
      { action: "not_locked" },
      { token: "" },
      { token: "x".repeat(257) },
    ]) {
      expect(() => parseControlDocument({ ...preview, ...changes })).toThrow();
    }
  });

  it("rejects unknown responses and HTML instead of assuming success", () => {
    for (const value of ["<html>Login</html>", null, {}, { status: "ok" }])
      expect(() => parseControlDocument(value)).toThrow();
    expect(parseControlDocument({ status: "login", rejected: false })).toEqual({
      status: "login",
      rejected: false,
    });
  });
});
