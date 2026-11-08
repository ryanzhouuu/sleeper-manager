import type { GameItem, GameStateName, MatchupSide, TeamRecord } from "./home";

export const MISSING = "—";

export interface GameDay {
  key: string;
  label: string;
  games: GameItem[];
}

export function groupGamesByDay(
  items: readonly GameItem[],
  timeZone: string,
): GameDay[] {
  const keyFormat = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const labelFormat = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  const days = new Map<string, GameDay>();
  const ordered = [...items].sort(
    (a, b) => a.start.getTime() - b.start.getTime(),
  );
  for (const game of ordered) {
    const key = keyFormat.format(game.start);
    let day = days.get(key);
    if (!day) {
      day = {
        key,
        label: labelFormat.format(game.start).replace(",", ""),
        games: [],
      };
      days.set(key, day);
    }
    day.games.push(game);
  }
  return [...days.values()];
}

export function formatLocalTime(start: Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "numeric",
    minute: "2-digit",
  }).format(start);
}

export function opponentLabel(
  game: Pick<GameItem, "home" | "opponent">,
): string {
  return `${game.home ? "vs" : "@"} ${game.opponent ?? MISSING}`;
}

export function formatScore(score: number): string {
  return score.toFixed(1);
}

export function formatRecord({ wins, losses, ties }: TeamRecord): string {
  return ties > 0 ? `${wins}-${losses}-${ties}` : `${wins}-${losses}`;
}

export function formatPointsFor(points: number): string {
  return `${points.toFixed(1)} pf`;
}

export function formatLockProgress(
  side: Pick<MatchupSide, "score_final" | "locked" | "starters" | "lock_data">,
): string | null {
  if (side.lock_data === "unavailable") {
    return "lock data unavailable";
  }
  if (side.score_final) {
    return "final";
  }
  const count =
    side.locked === null || side.starters === null
      ? null
      : `${side.locked}/${side.starters} locked`;
  if (side.lock_data === "pending") {
    return count === null ? "stats pending" : `${count} · stats pending`;
  }
  return count;
}

export const GAME_STATE_LABELS: Record<GameStateName, string> = {
  locked: "locked",
  lockable: "lockable",
  passed_eligibility_lost: "passed · eligibility lost",
  passed_window_closed: "passed · window closed",
  not_eligible: "not eligible",
  unverified: "unverified",
  upcoming: "upcoming",
  live: "live",
};

export function formatGameMoment(moment: Date, timeZone: string): string {
  const day = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
  }).format(moment);
  return `${day} ${formatLocalTime(moment, timeZone)}`;
}

export function formatTotals(before: number | null, after: number): string {
  return before === null
    ? formatScore(after)
    : `${formatScore(before)} → ${formatScore(after)}`;
}

export function formatPercent(fraction: number): string {
  return `${Math.round(fraction * 100)}%`;
}
