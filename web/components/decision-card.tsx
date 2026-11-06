/** Native radio groups record the owner's choice independently of advisor recommendations. */
import Image from "next/image";
import { ACTION_LABELS } from "@/lib/control-document";
import type { DecisionAction, DecisionGame } from "@/lib/control-types";
import {
  adviceFor,
  countdown,
  recordedNote,
  type AdviceTone,
} from "@/lib/decision-display";

const CHOICE_TEXT: Record<DecisionAction, string> = {
  not_locked: "Not yet",
  locked: "Locked",
  passed: "Passed",
  not_lockable: "Not lockable",
  lockable: "Sleeper shows a lock",
};

const ADVICE_ICONS: Record<AdviceTone, DecisionAction | null> = {
  lock: "locked",
  pass: "passed",
  wait: "not_locked",
  none: null,
};

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
    not_lockable: (
      <>
        <circle cx="12" cy="12" r="8" />
        <path d="m7 7 10 10" />
      </>
    ),
    lockable: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V7a4 4 0 017-2.6" />
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

function AdviceLine({ advisor }: { advisor: string }) {
  const advice = adviceFor(advisor);
  const icon = ADVICE_ICONS[advice.tone];
  return (
    <p
      className={`decision-advice decision-advice--${advice.tone}`}
      aria-label={`Advisor: ${advisor}`}
    >
      {icon ? <ControlIcon kind={icon} /> : null}
      <span>{advice.text}</span>
    </p>
  );
}

export function DecisionCard({
  game,
  index,
  timezone,
  now = null,
}: {
  game: DecisionGame;
  index: number;
  timezone: string;
  now?: number | null;
}) {
  const recorded = recordedNote(game.saved);
  const remaining = now === null ? null : countdown(game.deadline, now);
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
              {game.placement}
              <span
                className="decision-deadline"
                data-urgency={remaining?.urgency ?? "later"}
              >
                <ControlIcon kind="not_locked" />
                {game.start === game.deadline ? null : (
                  <>
                    Tips off{" "}
                    <ControlTime value={game.start} timezone={timezone} />{" "}
                    ·{" "}
                  </>
                )}
                Lock by{" "}
                <ControlTime value={game.deadline} timezone={timezone} />
                {remaining ? <> · {remaining.text}</> : null}
              </span>
            </p>
          </div>
        </div>
        <div className="decision-score">
          {game.score?.toFixed(2) ?? "—"}
          <small>pts</small>
        </div>
      </div>
      <AdviceLine advisor={game.advisor} />
      {recorded ? (
        <p className="decision-recorded" aria-label={`Recorded: ${game.saved}`}>
          Recorded · {game.saved}
        </p>
      ) : null}
      <input type="hidden" name={`player_id_${index}`} value={game.player_id} />
      <input type="hidden" name={`game_id_${index}`} value={game.game_id} />
      <input type="hidden" name={`version_${index}`} value={game.version} />
      <fieldset className="decision-choices">
        <legend className="decision-legend">
          What you did in Sleeper
          <span className="control-sr"> for {game.name}</span>
        </legend>
        {game.choices.map((action) => (
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
              {CHOICE_TEXT[action]}
            </span>
          </label>
        ))}
      </fieldset>
    </section>
  );
}
