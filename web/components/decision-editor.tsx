/** The editor preserves the Worker's indexed batch form and correction history. */
import type { FormEvent } from "react";
import type { ControlOperation, ManagerDocument } from "@/lib/control-types";
import { ACTION_LABELS } from "@/lib/control-document";
import { ControlIcon, ControlTime, DecisionCard } from "./decision-card";
import { ControlStatus } from "./control-status";

export function DecisionEditor({
  document,
  busy,
  dirty,
  paused,
  onEdit,
  onSubmit,
  onRefresh,
}: {
  document: ManagerDocument;
  busy: boolean;
  dirty: boolean;
  paused: boolean;
  onEdit: () => void;
  onRefresh: () => void;
  onSubmit: (
    event: FormEvent<HTMLFormElement>,
    operation: ControlOperation,
  ) => void;
}) {
  const games = [...document.active, ...document.saved];
  return (
    <>
      {document.pending ? (
        <section className="control-plan">
          {paused ? (
            <p role="status">Your plan is still updating.</p>
          ) : (
            <ControlStatus>Updating your plan…</ControlStatus>
          )}
          {dirty ? (
            <p className="control-muted">
              Your unsaved choices are kept. Save them to update the plan.
            </p>
          ) : paused ? (
            <button
              type="button"
              className="control-text-button"
              onClick={onRefresh}
              disabled={busy}
            >
              Check again ↻
            </button>
          ) : (
            <p className="control-muted">
              This page will update when it is ready.
            </p>
          )}
        </section>
      ) : document.plan ? (
        <details className="control-plan">
          <summary>Team plan</summary>
          <h2 className="label">{document.plan.title}</h2>
          <pre>{document.plan.message}</pre>
        </details>
      ) : (
        <section className="control-plan">
          <p>Plan unavailable</p>
          <form
            method="post"
            action="/manage/api/replan"
            onSubmit={(event) => onSubmit(event, "replan")}
          >
            <input type="hidden" name="csrf" value={document.csrf} />
            <input
              type="hidden"
              name="request_id"
              value={document.request_id}
            />
            <button type="submit" disabled={busy || dirty}>
              Retry plan
            </button>
          </form>
        </section>
      )}
      {games.length ? (
        <form
          key={document.batch_id}
          method="post"
          action="/manage/api/save"
          onSubmit={(event) => onSubmit(event, "save")}
          onChange={onEdit}
        >
          <fieldset disabled={busy} className="control-fieldset">
            <input type="hidden" name="csrf" value={document.csrf} />
            <input type="hidden" name="batch_id" value={document.batch_id} />
            <input type="hidden" name="count" value={games.length} />
            <h2 className="control-section-title">
              To review <span>{document.active.length}</span>
            </h2>
            {document.active.length ? (
              document.active.map((game, index) => (
                <DecisionCard
                  game={game}
                  index={index}
                  timezone={document.timezone}
                  key={`${game.player_id}:${game.game_id}`}
                />
              ))
            ) : (
              <p className="control-muted">No lockable games yet.</p>
            )}
            {document.saved.length ? (
              <details>
                <summary>Saved decisions · {document.saved.length}</summary>
                <p className="control-muted">
                  Edit to correct a saved decision.
                </p>
                {document.saved.map((game, index) => (
                  <DecisionCard
                    game={game}
                    index={document.active.length + index}
                    timezone={document.timezone}
                    key={`${game.player_id}:${game.game_id}`}
                  />
                ))}
              </details>
            ) : null}
            <div className="control-save-bar">
              <small>{dirty ? "Unsaved choices" : "Updates your plan"}</small>
              <button type="submit">
                Save &amp; replan <ControlIcon kind="arrow" />
              </button>
            </div>
          </fieldset>
        </form>
      ) : (
        <section className="decision-card control-muted">
          No lockable games yet.
        </section>
      )}
      {document.past.length ? (
        <details>
          <summary>Past games</summary>
          <ul>
            {document.past.map((game, index) => (
              <li key={index}>
                {game.name} ·{" "}
                <ControlTime value={game.start} timezone={document.timezone} />{" "}
                · {game.score.toFixed(2)} pts
                <br />
                <span className="control-muted">{game.state}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <details>
        <summary>History</summary>
        <ul>
          {document.history.length ? (
            document.history.map((entry, index) => (
              <li key={index}>
                {entry.name} · game {entry.game_id}
                <br />
                {ACTION_LABELS[entry.previous]} → {ACTION_LABELS[entry.action]}
                <br />
                <small>
                  <ControlTime
                    value={entry.recorded_at}
                    timezone={document.timezone}
                  />
                </small>
              </li>
            ))
          ) : (
            <li className="control-muted">No manual corrections yet.</li>
          )}
        </ul>
      </details>
      <details className="control-help">
        <summary>How it works</summary>
        <p>
          <strong>Wait</strong> · Undecided.
        </p>
        <p>
          <strong>Lock</strong> · Already locked in Sleeper.
        </p>
        <p>
          <strong>Pass</strong> · Choose not to lock. Substitution optional.
        </p>
        <p>Saving updates the plan, not your Sleeper lineup.</p>
      </details>
      <footer className="control-footer">
        <span className="control-muted">Sleeper · Lock-In</span>
        <form
          method="post"
          action="/manage/api/logout"
          onSubmit={(event) => onSubmit(event, "logout")}
        >
          <input type="hidden" name="csrf" value={document.csrf} />
          <button className="control-text-button" type="submit" disabled={busy}>
            Sign out ↗
          </button>
        </form>
      </footer>
    </>
  );
}
