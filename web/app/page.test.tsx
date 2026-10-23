import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "@/test/fixtures/home.json";
import { parseHomeDocument, type HomeResult } from "@/lib/home";
import HomePage from "./page";

const loadHome = vi.hoisted(() => vi.fn<() => Promise<HomeResult>>());

vi.mock("@/lib/home", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/home")>()),
  loadHome,
}));

vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element -- stands in for next/image
    <img src={src} alt={alt} />
  ),
}));

async function render(): Promise<string> {
  return renderToStaticMarkup(await HomePage());
}

describe("HomePage", () => {
  beforeEach(() => loadHome.mockReset());

  it("puts the schedule in its own column after the other blocks", async () => {
    loadHome.mockResolvedValue({
      status: "ok",
      document: parseHomeDocument(fixture),
    });

    const columns = (await render()).split('<div class="column">');

    expect(columns[0]).toContain('<main class="page split">');
    expect(columns).toHaveLength(3);
    expect(columns[1]).toContain("week 3");
    expect(columns[1]).toContain("roster");
    expect(columns[1]).not.toContain("this week");
    expect(columns[2]).toContain("this week");
  });

  it("keeps the unavailable page to one column", async () => {
    loadHome.mockResolvedValue({ status: "unavailable" });

    const html = await render();

    expect(html).toContain('<main class="page">');
    expect(html).not.toContain("column");
    expect(html).toContain("league could not be loaded.");
  });

  it("links to owner controls on the same app origin", async () => {
    vi.stubEnv("HOME_URL", "https://worker.test/home");
    try {
      loadHome.mockResolvedValue({
        status: "ok",
        document: parseHomeDocument(fixture),
      });
      const html = await render();
      expect(html).toContain('href="/manage"');
      expect(html).toContain("update lock / pass decisions");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
