import {
  MISSING,
  formatLocalTime,
  formatScore,
  groupGamesByDay,
  opponentLabel,
} from "@/lib/format";
import type {
  HomeDocument,
  MatchupSide,
  Recommendation,
  RosterEntry,
  SectionStatus,
} from "@/lib/home";
import { Mark } from "./mark";

const RECOMMENDATION_SENTENCES = {
  empty: "no lineup recommendation yet.",
  unavailable: "recommendation could not be loaded.",
} as const;

const GAMES_SENTENCES: Record<Exclude<SectionStatus, "ready">, string> = {
  empty: "no games this week.",
  unavailable: "games could not be loaded.",
};

export function UnavailableBlock() {
  return (
    <section className="block">
      <p>league could not be loaded.</p>
    </section>
  );
}

export function WeekBlock({
  league,
  matchup,
}: Pick<HomeDocument, "league" | "matchup">) {
  const sides = matchup.opponent
    ? [matchup.manager, matchup.opponent]
    : [matchup.manager];
  return (
    <section className="block">
      <h1 className="label">
        {league.name} · week {league.week}
      </h1>
      <ul className="rows">
        {sides.map((side, index) => (
          <SideRow key={index} side={side} />
        ))}
      </ul>
    </section>
  );
}

function SideRow({ side }: { side: MatchupSide }) {
  return (
    <li className="row side-row">
      <span className="cell">
        <Mark src={side.image_url} alt={side.name ?? ""} size={16} />
      </span>
      <span className="text">{side.name ?? MISSING}</span>
      <span className="number">
        {side.score === null ? null : formatScore(side.score)}
      </span>
    </li>
  );
}

export function RecommendationBlock({
  recommendation,
}: {
  recommendation: Recommendation;
}) {
  if (recommendation.status !== "ready") {
    return (
      <section className="block">
        <p>{RECOMMENDATION_SENTENCES[recommendation.status]}</p>
      </section>
    );
  }
  const urgent = recommendation.plan_status === "action_required";
  return (
    <section className="block">
      <h2 className={urgent ? "heading urgent" : "heading"}>
        {urgent ? "! " : null}
        {recommendation.title}
      </h2>
      <p className="message">{recommendation.message}</p>
    </section>
  );
}

export function RosterBlock({ roster }: { roster: RosterEntry[] }) {
  return (
    <section className="block">
      <h2 className="label">roster</h2>
      <ul className="rows">
        {roster.map((entry, index) => (
          <li key={index} className="row roster-row">
            <span className="text">{entry.slot}</span>
            <span className="cell">
              <Mark src={entry.image_url} alt={entry.name ?? ""} size={20} />
            </span>
            <span className="text">{entry.name ?? MISSING}</span>
            <span className="cell">
              <Mark
                src={entry.team_image_url}
                alt={entry.nba_team ?? ""}
                size={16}
              />
            </span>
            <span className="text">{entry.nba_team ?? MISSING}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function GamesBlock({
  games,
  timeZone,
}: {
  games: HomeDocument["games"];
  timeZone: string;
}) {
  if (games.status !== "ready") {
    return (
      <section className="block">
        <h2 className="label">this week</h2>
        <p>{GAMES_SENTENCES[games.status]}</p>
      </section>
    );
  }
  return (
    <section className="block">
      <h2 className="label">this week</h2>
      {groupGamesByDay(games.items, timeZone).map((day) => (
        <div key={day.key} className="day">
          <h3 className="label">{day.label}</h3>
          <ul className="rows">
            {day.games.map((game, index) => (
              <li key={index} className="row game-row">
                <span className="number">
                  {formatLocalTime(game.start, timeZone)}
                </span>
                <span className="cell">
                  <Mark
                    src={game.player_image_url}
                    alt={game.player_name ?? ""}
                    size={20}
                  />
                </span>
                <span className="text">{game.player_name ?? MISSING}</span>
                <span className="cell">
                  <Mark
                    src={game.opponent_image_url}
                    alt={game.opponent ?? ""}
                    size={16}
                  />
                </span>
                <span className="text">{opponentLabel(game)}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
