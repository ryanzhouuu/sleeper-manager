/** Native radio groups record the owner's choice independently of advisor recommendations. */
import Image from "next/image";
import { ACTION_LABELS } from "@/lib/control-document";
import type { DecisionAction, DecisionGame } from "@/lib/control-types";

export const CHOICES: DecisionAction[] = ["not_locked", "locked", "passed"];

export function ControlIcon({ kind }: { kind: DecisionAction | "arrow" }) {
  const paths = {
    not_locked: (
      <>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    locked: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V7a4 4 0 018 0v3" />
      </>
    ),
    passed: (
      <>
        <path d="m5 5 10 7-10 7V5Zm14 0v14" />
      </>
    ),
    arrow: <path d="M4 12h16m-6-6l6 6-6 6" />,
  };
  return (
    <svg
      className="control-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      aria-hidden="true"
    >
      {paths[kind]}
    </svg>
  );
}

/** Show short local timestamps while retaining a machine-readable instant. */
export function ControlTime({
  value,
  timezone,
}: {
  value: string;
  timezone: string;
}) {
  const label = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
  return <time dateTime={value}>{label}</time>;
}

function Badge({
  state,
  source,
}: {
  state: string;
  source: "Advisor" | "Recorded";
}) {
  const label = state.split(" · ")[0].replace("Locked in Sleeper", "Locked");
  const tone = ["Lock", "Locked"].includes(label)
    ? "lock"
    : ["Pass", "Passed"].includes(label)
      ? "pass"
      : label === "Wait"
        ? "wait"
        : "alert";
  return (
    <span
      className={`control-chip control-chip--${tone}`}
      aria-label={`${source}: ${state}`}
      title={state}
    >
      {label}
    </span>
  );
}

export function DecisionCard({
  game,
  index,
  timezone,
}: {
  game: DecisionGame;
  index: number;
  timezone: string;
}) {
  return (
    <section className="decision-card" aria-label={game.name}>
      <div className="decision-head">
        <div className="decision-identity">
          {game.portrait_url ? (
            <Image
              className="decision-portrait"
              src={game.portrait_url}
              alt=""
              width={44}
              height={44}
              referrerPolicy="no-referrer"
            />
          ) : null}
          <div>
            <h3>{game.name}</h3>
            <p className="control-muted decision-meta">
              {game.team_image_url ? (
                <Image
                  src={game.team_image_url}
                  alt={game.team ?? ""}
                  width={18}
                  height={18}
                  referrerPolicy="no-referrer"
                />
              ) : null}
              {game.placement} ·{" "}
              <ControlTime value={game.start} timezone={timezone} />
            </p>
          </div>
        </div>
        <div className="decision-score">
          {game.score?.toFixed(2) ?? "—"}
          <small>pts</small>
        </div>
      </div>
      <div className="decision-states">
        <span>
          <span className="control-muted">Plan</span>
          <Badge state={game.advisor} source="Advisor" />
        </span>
        <span>
          <span className="control-muted">Saved</span>
          <Badge state={game.saved} source="Recorded" />
        </span>
      </div>
      <input type="hidden" name={`player_id_${index}`} value={game.player_id} />
      <input type="hidden" name={`game_id_${index}`} value={game.game_id} />
      <input type="hidden" name={`version_${index}`} value={game.version} />
      <fieldset className="decision-choices">
        <legend className="control-sr">Your choice for {game.name}</legend>
        {CHOICES.map((action) => (
          <label
            className="decision-choice"
            key={action}
            title={ACTION_LABELS[action]}
          >
            <input
              type="radio"
              name={`action_${index}`}
              value={action}
              defaultChecked={game.action === action}
              aria-label={ACTION_LABELS[action]}
            />
            <span className="decision-option">
              <ControlIcon kind={action} />
              {action === "locked"
                ? "Lock"
                : action === "passed"
                  ? "Pass"
                  : "Wait"}
            </span>
          </label>
        ))}
      </fieldset>
      <p className="decision-deadline">
        <ControlIcon kind="not_locked" />
        <span className="control-sr">Lock window ends </span>
        <ControlTime value={game.deadline} timezone={timezone} />
      </p>
    </section>
  );
}
