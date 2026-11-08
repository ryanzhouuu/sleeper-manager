export type SectionStatus = "ready" | "empty" | "unavailable";

export type PlanStatus =
  "action_required" | "no_action" | "degraded" | "blocked";

export interface TeamRecord {
  wins: number;
  losses: number;
  ties: number;
}

export type LockData = "ready" | "pending" | "unavailable";

export interface MatchupSide {
  name: string | null;
  score: number | null;
  image_url: string | null;
  record: TeamRecord | null;
  points_for: number | null;
  score_final: boolean;
  locked: number | null;
  starters: number | null;
  lock_data: LockData;
}

export interface RosterEntry {
  slot: string;
  name: string | null;
  nba_team: string | null;
  positions: string[];
  injury_status: string | null;
  image_url: string | null;
  team_image_url: string | null;
}

export interface GameItem {
  start: Date;
  player_name: string | null;
  injury_status: string | null;
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

export type GameStateName =
  | "locked"
  | "lockable"
  | "passed_eligibility_lost"
  | "passed_window_closed"
  | "not_eligible"
  | "unverified"
  | "upcoming"
  | "live";

export type DecisionKind = "lock" | "preserve" | "sacrifice" | "move";

export interface LineupGame {
  game_id: string | null;
  start: Date;
  opponent: string | null;
  opponent_image_url: string | null;
  home: boolean | null;
  score: number | null;
  state: GameStateName;
  deadline: Date | null;
  overridable: boolean;
  version: number | null;
}

export interface Decision {
  kind: DecisionKind;
  text: string;
  game_id: string | null;
  score: number | null;
  alternative_expected: number | null;
  confidence: number | null;
  deadline: Date | null;
  team_before: number | null;
  team_after: number | null;
}

export interface LineupRow {
  slot: string;
  slot_index: number | null;
  player_id: string | null;
  name: string | null;
  nba_team: string | null;
  image_url: string | null;
  games: LineupGame[];
  decision: Decision | null;
}

export interface UndecidedFinal {
  name: string | null;
  pass_confidence: number | null;
  lock_confidence: number | null;
}

export interface PlanSummary {
  status: PlanStatus | null;
  team_before: number | null;
  team_after: number | null;
  confidence: string | null;
  p10: number | null;
  p90: number | null;
  undecided: UndecidedFinal[];
}

export interface HomeDocument {
  league: { name: string; week: number; timezone: string };
  matchup: { manager: MatchupSide; opponent: MatchupSide | null };
  roster: RosterEntry[];
  games: { status: SectionStatus; items: GameItem[] };
  recommendation: Recommendation;
  lineup: LineupRow[];
  plan: PlanSummary;
  lock_data: LockData;
}

export type HomeResult =
  { status: "ok"; document: HomeDocument } | { status: "unavailable" };

const SECTION_STATUSES: readonly string[] = ["ready", "empty", "unavailable"];
const LOCK_DATA: readonly string[] = ["ready", "pending", "unavailable"];
const GAME_STATES: readonly string[] = [
  "locked",
  "lockable",
  "passed_eligibility_lost",
  "passed_window_closed",
  "not_eligible",
  "unverified",
  "upcoming",
  "live",
];
const DECISION_KINDS: readonly string[] = [
  "lock",
  "preserve",
  "sacrifice",
  "move",
];
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
    // Workers deployed before the single dashboard omit these sections.
    lineup: root.lineup === undefined ? [] : array(root.lineup).map(lineupRow),
    plan: planSummary(root.plan),
    lock_data: lockData(root.lock_data),
  };
}

function lineupRow(value: unknown): LineupRow {
  const fields = record(value);
  return {
    slot: string(fields.slot),
    slot_index: nullableInteger(fields.slot_index),
    player_id: nullableString(fields.player_id),
    name: nullableString(fields.name),
    nba_team: nullableString(fields.nba_team),
    image_url: nullableString(fields.image_url),
    games: array(fields.games).map(lineupGame),
    decision: fields.decision === null ? null : decision(fields.decision),
  };
}

