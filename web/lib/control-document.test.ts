import { describe, expect, it } from "vitest";
import { parseControlDocument } from "./control-document";

describe("control documents", () => {
  it("reads the owner session and rejects an inconsistent proof", () => {
    expect(
      parseControlDocument({ status: "session", signed_in: true, csrf: "p" }),
    ).toEqual({ status: "session", signed_in: true, csrf: "p" });
    expect(
      parseControlDocument({ status: "session", signed_in: false, csrf: null }),
    ).toEqual({ status: "session", signed_in: false, csrf: null });
    expect(() =>
      parseControlDocument({ status: "session", signed_in: false, csrf: "p" }),
    ).toThrow();
  });

  it("rejects the retired decision editor document", () => {
    expect(() => parseControlDocument({ status: "ready", week: 3 })).toThrow();
  });

  it("rejects confirmation documents now that locks come from Sleeper", () => {
    expect(() =>
      parseControlDocument({
        status: "confirmation",
        heading: "Confirm",
        title: "Player",
        message: "Advice",
        instruction: "Confirm your action",
        label: "Confirm",
        token: "test-token",
        action: "locked",
      }),
    ).toThrow();
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
