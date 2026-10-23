"use client";

/** Client state owns pending feedback; decision eligibility and saving remain on the Worker. */
import { useEffect, useState, type FormEvent } from "react";
import {
  encodeControlForm,
  pollManager,
  requestControl,
  updateManager,
} from "@/lib/control-client";
import type { ControlDocument, ControlOperation } from "@/lib/control-types";
import { ControlNotice, ControlStatus } from "./control-status";
import { DecisionEditor } from "./decision-editor";

export function ManagerScreen({ initial }: { initial: ControlDocument }) {
  const [document, setDocument] = useState(initial);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [dirty, setDirty] = useState(false);
  const [paused, setPaused] = useState(false);
  const [needsCheck, setNeedsCheck] = useState(false);
  const pending = document.status === "ready" && document.pending;

  useEffect(() => {
    if (!pending || dirty || busy || paused || needsCheck) return;
    const controller = new AbortController();
    pollManager(controller.signal, setDocument)
      .then((result) => {
        if (!controller.signal.aborted && result === "paused") setPaused(true);
      })
      .catch(() => {
        if (!controller.signal.aborted) setPaused(true);
      });
    return () => controller.abort();
  }, [pending, dirty, busy, paused, needsCheck]);

  /** A failed refresh leaves the current form and its selections intact. */
  async function refresh(checkSaved = false) {
    if (busy || (dirty && !checkSaved)) return;
    setBusy("Refreshing decisions…");
    try {
      const result = await requestControl("/manage/api");
      if (result.code >= 500) throw new Error("Unavailable");
      setDocument(result.data);
      setPaused(false);
      setDirty(false);
      setNeedsCheck(false);
      setNotice("");
    } catch {
      setNeedsCheck(true);
      setNotice(
        "Could not refresh. Your choices are still here. Try again shortly.",
      );
    } finally {
      setBusy("");
    }
  }

  /** Capture the form before disabling it and never replay a rejected submission. */
  async function submit(
    event: FormEvent<HTMLFormElement>,
    operation: ControlOperation,
  ) {
    event.preventDefault();
    if (busy) return;
    const body = encodeControlForm(event.currentTarget);
    setBusy(
      operation === "login"
        ? "Signing in…"
        : operation === "logout"
          ? "Signing out…"
          : "Saving decisions…",
    );
    setNotice("");
    try {
      const result = await updateManager(operation, body, setBusy);
      if (result.data) {
        setDocument(result.data);
        setDirty(false);
        setPaused(false);
        setNeedsCheck(false);
      }
      setNotice(result.notice);
    } catch {
      setNeedsCheck(true);
      setNotice(
        "Could not confirm the result. Refresh to check the saved status before trying again.",
      );
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <header className="control-heading">
        <h1>Game decisions</h1>
        {document.status === "ready" ? (
          <p className="control-muted">
            W{document.week} · {document.timezone}
          </p>
        ) : null}
      </header>
      {busy ? <ControlStatus>{busy}</ControlStatus> : null}
      {notice ? (
        <div className="control-notice" role="status">
          {notice}
          {needsCheck ? (
            <button
              className="control-text-button"
              type="button"
              onClick={() => refresh(true)}
              disabled={Boolean(busy)}
            >
              Check saved status ↻
            </button>
          ) : null}
        </div>
      ) : null}
      {document.status === "ready" ? (
        <DecisionEditor
          document={document}
          busy={Boolean(busy) || needsCheck}
          dirty={dirty}
          paused={paused}
          onEdit={() => setDirty(true)}
          onSubmit={submit}
          onRefresh={() => refresh()}
        />
      ) : document.status === "login" ? (
        <section className="block">
          <h2 className="label">Owner access</h2>
          {document.rejected && !notice ? (
            <p role="alert">Access key not accepted.</p>
          ) : null}
          <form
            className="control-login"
            method="post"
            action="/manage/api/login"
            onSubmit={(event) => submit(event, "login")}
          >
            <label>
              Access key
              <input
                type="password"
                name="password"
                required
                autoComplete="current-password"
                maxLength={256}
                disabled={Boolean(busy)}
              />
            </label>
            <button type="submit" disabled={Boolean(busy)}>
              Continue <span aria-hidden="true">→</span>
            </button>
          </form>
        </section>
      ) : (
        <>
          <ControlNotice
            title={
              document.status === "message"
                ? document.title
                : "Controls unavailable"
            }
            message={
              document.status === "message" ? document.message : undefined
            }
          />
          <button
            type="button"
            onClick={() => refresh()}
            disabled={Boolean(busy)}
          >
            Try again
          </button>
        </>
      )}
    </>
  );
}
