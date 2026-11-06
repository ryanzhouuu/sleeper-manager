import { useSyncExternalStore } from "react";

const TICK_MS = 30_000;

function subscribe(onTick: () => void): () => void {
  const id = setInterval(onTick, TICK_MS);
  return () => clearInterval(id);
}

/** Snap to the tick so repeated reads within one interval return the same snapshot. */
function currentTick(): number {
  return Math.floor(Date.now() / TICK_MS) * TICK_MS;
}

/** Null during server rendering and hydration so relative times never mismatch the markup. */
export function useNow(): number | null {
  return useSyncExternalStore(subscribe, currentTick, () => null);
}
