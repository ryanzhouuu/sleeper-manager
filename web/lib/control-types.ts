/** Private control documents are independent of the public dashboard response. */
export type DecisionAction =
  "not_locked" | "locked" | "passed" | "not_lockable" | "lockable";
export type ControlOperation = "login" | "save" | "replan" | "logout";

export interface DecisionGame {
  player_id: string;
  game_id: string;
  version: number;
  name: string;
  placement: string;
  start: string;
  deadline: string;
  score: number | null;
  action: DecisionAction;
  choices: DecisionAction[];
  advisor: string;
  saved: string;
  portrait_url: string | null;
  team: string | null;
  team_image_url: string | null;
}

export interface ManagerDocument {
  status: "ready";
  week: number;
  timezone: string;
  csrf: string;
  batch_id: string;
  request_id: string;
  pending: boolean;
  plan: { title: string; message: string } | null;
  active: DecisionGame[];
  saved: DecisionGame[];
  reopenable: DecisionGame[];
  past: { name: string; start: string; score: number; state: string }[];
  history: {
    name: string;
    game_id: string;
    previous: DecisionAction;
    action: DecisionAction;
    recorded_at: string;
  }[];
}

export type ControlDocument =
  | ManagerDocument
  | { status: "login"; rejected: boolean }
  | { status: "message"; title: string; message?: string }
  | { status: "saved" };
