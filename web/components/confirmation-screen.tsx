"use client";

/** Opening a notification displays intent; only submitting the form consumes its token. */
import Link from "next/link";
import { useState, type FormEvent } from "react";
import type { ControlDocument } from "@/lib/control-types";
import { encodeControlForm, requestControl } from "@/lib/control-client";
import { ControlNotice, ControlStatus } from "./control-status";

export function ConfirmationScreen({ initial }: { initial: ControlDocument }) {
  const [document, setDocument] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const body = encodeControlForm(event.currentTarget);
    setBusy(true);
    setNotice("");
    try {
      setDocument((await requestControl("/ack/submit", body)).data);
    } catch {
      setNotice(
        "Could not confirm the result. Open this link again to check its saved status.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {busy ? <ControlStatus>Recording your decision…</ControlStatus> : null}
      {notice ? (
        <p className="control-notice" role="alert">
          {notice}
        </p>
      ) : null}
      {document.status === "confirmation" ? (
        <>
          <h1 className="control-title">{document.heading}</h1>
          <section className="block">
            <h2 className="label">{document.title}</h2>
            <pre>{document.message}</pre>
          </section>
          <p className="control-muted">{document.instruction}</p>
          <form method="post" action="/ack/submit" onSubmit={submit}>
            <input type="hidden" name="token" value={document.token} />
            <input type="hidden" name="action" value={document.action} />
            <button type="submit" disabled={busy}>
              {document.label}
            </button>
          </form>
        </>
      ) : (
        <ControlNotice
          title={
            document.status === "message"
              ? document.title
              : "Confirmation unavailable"
          }
          message={document.status === "message" ? document.message : undefined}
        />
      )}
      <Link className="controls-link" href="/manage">
        Review or correct decisions →
      </Link>
    </>
  );
}
