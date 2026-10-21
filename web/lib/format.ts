import type { GameItem, TeamRecord } from "./home";

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

export function formatPositions(positions: readonly string[]): string {
  return positions.length > 0 ? positions.join("/") : MISSING;
}
