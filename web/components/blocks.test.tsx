import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/home.json";
import { parseHomeDocument } from "@/lib/home";
import {
  GamesBlock,
  RecommendationBlock,
  RosterBlock,
  WeekBlock,
} from "./blocks";

vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element -- stands in for next/image
    <img src={src} alt={alt} />
  ),
}));

const document = parseHomeDocument(fixture);

describe("WeekBlock", () => {
  it("shows the league week and both sides with scores", () => {
    const html = renderToStaticMarkup(
      <WeekBlock league={document.league} matchup={document.matchup} />,
    );

    expect(html).toContain("Lock In · week 3");
    expect(html).toContain("142.6");
    expect(html).toContain("Away Team");
    expect(html).toContain("2-1<");
    expect(html).toContain("812.4 pf");
    expect(html).toContain("1-1-1");
  });

  it("omits the opponent on a bye and a missing score", () => {
    const html = renderToStaticMarkup(
      <WeekBlock
        league={document.league}
        matchup={{
          manager: { ...document.matchup.manager, score: null },
          opponent: null,
        }}
      />,
    );

    expect(html).not.toContain("Away Team");
    expect(html).not.toContain("142.6");
    expect(html.match(/<li[ >]/g)).toHaveLength(1);
  });
});

describe("RecommendationBlock", () => {
  it("marks an action-required plan", () => {
    const html = renderToStaticMarkup(
      <RecommendationBlock recommendation={document.recommendation} />,
    );

    expect(html).toContain('class="heading urgent"');
    expect(html).toContain("! Lineup move required");
    expect(html).toContain("Start Jayson Tatum in UTIL.\n\nExpected");
  });

  it.each(["no_action", "degraded", "blocked", null] as const)(
    "leaves plan status %s unmarked",
    (planStatus) => {
      const html = renderToStaticMarkup(
        <RecommendationBlock
          recommendation={{
            status: "ready",
            plan_status: planStatus,
            title: "Lineup move required",
            message: "Body",
          }}
        />,
      );

      expect(html).toContain('class="heading"');
      expect(html).not.toContain("!");
    },
  );

  it.each([
    ["empty", "no lineup recommendation yet."],
    ["unavailable", "recommendation could not be loaded."],
  ] as const)("shows only the %s sentence", (status, sentence) => {
    const html = renderToStaticMarkup(
      <RecommendationBlock recommendation={{ status }} />,
    );

    expect(html).toBe(`<section class="block"><p>${sentence}</p></section>`);
  });
});

describe("RosterBlock", () => {
  it("renders dashes for an empty slot and keeps image cells", () => {
    const html = renderToStaticMarkup(<RosterBlock roster={document.roster} />);
    const emptySlot = html.split(/<li[ >]/)[2];

    expect(emptySlot).toContain("UTIL");
    expect(emptySlot.match(/—/g)).toHaveLength(3);
    expect(emptySlot).not.toContain("<img");
    expect(emptySlot.match(/class="cell"/g)).toHaveLength(2);
    expect(html).toContain('alt="Jalen Brunson"');
  });
});

describe("RosterBlock positions and injuries", () => {
  it("shows positions, injury status, and sizes the positions column", () => {
    const html = renderToStaticMarkup(<RosterBlock roster={document.roster} />);
    const tatum = html.split(/<li[ >]/)[3];

    expect(tatum).toContain("SF/PF");
    expect(tatum).toContain(
      '<span class="status" data-injury-status="DTD"> · DTD</span>',
    );
    expect(html).toContain("--positions-width:5ch");
    expect(html.split(/<li[ >]/)[1]).not.toContain('class="status"');
  });
});

describe("GamesBlock", () => {
  it("groups by local day in start order", () => {
    const html = renderToStaticMarkup(
      <GamesBlock games={document.games} timeZone={document.league.timezone} />,
    );

    expect(html.indexOf("Wed Oct 21")).toBeLessThan(html.indexOf("Thu Oct 22"));
    expect(html.indexOf("@ BOS")).toBeLessThan(html.indexOf("vs LAL"));
    expect(html).toContain("11:30 PM");
    expect(html.match(/Jalen Brunson<\/span>/g)).toHaveLength(2);
    expect(html).toContain(
      'Jayson Tatum</span><span class="status" data-injury-status="DTD"> · DTD',
    );
  });

  it.each([
    ["empty", "no games this week."],
    ["unavailable", "games could not be loaded."],
  ] as const)("shows only the %s sentence", (status, sentence) => {
    const html = renderToStaticMarkup(
      <GamesBlock games={{ status, items: [] }} timeZone="America/Chicago" />,
    );

    expect(html).toContain(sentence);
    expect(html).not.toMatch(/<li[ >]/);
  });
});
