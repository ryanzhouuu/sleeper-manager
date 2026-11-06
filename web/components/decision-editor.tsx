/** The editor preserves the Worker's indexed batch form and correction history. */
import { useState, type FormEvent } from "react";
import type { ControlOperation, ManagerDocument } from "@/lib/control-types";
import { ACTION_LABELS } from "@/lib/control-document";
import { ControlIcon, ControlTime, DecisionCard } from "./decision-card";
import { ControlStatus } from "./control-status";
import { InfoTabs } from "./info-tabs";
import { useNow } from "./use-now";
import {
  dayKey,
  groupDecisionsByDay,
  saveSummary,
} from "@/lib/decision-display";

const SAVE_FORM_ID = "decision-form";

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
  const games = [...document.active, ...document.saved, ...document.reopenable];
  const now = useNow();
  const today = now === null ? null : dayKey(now, document.timezone);
  const [edits, setEdits] = useState({ batch: "", count: 0 });
  const changed = edits.batch === document.batch_id ? edits.count : 0;

  /** Server-rendered defaults mark each game's saved choice, so changes need no extra state. */
  function trackEdits(event: FormEvent<HTMLFormElement>) {
    const checked = event.currentTarget.querySelectorAll<HTMLInputElement>(
      'input[type="radio"]:checked',
    );
    setEdits({
      batch: document.batch_id,
      count: [...checked].filter((input) => !input.defaultChecked).length,
    });
    onEdit();
  }
  return (
    <>
      <div className="decision-layout">
        <div className="decision-plan-area">
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
            <section className="control-plan control-plan--ready">
              <p className="control-eyebrow">Team plan</p>
              <h2 className="control-plan-title">{document.plan.title}</h2>
              <pre>{document.plan.message}</pre>
            </section>
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
        </div>
        <div className="decision-main">
          {games.length ? (
            <form
              id={SAVE_FORM_ID}
              key={document.batch_id}
              method="post"
              action="/manage/api/save"
              onSubmit={(event) => onSubmit(event, "save")}
              onChange={trackEdits}
            >
              <fieldset disabled={busy} className="control-fieldset">
                <input type="hidden" name="csrf" value={document.csrf} />
                <input
                  type="hidden"
                  name="batch_id"
                  value={document.batch_id}
                />
                <input type="hidden" name="count" value={games.length} />
                <h2 className="control-section-title">
                  To review <span>{document.active.length}</span>
                </h2>
                {document.active.length ? (
                  groupDecisionsByDay(document.active, document.timezone).map(
                    (day) => (
                      <div className="decision-day" key={day.key}>
                        <h3 className="decision-day-title">
                          {day.key === today
                            ? `Today · ${day.label}`
                            : day.label}
                        </h3>
                        {day.games.map(({ game, index }) => (
                          <DecisionCard
                            game={game}
                            index={index}
                            timezone={document.timezone}
                            now={now}
                            key={`${game.player_id}:${game.game_id}`}
                          />
                        ))}
                      </div>
                    ),
                  )
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
                {document.reopenable.length ? (
                  <details>
                    <summary>
                      Marked ineligible · {document.reopenable.length}
                    </summary>
                    <p className="control-muted">
                      Use this if Sleeper shows a lock for the game.
                    </p>
                    {document.reopenable.map((game, index) => (
                      <DecisionCard
                        game={game}
                        index={
                          document.active.length + document.saved.length + index
                        }
                        timezone={document.timezone}
                        key={`${game.player_id}:${game.game_id}`}
                      />
                    ))}
                  </details>
                ) : null}
              </fieldset>
            </form>
          ) : (
            <section className="decision-card control-muted">
              No lockable games yet.
            </section>
          )}
        </div>
        <aside className="decision-side" aria-label="Save and week details">
          {games.length ? (
            <div className="control-save-bar">
              <small data-changed={changed > 0}>
                {saveSummary(changed, dirty)}
              </small>
              <button type="submit" form={SAVE_FORM_ID} disabled={busy}>
                Save &amp; replan <ControlIcon kind="arrow" />
              </button>
            </div>
          ) : null}
          <InfoTabs
            label="Week details"
            tabs={[
              {
                id: "past",
                label: `Past · ${document.past.length}`,
                content: document.past.length ? (
                  <ul>
                    {document.past.map((game, index) => (
                      <li key={index}>
                        {game.name} ·{" "}
                        <ControlTime
                          value={game.start}
                          timezone={document.timezone}
                        />{" "}
                        · {game.score.toFixed(2)} pts
                        <br />
                        <span className="control-muted">{game.state}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="control-muted">No finished games yet.</p>
                ),
              },
              {
                id: "history",
                label: "History",
                content: document.history.length ? (
                  <ul>
                    {document.history.map((entry, index) => (
                      <li key={index}>
                        {entry.name} · game {entry.game_id}
                        <br />
                        {ACTION_LABELS[entry.previous]} →{" "}
                        {ACTION_LABELS[entry.action]}
                        <br />
                        <small>
                          <ControlTime
                            value={entry.recorded_at}
                            timezone={document.timezone}
                          />
                        </small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="control-muted">No manual corrections yet.</p>
                ),
              },
              {
                id: "help",
                label: "How it works",
                content: (
                  <div className="control-help">
                    <p>
                      <strong>Planner suggests</strong> · What the planner wants
                      you to do next in Sleeper.
                    </p>
                    <p>
                      <strong>Recorded</strong> · What the planner believes has
                      already happened. Use the buttons to correct it.
                    </p>
                    <p>
                      <strong>Not yet</strong> · You have not locked or passed.
                    </p>
                    <p>
                      <strong>Locked</strong> · You locked this game in Sleeper.
                    </p>
                    <p>
                      <strong>Passed</strong> · You chose not to lock.
                      Substitution optional.
                    </p>
                    <p>Saving updates the plan, not your Sleeper lineup.</p>
                  </div>
                ),
              },
            ]}
          />
        </aside>
      </div>
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
