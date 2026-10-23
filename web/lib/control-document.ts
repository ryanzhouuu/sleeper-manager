/** Validate Worker documents before rendering private forms or using their versions. */
import type {
  ControlDocument,
  DecisionAction,
  DecisionGame,
} from "./control-types";

export const ACTION_LABELS: Record<DecisionAction, string> = {
  not_locked: "Wait",
  locked: "Locked in Sleeper",
  passed: "Passed",
};

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid document");
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid text");
  return value;
}

function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error("Invalid number");
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Invalid flag");
  return value;
}

function list<T>(
  value: unknown,
  parse: (item: Record<string, unknown>) => T,
): T[] {
  if (!Array.isArray(value)) throw new Error("Invalid list");
  return value.map((item) => parse(record(item)));
}

function moment(value: unknown): string {
  const result = text(value);
  if (!Number.isFinite(Date.parse(result))) throw new Error("Invalid time");
  return result;
}

function action(value: unknown): DecisionAction {
  if (value !== "not_locked" && value !== "locked" && value !== "passed")
    throw new Error("Invalid action");
  return value;
}

/** Image sources remain restricted to the provider already allowed by the dashboard. */
function image(value: unknown): string | null {
  if (value === null) return null;
  const url = new URL(text(value));
  if (url.origin !== "https://a.espncdn.com" || url.username || url.password)
    throw new Error("Invalid image");
  return url.href;
}

function game(row: Record<string, unknown>): DecisionGame {
  const version = number(row.version);
  if (!Number.isSafeInteger(version) || version < 0)
    throw new Error("Invalid version");
  return {
    player_id: text(row.player_id),
    game_id: text(row.game_id),
    version,
    name: text(row.name),
    placement: text(row.placement),
    start: moment(row.start),
    deadline: moment(row.deadline),
    score: row.score === null ? null : number(row.score),
    action: action(row.action),
    advisor: text(row.advisor),
    saved: text(row.saved),
    portrait_url: image(row.portrait_url),
    team: row.team === null ? null : text(row.team),
    team_image_url: image(row.team_image_url),
  };
}

/** Reject unknown response shapes; never interpret an upstream HTML login as saved. */
export function parseControlDocument(value: unknown): ControlDocument {
  const row = record(value);
  switch (row.status) {
    case "login":
      return { status: "login", rejected: boolean(row.rejected) };
    case "saved":
      return { status: "saved" };
    case "message":
      return {
        status: "message",
        title: text(row.title),
        ...(row.message === undefined ? {} : { message: text(row.message) }),
      };
    case "confirmation": {
      const decision = action(row.action);
      const token = text(row.token);
      if (decision === "not_locked" || !token || token.length > 256)
        throw new Error("Invalid confirmation");
      return {
        status: "confirmation",
        heading: text(row.heading),
        title: text(row.title),
        message: text(row.message),
        instruction: text(row.instruction),
        label: text(row.label),
        token,
        action: decision,
      };
    }
    case "ready": {
      const timezone = text(row.timezone);
      new Intl.DateTimeFormat("en-US", { timeZone: timezone });
      const week = number(row.week);
      if (!Number.isSafeInteger(week) || week < 1)
        throw new Error("Invalid week");
      const plan = row.plan === null ? null : record(row.plan);
      return {
        status: "ready",
        week,
        timezone,
        csrf: text(row.csrf),
        batch_id: text(row.batch_id),
        request_id: text(row.request_id),
        pending: boolean(row.pending),
        plan: plan && { title: text(plan.title), message: text(plan.message) },
        active: list(row.active, game),
        saved: list(row.saved, game),
        past: list(row.past, (item) => ({
          name: text(item.name),
          start: moment(item.start),
          score: number(item.score),
          state: text(item.state),
        })),
        history: list(row.history, (item) => ({
          name: text(item.name),
          game_id: text(item.game_id),
          previous: action(item.previous),
          action: action(item.action),
          recorded_at: moment(item.recorded_at),
        })),
      };
    }
    default:
      throw new Error("Unknown document");
  }
}
