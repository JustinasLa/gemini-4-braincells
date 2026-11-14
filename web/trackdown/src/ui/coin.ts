// Verdict coin stills (panel badge). The coin is not animated.
import type { Status } from "../types";

const base = import.meta.env.BASE_URL;
export const coinImg = (s: Status) => `${base}coin/coin-${s === "coordinated" ? "shill" : s === "likely_organic" ? "chill" : "unsure"}.webp`;

export function preloadCoin() {
  for (const s of ["coordinated", "likely_organic", "insufficient"] as Status[]) new Image().src = coinImg(s);
}

export const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
