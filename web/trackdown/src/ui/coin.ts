// Verdict coin: Blender-rendered 48-frame flip (frame 0 = SHILL, 12 = edge, 24 = CHILL).
import type { Status } from "../types";

const FRAMES = 48;
const COLS = 8;
const base = import.meta.env.BASE_URL;
export const coinImg = (s: Status) => `${base}coin/coin-${s === "coordinated" ? "shill" : s === "likely_organic" ? "chill" : "unsure"}.webp`;
const SHEET = `${base}coin/coin-sheet.webp`;

let sheetReady: Promise<void> | null = null;
export function preloadCoin() {
  sheetReady ??= new Promise((res) => {
    const im = new Image();
    im.onload = () => res();
    im.onerror = () => res();
    im.src = SHEET;
  });
  for (const s of ["coordinated", "likely_organic", "insufficient"] as Status[]) new Image().src = coinImg(s);
  return sheetReady;
}

function setFrame(el: HTMLElement, f: number, size: number) {
  const i = ((Math.round(f) % FRAMES) + FRAMES) % FRAMES;
  el.style.backgroundPosition = `${-(i % COLS) * size}px ${-Math.floor(i / COLS) * size}px`;
}

export const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Flip the coin inside an element (the event window's illustration). Resolves when it lands. */
export async function flipInto(slot: HTMLElement, status: Status, skip: () => boolean): Promise<void> {
  await preloadCoin();
  const size = slot.getBoundingClientRect().width || 170;
  slot.style.backgroundImage = `url(${SHEET})`;
  slot.style.backgroundSize = `${size * COLS}px ${size * 6}px`;
  const land = status === "coordinated" ? 0 : status === "likely_organic" ? 24 : 12;
  const total = FRAMES * 4 + land;
  const dur = 1900;
  const t0 = performance.now();
  await new Promise<void>((done) => {
    const tick = (now: number) => {
      const t = Math.min(1, (now - t0) / dur);
      if (skip()) return (setFrame(slot, land, size), done());
      const e = 1 - Math.pow(1 - t, 3.2);
      setFrame(slot, e * total, size);
      const h = Math.sin(Math.PI * Math.min(1, t * 1.05));
      slot.style.transform = `translate(-50%, calc(-50% - ${(h * 40).toFixed(1)}px)) scale(${(0.7 + 0.45 * h * (1 - t * 0.6) + t * 0.3).toFixed(3)})`;
      if (t < 1) requestAnimationFrame(tick);
      else done();
    };
    requestAnimationFrame(tick);
  });
  slot.style.transition = "transform .45s cubic-bezier(.34,1.56,.64,1)";
  slot.style.transform = "translate(-50%, -50%) scale(1)";
  if (status === "insufficient") {
    const w0 = performance.now();
    await new Promise<void>((done) => {
      const tick = (now: number) => {
        const t = (now - w0) / 1300;
        if (t >= 1 || skip()) return (setFrame(slot, 12, size), done());
        setFrame(slot, 12 + Math.sin(t * 22) * 3.5 * (1 - t), size);
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }
}
