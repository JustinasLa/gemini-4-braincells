// Width of the right panel, which the user drags from its left edge. Pure so it can be tested.
export const PANEL_MIN = 320;
const PANEL_MAX = 960;
const CANVAS_MIN = 380; // the canvas keeps at least this much room to read

export function panelMax(viewport: number) {
  return Math.max(PANEL_MIN, Math.min(PANEL_MAX, viewport - CANVAS_MIN));
}

export function clampPanel(w: number, viewport: number) {
  return Math.round(Math.min(panelMax(viewport), Math.max(PANEL_MIN, w)));
}

/** Width after a key press on the focused grip, or null when the key doesn't resize. */
export function stepPanel(w: number, key: string, shift: boolean, viewport: number): number | null {
  const step = shift ? 80 : 20;
  if (key === "ArrowLeft") return clampPanel(w + step, viewport);
  if (key === "ArrowRight") return clampPanel(w - step, viewport);
  if (key === "Home") return panelMax(viewport);
  if (key === "End") return PANEL_MIN;
  return null;
}

/** A saved width from storage, or null when there is none or it is garbage. */
export function parseSaved(v: string | null): number | null {
  const n = parseFloat(v ?? "");
  return Number.isFinite(n) && n > 0 ? n : null;
}
