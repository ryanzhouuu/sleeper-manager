import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/home.json";
import { parseHomeDocument } from "@/lib/home";
import { LineupBlock, PlanBlock } from "./lineup-block";

vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element -- stands in for next/image
    <img src={src} alt={alt} />
  ),
}));

const document = parseHomeDocument(fixture);
const timeZone = document.league.timezone;

function rows(html: string): string[] {
  return html.split('<li class="lineup-row"').slice(1);
}

describe("LineupBlock", () => {
  it("renders lineup rows in slot order with game states", () => {
    const html = renderToStaticMarkup(
      <LineupBlock lineup={document.lineup} timeZone={timeZone} />,
    );
    const [brunson, tatum, bench] = rows(html);

    expect(brunson).toContain("PG");
    expect(brunson).toContain("Jalen Brunson");
    expect(brunson).toContain("vs TOR");
    expect(brunson).toContain("41.5");
    expect(brunson).toContain("lockable");
    expect(brunson).toContain("@ MIA");
    expect(brunson).toContain("upcoming");
    expect(tatum).toContain("UTIL");
    expect(bench).toContain("BN");
    expect(bench).toContain("unverified");
  });

  it("shows a sacrifice decision with before and after totals", () => {
    const tatum = rows(
      renderToStaticMarkup(
        <LineupBlock lineup={document.lineup} timeZone={timeZone} />,
      ),
    )[1];

    expect(tatum).toContain('data-decision="sacrifice"');
    expect(tatum).toContain("Bench for Cal — gives up 19.1 vs PHI");
    expect(tatum).toContain("300.0 → 312.5");
    expect(tatum).toContain("alt 27.4");
    expect(tatum).toContain("70%");
  });

  it("shows override buttons only on unverified games", () => {
    const html = renderToStaticMarkup(
      <LineupBlock lineup={document.lineup} timeZone={timeZone} />,
    );
    const [brunson, tatum, bench] = rows(html);

    expect(bench).toContain("Was starting at tipoff");
    expect(bench).toContain("Was benched at tipoff");
    expect(brunson).not.toContain("tipoff");
    expect(tatum).not.toContain("tipoff");
  });

  it("says when the lineup is empty", () => {
    expect(
      renderToStaticMarkup(<LineupBlock lineup={[]} timeZone={timeZone} />),
    ).toContain("no lineup yet.");
  });
});

describe("PlanBlock", () => {
  it("shows the team total before and after the plan with its range", () => {
    const html = renderToStaticMarkup(<PlanBlock plan={document.plan} />);

    expect(html).toContain("300.0 → 312.5");
    expect(html).toContain("medium confidence");
    expect(html).toContain("290.0–335.0");
  });

  it("omits the block without a plan total", () => {
    const html = renderToStaticMarkup(
      <PlanBlock plan={{ ...document.plan, team_after: null }} />,
    );

    expect(html).toBe("");
  });
});
