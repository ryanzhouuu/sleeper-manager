import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/home.json";
import { parseHomeDocument } from "@/lib/home";
import { GamesBlock, RecommendationBlock, WeekBlock } from "./blocks";

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

describe("WeekBlock lock progress", () => {
  const manager = document.matchup.manager;
  const render = (side: typeof manager) =>
    renderToStaticMarkup(
      <WeekBlock
        league={document.league}
        matchup={{ manager: side, opponent: null }}
      />,
    );

  it("shows the live score with the locked count", () => {
    const html = render({ ...manager, score: 58, locked: 1, starters: 9 });

    expect(html).toContain("58.0");
    expect(html).toContain("1/9 locked");
  });

  it("marks a final score", () => {
    const html = render({ ...manager, score: 383, score_final: true });

    expect(html).toContain("383.0");
    expect(html).toContain("final");
    expect(html).not.toContain("locked");
  });

  it("says when lock data is unavailable", () => {
    const html = render({
      ...manager,
      score: null,
      locked: null,
      lock_data: "unavailable",
    });

    expect(html).toContain("lock data unavailable");
    expect(html).not.toContain("142.6");
  });

  it("says when locked stats are pending", () => {
    const html = render({ ...manager, score: null, lock_data: "pending" });

    expect(html).toContain("4/9 locked · stats pending");
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