function lineupGame(value: unknown): LineupGame {
  const fields = record(value);
  const overridable = flag(fields.overridable);
  const version = nullableInteger(fields.version);
  if (overridable && version === null) {
    throw new InvalidDocument("version");
  }
  return {
    game_id: nullableString(fields.game_id),
    start: instant(fields.start),
    opponent: nullableString(fields.opponent),
    opponent_image_url: nullableString(fields.opponent_image_url),
    home: fields.home === null ? null : flag(fields.home),
    score: nullableNumber(fields.score),
    state: member(fields.state, GAME_STATES) as GameStateName,
    deadline: fields.deadline === null ? null : instant(fields.deadline),
    overridable,
    version,
  };
}

function decision(value: unknown): Decision {
  const fields = record(value);
  return {
    kind: member(fields.kind, DECISION_KINDS) as DecisionKind,
    text: string(fields.text),
    game_id: nullableString(fields.game_id),
    score: nullableNumber(fields.score),
    alternative_expected: nullableNumber(fields.alternative_expected),
    confidence: nullableNumber(fields.confidence),
    deadline:
      fields.deadline === null || fields.deadline === undefined
        ? null
        : instant(fields.deadline),
    team_before: nullableNumber(fields.team_before),
    team_after: nullableNumber(fields.team_after),
  };
}

function planSummary(value: unknown): PlanSummary {
  const fields = value === undefined ? {} : record(value);
  return {
    status: planStatus(fields.status),
    team_before: nullableNumber(fields.team_before),
    team_after: nullableNumber(fields.team_after),
    confidence: nullableString(fields.confidence),
    p10: nullableNumber(fields.p10),
    p90: nullableNumber(fields.p90),
    // Workers deployed before undecided finals were reported omit the field.
    undecided:
      fields.undecided === undefined
        ? []
        : array(fields.undecided).map(undecidedFinal),
  };
}

function undecidedFinal(value: unknown): UndecidedFinal {
  const fields = record(value);
  return {
    name: nullableString(fields.name),
    pass_confidence: nullableNumber(fields.pass_confidence),
    lock_confidence: nullableNumber(fields.lock_confidence),
  };
}

function member(value: unknown, allowed: readonly string[]): string {
  if (typeof value !== "string" || !allowed.includes(value)) {
    throw new InvalidDocument("member");
  }
  return value;
}

function flag(value: unknown): boolean {
  if (typeof value !== "boolean") {
    throw new InvalidDocument("flag");
  }
  return value;
}

function side(value: unknown): MatchupSide {
  const fields = record(value);
  return {
    name: nullableString(fields.name),
    score: nullableNumber(fields.score),
    image_url: nullableString(fields.image_url),
    record: teamRecord(fields.record),
    points_for: nullableNumber(fields.points_for),
    // Workers deployed before lock-in scoring omit the lock fields.
    score_final: fields.score_final === true,
    locked: nullableInteger(fields.locked),
    starters: nullableInteger(fields.starters),
    lock_data: lockData(fields.lock_data),
  };
}

function lockData(value: unknown): LockData {
  if (value === undefined) {
    return "ready";
  }
  if (typeof value !== "string" || !LOCK_DATA.includes(value)) {
    throw new InvalidDocument("lock_data");
  }
  return value as LockData;
}

// Workers deployed before records, positions, and injury status omit them.
function teamRecord(value: unknown): TeamRecord | null {
  if (value === null || value === undefined) {
    return null;
  }
  const fields = record(value);
  return {
    wins: integer(fields.wins),
    losses: integer(fields.losses),
    ties: integer(fields.ties),
  };
}

function positions(value: unknown): string[] {
  return value === undefined ? [] : array(value).map(string);
}

function rosterEntry(value: unknown): RosterEntry {
  const fields = record(value);
  return {
    slot: string(fields.slot),
    name: nullableString(fields.name),
    nba_team: nullableString(fields.nba_team),
    positions: positions(fields.positions),
    injury_status: nullableString(fields.injury_status),
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
    injury_status: nullableString(fields.injury_status),
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

function nullableInteger(value: unknown): number | null {
  return value === null || value === undefined ? null : integer(value);
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
