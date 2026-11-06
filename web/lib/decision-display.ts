/** Owner-facing wording for Worker decision states; values sent back to the Worker never change. */
import type { DecisionGame } from "./control-types";

export type AdviceTone = "lock" | "pass" | "wait" | "none";

export interface Advice {
  tone: AdviceTone;
  text: string;
}

/** The advisor names a Sleeper action to take, while the owner's choice records one already taken. */
export function adviceFor(advisor: string): Advice {
  switch (advisor) {
    case "Lock":
      return { tone: "lock", text: "Lock this game in Sleeper" };
    case "Pass":
      return { tone: "pass", text: "Pass on this game" };
    case "Wait":
      return { tone: "wait", text: "Hold for now" };
    case "Unavailable":
      return { tone: "none", text: "No suggestion right now" };
    default:
      return { tone: "none", text: advisor };
  }
}

export type RecordedTone = "lock" | "pass" | "alert" | "none";

export interface RecordedStatus {
  tone: RecordedTone;
  text: string;
}

const ALERT_STATES = new Set([
  "Eligibility unverified",
  "Check Sleeper",
  "Not lockable",
]);

/** The Worker's recorded state is what the planner believes already happened, not advice. */
export function recordedStatus(saved: string): RecordedStatus {
  if (saved === "Wait") return { tone: "none", text: "Nothing recorded yet" };
  if (saved === "Locked in Sleeper") return { tone: "lock", text: saved };
  if (saved.startsWith("Passed")) return { tone: "pass", text: saved };
  if (ALERT_STATES.has(saved)) return { tone: "alert", text: saved };
  return { tone: "none", text: saved };
}

export type Urgency = "closed" | "soon" | "later";

export interface Countdown {
  urgency: Urgency;
  text: string;
}

const SOON_MINUTES = 60;

export function countdown(deadline: string, now: number): Countdown {
  const minutes = Math.ceil((new Date(deadline).getTime() - now) / 60_000);
  if (minutes <= 0) return { urgency: "closed", text: "window closed" };
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const rest = minutes % 60;
  const text =
    days > 0
      ? `in ${days}d ${hours}h`
      : hours > 0
        ? `in ${hours}h ${rest}m`
        : `in ${rest}m`;
  return { urgency: minutes <= SOON_MINUTES ? "soon" : "later", text };
}

export interface DecisionDay {
  key: string;
  label: string;
  games: { game: DecisionGame; index: number }[];
}

function dayKeyFormat(timeZone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

export function dayKey(instant: number, timeZone: string): string {
  return dayKeyFormat(timeZone).format(new Date(instant));
}

/** Order by lock deadline and group by local tip-off day, keeping each game's form index. */
export function groupDecisionsByDay(
  games: readonly DecisionGame[],
  timeZone: string,
): DecisionDay[] {
  const keyFormat = dayKeyFormat(timeZone);
  const labelFormat = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  const days = new Map<string, DecisionDay>();
  const ordered = games
    .map((game, index) => ({ game, index }))
    .sort(
      (a, b) =>
        new Date(a.game.deadline).getTime() -
        new Date(b.game.deadline).getTime(),
    );
  for (const entry of ordered) {
    const start = new Date(entry.game.start);
    const key = keyFormat.format(start);
    let day = days.get(key);
    if (!day) {
      day = { key, label: labelFormat.format(start), games: [] };
      days.set(key, day);
    }
    day.games.push(entry);
  }
  return [...days.values()];
}

export function saveSummary(changed: number, dirty: boolean): string {
  if (changed > 0)
    return `${changed} unsaved ${changed === 1 ? "change" : "changes"}`;
  return dirty ? "No unsaved changes" : "Updates your plan";
}
