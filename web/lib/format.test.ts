import { describe, expect, it } from "vitest";
import {
  formatLocalTime,
  formatScore,
  groupGamesByDay,
  opponentLabel,
} from "./format";
import type { GameItem } from "./home";

function game(start: string, overrides: Partial<GameItem> = {}): GameItem {
  return {
    start: new Date(start),
    player_name: "Jalen Brunson",
    player_image_url: null,
    opponent: "BOS",
    opponent_image_url: null,
    home: false,
    ...overrides,
  };
}

describe("groupGamesByDay", () => {
  it("groups by the league's calendar day and orders by start", () => {
    const late = game("2026-10-22T04:30:00Z", { player_name: "Late" });
    const early = game("2026-10-21T23:00:00Z", { player_name: "Early" });
    const next = game("2026-10-23T00:00:00Z", { player_name: "Next" });

    const days = groupGamesByDay([next, late, early], "America/Chicago");

    expect(days.map((day) => day.label)).toEqual(["Wed Oct 21", "Thu Oct 22"]);
    expect(days[0].games.map((g) => g.player_name)).toEqual(["Early", "Late"]);
    expect(days[1].games.map((g) => g.player_name)).toEqual(["Next"]);
  });

  it("uses the given timezone for day boundaries", () => {
    const days = groupGamesByDay([game("2026-10-22T04:30:00Z")], "UTC");

    expect(days[0].label).toBe("Thu Oct 22");
  });
});

describe("formatLocalTime", () => {
  it("formats in the league timezone", () => {
    expect(
      formatLocalTime(new Date("2026-10-21T23:00:00Z"), "America/Chicago"),
    ).toBe("6:00 PM");
  });
});

describe("opponentLabel", () => {
  it.each([
    [false, "BOS", "@ BOS"],
    [true, "BOS", "vs BOS"],
    [false, null, "@ —"],
  ])("home=%s opponent=%s reads %s", (home, opponent, expected) => {
    expect(opponentLabel({ home, opponent })).toBe(expected);
  });
});

describe("formatScore", () => {
  it("shows one decimal", () => {
    expect(formatScore(142.6)).toBe("142.6");
    expect(formatScore(9)).toBe("9.0");
  });
});
