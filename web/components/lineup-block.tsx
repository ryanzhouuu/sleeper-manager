import {
  MISSING,
  formatGameMoment,
  formatPercent,
  formatScore,
  formatTotals,
  opponentLabel,
  GAME_STATE_LABELS,
} from "@/lib/format";
import type { Decision, LineupGame, LineupRow, PlanSummary } from "@/lib/home";
import { Mark } from "./mark";
import { OverrideControl } from "./override-control";

export function PlanBlock({ plan }: { plan: PlanSummary }) {
  if (plan.team_after === null) return null;
  const range =
    plan.p10 === null || plan.p90 === null
      ? null
      : `${formatScore(plan.p10)}–${formatScore(plan.p90)} range`;
  return (
    <section className="block">
      <h2 className="label">plan</h2>
      <p className="plan-total">
        team {formatTotals(plan.team_before, plan.team_after)}
      </p>
      <p className="plan-detail">
        {[plan.confidence && `${plan.confidence} confidence`, range]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </section>
  );
}

export function LineupBlock({
  lineup,
  timeZone,
}: {
  lineup: LineupRow[];
  timeZone: string;
}) {
  return (
    <section className="block">
      <h2 className="label">lineup</h2>
      {lineup.length === 0 ? (
        <p>no lineup yet.</p>
      ) : (
        <ul className="lineup">
          {lineup.map((row, index) => (
            <li className="lineup-row" key={index}>
              <div className="row lineup-head">
                <span className="text">{row.slot}</span>
                <span className="cell">
                  <Mark src={row.image_url} alt={row.name ?? ""} size={20} />
                </span>
                <span className="text">{row.name ?? MISSING}</span>
                <span className="text">{row.nba_team ?? ""}</span>
              </div>
              <ul className="lineup-games">
                {row.games.map((game, gameIndex) => (
                  <GameLine
                    key={gameIndex}
                    game={game}
                    playerId={row.player_id}
                    timeZone={timeZone}
                  />
                ))}
              </ul>
              {row.decision ? (
                <DecisionLine decision={row.decision} timeZone={timeZone} />
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function GameLine({
  game,
  playerId,
  timeZone,
}: {
  game: LineupGame;
  playerId: string | null;
  timeZone: string;
}) {
  return (
    <li className="lineup-game" data-state={game.state}>
      <span className="number">{formatGameMoment(game.start, timeZone)}</span>
      <span className="text">
        {game.home === null
          ? (game.opponent ?? MISSING)
          : opponentLabel({ home: game.home, opponent: game.opponent })}
      </span>
      <span className="number">
        {game.score === null ? "" : formatScore(game.score)}
      </span>
      <span className="game-state">
        {GAME_STATE_LABELS[game.state]}
        {game.deadline
          ? ` · lock by ${formatGameMoment(game.deadline, timeZone)}`
          : ""}
      </span>
      {game.overridable &&
      playerId !== null &&
      game.game_id !== null &&
      game.version !== null ? (
        <OverrideControl
          playerId={playerId}
          gameId={game.game_id}
          version={game.version}
        />
      ) : null}
    </li>
  );
}

function DecisionLine({
  decision,
  timeZone,
}: {
  decision: Decision;
  timeZone: string;
}) {
  const evidence = [
    decision.team_after === null
      ? null
      : `team ${formatTotals(decision.team_before, decision.team_after)}`,
    decision.alternative_expected === null
      ? null
      : `alt ${formatScore(decision.alternative_expected)}`,
    decision.confidence === null ? null : formatPercent(decision.confidence),
    decision.deadline === null
      ? null
      : `by ${formatGameMoment(decision.deadline, timeZone)}`,
  ].filter(Boolean);
  return (
    <div className="decision" data-decision={decision.kind}>
      <p className="heading">→ {decision.text}</p>
      {evidence.length > 0 ? (
        <p className="decision-evidence">{evidence.join(" · ")}</p>
      ) : null}
    </div>
  );
}
