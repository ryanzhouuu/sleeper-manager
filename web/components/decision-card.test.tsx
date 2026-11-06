import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/controls.json";
import { parseControlDocument } from "@/lib/control-document";
import type { ManagerDocument } from "@/lib/control-types";
import { DecisionCard } from "./decision-card";

vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element -- replaces the image optimizer in markup tests
    <img src={src} alt={alt} />
  ),
}));

const game = (parseControlDocument(fixture) as ManagerDocument).active[0];

describe("decision cards", () => {
  it("keeps the observed score read-only and the optimistic version in the batch", () => {
    const html = renderToStaticMarkup(
      <DecisionCard game={game} index={2} timezone="America/Chicago" />,
    );
    expect(html).toContain("32.80");
    expect(html).not.toContain('name="score');
    expect(html).toContain('name="version_2" value="0"');
    expect(html).toContain('aria-label="Advisor: Pass"');
    expect(html).not.toContain("Recorded");
    expect(html).toContain('name="action_2" checked="" value="not_locked"');
  });

  it("separates the advisor's suggestion from the owner's Sleeper record", () => {
    const html = renderToStaticMarkup(
      <DecisionCard
        game={{ ...game, advisor: "Lock", saved: "Eligibility unverified" }}
        index={0}
        timezone="America/Chicago"
      />,
    );
    expect(html).toContain("decision-advice--lock");
    expect(html).toContain("Lock this game in Sleeper");
    expect(html).toContain("What you did in Sleeper");
    expect(html).toContain("Recorded · Eligibility unverified");
    expect(html).toContain("Not yet");
  });

  it("offers the actions that settle an uncertain game", () => {
    const html = renderToStaticMarkup(
      <DecisionCard
        game={{ ...game, choices: ["not_locked", "lockable"] }}
        index={0}
        timezone="America/Chicago"
      />,
    );
    expect(html).toContain('value="lockable"');
    expect(html).toContain("Sleeper shows a lock");
    expect(html).not.toContain('value="locked"');
  });

  it("escapes provider names in headings and accessible descriptions", () => {
    const html = renderToStaticMarkup(
      <DecisionCard
        game={{ ...game, name: '<script>alert("name")</script>' }}
        index={0}
        timezone="UTC"
      />,
    );
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain('aria-label="&lt;script&gt;');
  });
});
