"use client";

/** The one manual input: whether an unverified starter was in a starting slot at tipoff. */
import { useState, type FormEvent } from "react";
import { submitOverride } from "@/lib/control-client";
import type { OverrideAction } from "@/lib/control-types";

const CHOICES: readonly [OverrideAction, string][] = [
  ["lockable", "Was starting at tipoff"],
  ["not_lockable", "Was benched at tipoff"],
];

type Phase =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "login"; action: OverrideAction; rejected: boolean }
  | { kind: "error"; message: string };

export function OverrideControl({
  playerId,
  gameId,
  version,
}: {
  playerId: string;
  gameId: string;
  version: number;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  async function answer(action: OverrideAction, password?: string) {
    setPhase({ kind: "saving" });
    try {
      const result = await submitOverride(
        { playerId, gameId, version, action },
        password,
      );
      if (result.status === "saved") {
        window.location.reload();
        return;
      }
      setPhase(
        result.status === "login"
          ? { kind: "login", action, rejected: result.rejected === true }
          : { kind: "error", message: result.message },
      );
    } catch {
      setPhase({ kind: "error", message: "Could not reach your league." });
    }
  }

  function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (phase.kind !== "login") return;
    const password = new FormData(event.currentTarget).get("password");
    void answer(phase.action, typeof password === "string" ? password : "");
  }

  if (phase.kind === "login") {
    return (
      <form className="override" onSubmit={signIn}>
        {phase.rejected ? <p role="alert">Access key not accepted.</p> : null}
        <label>
          access key{" "}
          <input
            type="password"
            name="password"
            required
            maxLength={256}
            autoComplete="current-password"
          />
        </label>
        <button type="submit">continue</button>
      </form>
    );
  }
  return (
    <div className="override" role="group" aria-label="Tipoff eligibility">
      {CHOICES.map(([action, label]) => (
        <button
          key={action}
          type="button"
          disabled={phase.kind === "saving"}
          onClick={() => void answer(action)}
        >
          {label}
        </button>
      ))}
      {phase.kind === "saving" ? <span role="status">saving…</span> : null}
      {phase.kind === "error" ? <p role="alert">{phase.message}</p> : null}
    </div>
  );
}
