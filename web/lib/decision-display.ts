/** Owner-facing wording for Worker decision states; values sent back to the Worker never change. */
export type AdviceTone = "lock" | "pass" | "wait" | "none";

export interface Advice {
  tone: AdviceTone;
  text: string;
}

/** The advisor names a Sleeper action to take, while the owner's choice records one already taken. */
export function adviceFor(advisor: string): Advice {
  switch (advisor) {
    case "Lock":
      return { tone: "lock", text: "Lock this game in Sleeper" };
    case "Pass":
      return { tone: "pass", text: "Pass on this game" };
    case "Wait":
      return { tone: "wait", text: "Hold for now" };
    case "Unavailable":
      return { tone: "none", text: "No advice right now" };
    default:
      return { tone: "none", text: advisor };
  }
}

/** "Wait" is the Worker's default recorded state and repeats the selected choice. */
export function recordedNote(saved: string): string | null {
  return saved === "Wait" ? null : saved;
}
