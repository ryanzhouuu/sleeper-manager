import { describe, expect, it } from "vitest";
import fixture from "@/test/fixtures/home.json";
import { loadHome, parseHomeDocument } from "./home";

const REMOVE = Symbol("remove");

type Fields = Record<string, unknown>;

function withFields(changes: Record<string, unknown>): unknown {
  const document = structuredClone(fixture) as unknown as Fields;
  for (const [path, value] of Object.entries(changes)) {
    const keys = path.split(".");
    const last = keys.pop() as string;
    const target = keys.reduce<Fields>(
      (node, key) => node[key] as Fields,
      document,
    );
    if (value === REMOVE) {
      delete target[last];
    } else {
      target[last] = value;
    }
  }
  return document;
}

function respond(status: number, body: unknown): typeof fetch {
  return async () => new Response(JSON.stringify(body), { status });
}

describe("parseHomeDocument", () => {
  it("reads the full document", () => {
    const document = parseHomeDocument(fixture);

    expect(document.league).toEqual({
      name: "Lock In",
      week: 3,
      timezone: "America/Chicago",
    });
    expect(document.roster[1].name).toBeNull();
    expect(document.games.items[0].start).toEqual(
      new Date("2026-10-22T04:30:00Z"),
    );
    expect(document.recommendation).toMatchObject({
      plan_status: "action_required",
    });
  });

  it("reads records, positions, and injury status", () => {
    const document = parseHomeDocument(fixture);

    expect(document.matchup.manager.record).toEqual({
      wins: 2,
      losses: 1,
      ties: 0,
    });
    expect(document.matchup.manager.points_for).toBe(812.4);
    expect(document.roster[2].positions).toEqual(["SF", "PF"]);
    expect(document.roster[2].injury_status).toBe("DTD");
    expect(document.games.items[0].injury_status).toBe("DTD");
  });

  it("defaults fields an older worker omits", () => {
    const document = parseHomeDocument(
      withFields({
        "matchup.manager.record": REMOVE,
        "matchup.manager.points_for": REMOVE,
        "roster.0.positions": REMOVE,
        "roster.0.injury_status": REMOVE,
        "games.items.0.injury_status": REMOVE,
      }),
    );

    expect(document.matchup.manager.record).toBeNull();
    expect(document.matchup.manager.points_for).toBeNull();
    expect(document.roster[0].positions).toEqual([]);
    expect(document.roster[0].injury_status).toBeNull();
    expect(document.games.items[0].injury_status).toBeNull();
  });

  it("treats a missing or unknown plan status as null", () => {
    const missing = withFields({ "recommendation.plan_status": REMOVE });
    const unknown = withFields({ "recommendation.plan_status": "pending" });

    expect(parseHomeDocument(missing).recommendation).toMatchObject({
      plan_status: null,
    });
    expect(parseHomeDocument(unknown).recommendation).toMatchObject({
      plan_status: null,
    });
  });

  it("keeps a bye week and null scores", () => {
    const document = parseHomeDocument(
      withFields({ "matchup.opponent": null, "matchup.manager.score": null }),
    );

    expect(document.matchup.opponent).toBeNull();
    expect(document.matchup.manager.score).toBeNull();
  });

  it("drops items for a section that is not ready", () => {
    const document = parseHomeDocument(
      withFields({ "games.status": "unavailable" }),
    );

    expect(document.games).toEqual({ status: "unavailable", items: [] });
  });

  it.each([
    ["an unknown section status", { "games.status": "partial" }],
    ["an invalid timezone", { "league.timezone": "Mars/Base" }],
    ["an invalid game start", { "games.items.0.start": "soon" }],
    ["a missing roster", { roster: REMOVE }],
    ["a string score", { "matchup.manager.score": "142.6" }],
    ["a fractional win count", { "matchup.manager.record.wins": 1.5 }],
    ["a non-string position", { "roster.0.positions": ["PG", 3] }],
  ])("rejects %s", (_, changes) => {
    expect(() => parseHomeDocument(withFields(changes))).toThrow();
  });
});

describe("loadHome", () => {
  it("returns the parsed document on 200", async () => {
    const result = await loadHome(
      "https://worker.test/home",
      respond(200, fixture),
    );

    expect(result.status).toBe("ok");
  });

  it.each([
    ["no url", undefined, respond(200, fixture)],
    [
      "a 503",
      "https://worker.test/home",
      respond(503, { status: "league_unavailable" }),
    ],
    [
      "an invalid body",
      "https://worker.test/home",
      respond(200, { status: "ok" }),
    ],
    [
      "a network failure",
      "https://worker.test/home",
      (async () => {
        throw new TypeError("fetch failed");
      }) as typeof fetch,
    ],
  ])("is unavailable for %s", async (_, url, fetchImpl) => {
    expect(await loadHome(url, fetchImpl)).toEqual({ status: "unavailable" });
  });

  it("never caches the worker response", async () => {
    let init: RequestInit | undefined;
    await loadHome("https://worker.test/home", async (_, options) => {
      init = options;
      return new Response(JSON.stringify(fixture));
    });

    expect(init?.cache).toBe("no-store");
  });
});
