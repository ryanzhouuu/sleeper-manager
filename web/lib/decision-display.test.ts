import { describe, expect, it } from "vitest";
import fixture from "@/test/fixtures/controls.json";
import { parseControlDocument } from "./control-document";
import type { ManagerDocument } from "./control-types";
import {
  adviceFor,
  countdown,
  dayKey,
  groupDecisionsByDay,
  recordedNote,
} from "./decision-display";

const game = (parseControlDocument(fixture) as ManagerDocument).active[0];

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

describe("lock window countdown", () => {
  const deadline = "2026-11-07T00:30:00+00:00";
  const at = (iso: string) => new Date(iso).getTime();

  it("flags the final hour as soon and rounds partial minutes up", () => {
    expect(countdown(deadline, at("2026-11-06T23:45:30+00:00"))).toEqual({
      urgency: "soon",
      text: "in 45m",
    });
  });

  it("uses hours and days for later windows", () => {
    expect(countdown(deadline, at("2026-11-06T22:00:00+00:00"))).toEqual({
      urgency: "later",
      text: "in 2h 30m",
    });
    expect(countdown(deadline, at("2026-11-05T21:00:00+00:00")).text).toBe(
      "in 1d 3h",
    );
  });

  it("reports a closed window at or after the deadline", () => {
    expect(countdown(deadline, at(deadline)).urgency).toBe("closed");
  });
});

describe("decision days", () => {
  it("groups by local tip-off day in deadline order and keeps form indexes", () => {
    const late = {
      ...game,
      game_id: "late",
      start: "2026-11-08T02:00:00+00:00",
      deadline: "2026-11-08T02:00:00+00:00",
    };
    const early = {
      ...game,
      game_id: "early",
      start: "2026-11-07T01:00:00+00:00",
      deadline: "2026-11-07T01:00:00+00:00",
    };
    const days = groupDecisionsByDay([late, early], "America/Chicago");
    expect(days.map((day) => day.label)).toEqual(["Fri, Nov 6", "Sat, Nov 7"]);
    expect(days[0].games).toEqual([{ game: early, index: 1 }]);
    expect(days[1].games).toEqual([{ game: late, index: 0 }]);
  });

  it("keys today in the league timezone rather than UTC", () => {
    expect(
      dayKey(new Date("2026-11-07T03:00:00Z").getTime(), "America/Chicago"),
    ).toBe("2026-11-06");
  });
});
