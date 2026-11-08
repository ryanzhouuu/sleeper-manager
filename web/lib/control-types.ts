/** Private owner-session documents, independent of the public dashboard response. */
export type ControlOperation = "login" | "replan" | "logout" | "override";
export type OverrideAction = "lockable" | "not_lockable";

export type ControlDocument =
  | { status: "session"; signed_in: boolean; csrf: string | null }
  | { status: "login"; rejected: boolean }
  | { status: "message"; title: string; message?: string }
  | { status: "saved" };
