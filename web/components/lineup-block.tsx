import type { CSSProperties } from "react";
import {
  MISSING,
  formatGameMoment,
  formatPercent,
  formatScore,
  formatTotals,
  opponentLabel,
  GAME_STATE_LABELS,
} from "@/lib/format";
import type {
  Decision,
  LineupGame,
  LineupRow,
  PlanSummary,
  UndecidedFinal,
} from "@/lib/home";
import { Mark } from "./mark";
import { OverrideControl } from "./override-control";

export function PlanBlock({ plan }: { plan: PlanSummary }) {
  if (plan.team_after === null) return <WithheldPlanBlock plan={plan} />;
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

// The planner withholds its total while finals are undecided; team_before is then the all-pass total.
function WithheldPlanBlock({ plan }: { plan: PlanSummary }) {
  if (plan.team_before === null || plan.undecided.length === 0) return null;
  const subject =
    plan.undecided.length === 1 ? "undecided final" : "undecided finals";
  return (
    <section className="block">
      <h2 className="label">plan</h2>
      <p className="plan-total">
        team {formatScore(plan.team_before)} if passed
      </p>
      <p className="plan-detail">
        projection withheld: {subject} too close to call
      </p>
      {plan.undecided.map((final, index) => (
        <p className="plan-detail" key={index}>
          {undecidedLine(final)}
        </p>
      ))}
    </section>
  );
}

function undecidedLine(final: UndecidedFinal): string {
  const odds =
    final.pass_confidence === null || final.lock_confidence === null
      ? null
      : `pass ${formatPercent(final.pass_confidence)} / lock ${formatPercent(final.lock_confidence)}`;
  return [final.name ?? MISSING, odds].filter(Boolean).join(" · ");
}

export function LineupBlock({
  lineup,
  timeZone,
}: {
  lineup: LineupRow[];
  timeZone: string;
}) {
  const columns = Math.max(1, ...lineup.map((row) => row.games.length));
  return (
    <section className="block">
      <h2 className="label">lineup</h2>
      {lineup.length === 0 ? (
        <p>no lineup yet.</p>
      ) : (
        <ul className="lineup" style={{ "--games": columns } as CSSProperties}>
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
              {row.decision ? (
                <DecisionLine decision={row.decision} timeZone={timeZone} />
              ) : null}
              <ul className="lineup-games">
                {row.games.map((game, gameIndex) => (
                  <GameBox
                    key={gameIndex}
                    game={game}
                    playerId={row.player_id}
                    timeZone={timeZone}
                  />
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function GameBox({
  game,
  playerId,
  timeZone,
}: {
  game: LineupGame;
  playerId: string | null;
  timeZone: string;
}) {
  return (
    <li className="game-box" data-state={game.state}>
      <div className="game-box-head">
        <span className="number">{formatGameMoment(game.start, timeZone)}</span>
        <span className="game-score number">
          {game.score === null ? "" : formatScore(game.score)}
        </span>
      </div>
      <div className="game-opponent">
        <Mark
          src={game.opponent_image_url}
          alt={game.opponent ?? ""}
          size={16}
        />
        <span className="text">
          {game.home === null
            ? (game.opponent ?? MISSING)
            : opponentLabel({ home: game.home, opponent: game.opponent })}
        </span>
      </div>
      <p className="game-state">{GAME_STATE_LABELS[game.state]}</p>
      {game.deadline ? (
        <p className="game-deadline">
          lock by{" "}
          <span className="number">
            {formatGameMoment(game.deadline, timeZone)}
          </span>
        </p>
      ) : null}
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
