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

  it("puts the lineup first and the schedule tabs in the second column", async () => {
    loadHome.mockResolvedValue({
      status: "ok",
      document: parseHomeDocument(fixture),
    });

    const columns = (await render()).split('<div class="column">');

    expect(columns[0]).toContain('<main class="page split">');
    expect(columns).toHaveLength(3);
    expect(columns[1]).toContain("week 3");
    expect(columns[1]).toContain("300.0 → 312.5");
    expect(columns[1]).toContain("lineup");
    expect(columns[1]).not.toContain('role="tablist"');
    expect(columns[2]).toContain('role="tablist"');
    expect(columns[2]).toContain("this week");
    expect(columns[2]).toContain("help");
  });

  it("keeps the unavailable page to one column", async () => {
    loadHome.mockResolvedValue({ status: "unavailable" });

    const html = await render();

    expect(html).toContain('<main class="page">');
    expect(html).not.toContain("column");
    expect(html).toContain("league could not be loaded.");
  });

  it("shows lock data unavailable banner", async () => {
    const document = parseHomeDocument(fixture);
    loadHome.mockResolvedValue({
      status: "ok",
      document: { ...document, lock_data: "unavailable" },
    });

    const html = await render();

    expect(html).toContain('role="alert"');
    expect(html).toContain("Lock data unavailable from Sleeper");
  });

  it("has no link to /manage", async () => {
    vi.stubEnv("HOME_URL", "https://worker.test/home");
    try {
      loadHome.mockResolvedValue({
        status: "ok",
        document: parseHomeDocument(fixture),
      });
      const html = await render();
      expect(html).not.toContain('href="/manage"');
      expect(html).not.toContain("update lock / pass decisions");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
