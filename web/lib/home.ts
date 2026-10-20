export type SectionStatus = "ready" | "empty" | "unavailable";

export type PlanStatus =
  "action_required" | "no_action" | "degraded" | "blocked";

export interface MatchupSide {
  name: string | null;
  score: number | null;
  image_url: string | null;
}

export interface RosterEntry {
  slot: string;
  name: string | null;
  nba_team: string | null;
  image_url: string | null;
  team_image_url: string | null;
}

export interface GameItem {
  start: Date;
  player_name: string | null;
  player_image_url: string | null;
  opponent: string | null;
  opponent_image_url: string | null;
  home: boolean;
}

export type Recommendation =
  | {
      status: "ready";
      plan_status: PlanStatus | null;
      title: string;
      message: string;
    }
  | { status: "empty" | "unavailable" };

export interface HomeDocument {
  league: { name: string; week: number; timezone: string };
  matchup: { manager: MatchupSide; opponent: MatchupSide | null };
  roster: RosterEntry[];
  games: { status: SectionStatus; items: GameItem[] };
  recommendation: Recommendation;
}

export type HomeResult =
  { status: "ok"; document: HomeDocument } | { status: "unavailable" };

const SECTION_STATUSES: readonly string[] = ["ready", "empty", "unavailable"];
const PLAN_STATUSES: readonly string[] = [
  "action_required",
  "no_action",
  "degraded",
  "blocked",
];

class InvalidDocument extends Error {}

export async function loadHome(
  url: string | undefined,
  fetchImpl: typeof fetch = fetch,
): Promise<HomeResult> {
  if (!url) {
    return { status: "unavailable" };
  }
  try {
    const response = await fetchImpl(url, { cache: "no-store" });
    if (response.status !== 200) {
      return { status: "unavailable" };
    }
    return { status: "ok", document: parseHomeDocument(await response.json()) };
  } catch {
    return { status: "unavailable" };
  }
}

export function parseHomeDocument(value: unknown): HomeDocument {
  const root = record(value);
  const league = record(root.league);
  const matchup = record(root.matchup);
  const games = record(root.games);
  const gamesStatus = sectionStatus(games.status);
  return {
    league: {
      name: string(league.name),
      week: integer(league.week),
      timezone: timezone(league.timezone),
    },
    matchup: {
      manager: side(matchup.manager),
      opponent: matchup.opponent === null ? null : side(matchup.opponent),
    },
    roster: array(root.roster).map(rosterEntry),
    games: {
      status: gamesStatus,
      items: gamesStatus === "ready" ? array(games.items).map(gameItem) : [],
    },
    recommendation: recommendation(root.recommendation),
  };
}

function side(value: unknown): MatchupSide {
  const fields = record(value);
  return {
    name: nullableString(fields.name),
    score: nullableNumber(fields.score),
    image_url: nullableString(fields.image_url),
  };
}

function rosterEntry(value: unknown): RosterEntry {
  const fields = record(value);
  return {
    slot: string(fields.slot),
    name: nullableString(fields.name),
    nba_team: nullableString(fields.nba_team),
    image_url: nullableString(fields.image_url),
    team_image_url: nullableString(fields.team_image_url),
  };
}

function gameItem(value: unknown): GameItem {
  const fields = record(value);
  if (typeof fields.home !== "boolean") {
    throw new InvalidDocument("home");
  }
  return {
    start: instant(fields.start),
    player_name: nullableString(fields.player_name),
    player_image_url: nullableString(fields.player_image_url),
    opponent: nullableString(fields.opponent),
    opponent_image_url: nullableString(fields.opponent_image_url),
    home: fields.home,
  };
}

function recommendation(value: unknown): Recommendation {
  const fields = record(value);
  const status = sectionStatus(fields.status);
  if (status !== "ready") {
    return { status };
  }
  return {
    status,
    // Workers deployed before plan_status existed omit the field.
    plan_status: planStatus(fields.plan_status),
    title: string(fields.title),
    message: string(fields.message),
  };
}

function planStatus(value: unknown): PlanStatus | null {
  return typeof value === "string" && PLAN_STATUSES.includes(value)
    ? (value as PlanStatus)
    : null;
}

function sectionStatus(value: unknown): SectionStatus {
  if (typeof value !== "string" || !SECTION_STATUSES.includes(value)) {
    throw new InvalidDocument("status");
  }
  return value as SectionStatus;
}

function timezone(value: unknown): string {
  const name = string(value);
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: name });
  } catch {
    throw new InvalidDocument("timezone");
  }
  return name;
}

function instant(value: unknown): Date {
  const date = new Date(string(value));
  if (Number.isNaN(date.getTime())) {
    throw new InvalidDocument("instant");
  }
  return date;
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new InvalidDocument("object");
  }
  return value as Record<string, unknown>;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) {
    throw new InvalidDocument("array");
  }
  return value;
}

function string(value: unknown): string {
  if (typeof value !== "string") {
    throw new InvalidDocument("string");
  }
  return value;
}

function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new InvalidDocument("integer");
  }
  return value;
}

function nullableString(value: unknown): string | null {
  return value === null || value === undefined ? null : string(value);
}

function nullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new InvalidDocument("number");
  }
  return value;
}
