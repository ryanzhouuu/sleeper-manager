import { describe, expect, it } from "vitest";
import { adviceFor, recordedNote } from "./decision-display";

describe("decision display wording", () => {
  it("phrases advisor actions as Sleeper steps", () => {
    expect(adviceFor("Lock")).toEqual({
      tone: "lock",
      text: "Lock this game in Sleeper",
    });
    expect(adviceFor("Pass").tone).toBe("pass");
    expect(adviceFor("Wait").text).toBe("Hold for now");
    expect(adviceFor("Unavailable")).toEqual({
      tone: "none",
      text: "No advice right now",
    });
  });

  it("shows unknown advisor states verbatim without a tone", () => {
    expect(adviceFor("Check Sleeper")).toEqual({
      tone: "none",
      text: "Check Sleeper",
    });
  });

  it("hides the default recorded state that repeats the selection", () => {
    expect(recordedNote("Wait")).toBeNull();
    expect(recordedNote("Eligibility unverified")).toBe(
      "Eligibility unverified",
    );
  });
});
