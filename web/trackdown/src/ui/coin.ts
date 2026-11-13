// Verdict coin: Blender-rendered 48-frame flip (frame 0 = SHILL, 12 = edge, 24 = CHILL).
import type { Status } from "../types";
import { STATUS } from "./format";

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

/** Toss the coin over the stage, land on the verdict, then fly it into `target`. */
export async function tossCoin(stage: HTMLElement, status: Status, target: HTMLElement | null, skip: () => boolean): Promise<void> {
  await preloadCoin();
  const size = window.innerWidth < 860 ? 200 : 260;
  const wrap = document.createElement("div");
  wrap.className = "coin-stage";
  const v = STATUS[status];
  wrap.style.setProperty("--vc", v.css);
  wrap.innerHTML = `<div class="coin-flash"></div><div class="coin-sprite"></div><div class="coin-caption" style="opacity:0"><div class="big" style="color:${v.css}"><i>${v.word}.</i></div><div class="small">${v.tag}</div></div>`;
  stage.appendChild(wrap);
  const sprite = wrap.querySelector<HTMLElement>(".coin-sprite")!;
  const cap = wrap.querySelector<HTMLElement>(".coin-caption")!;
  const flash = wrap.querySelector<HTMLElement>(".coin-flash")!;
  sprite.style.backgroundImage = `url(${SHEET})`;
  sprite.style.backgroundSize = `${size * COLS}px ${size * 6}px`;
  sprite.style.width = sprite.style.height = `${size}px`;

  const land = status === "coordinated" ? 0 : status === "likely_organic" ? 24 : 12;
  const total = FRAMES * 4 + land; // four full flips, then the verdict
  const dur = 2100;
  const t0 = performance.now();
  await new Promise<void>((done) => {
    const tick = (now: number) => {
      const t = Math.min(1, (now - t0) / dur);
      if (skip()) return done();
      const e = 1 - Math.pow(1 - t, 3.2);
      setFrame(sprite, e * total, size);
      // toss arc: rise toward camera then fall back
      const h = Math.sin(Math.PI * Math.min(1, t * 1.05));
      const sc = 0.55 + 0.75 * h * (1 - t * 0.5) + t * 0.2;
      sprite.style.transform = `translateY(${(-h * 120).toFixed(1)}px) scale(${sc.toFixed(3)})`;
      if (t < 1) requestAnimationFrame(tick);
      else done();
    };
    requestAnimationFrame(tick);
  });
  // landing
  sprite.style.transition = "transform .5s cubic-bezier(.34,1.56,.64,1)";
  sprite.style.transform = "translateY(0) scale(1)";
  flash.animate([{ opacity: 0.55 }, { opacity: 0 }], { duration: 700, easing: "ease-out" });
  cap.animate([{ opacity: 0, transform: "translate(-50%, 16px)" }, { opacity: 1, transform: "translate(-50%, 0)" }], { duration: 600, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
  if (status === "insufficient") {
    // landed on its edge: wobble between frames 9..15 and settle
    const w0 = performance.now();
    await new Promise<void>((done) => {
      const tick = (now: number) => {
        const t = (now - w0) / 1400;
        if (t >= 1 || skip()) return (setFrame(sprite, 12, size), done());
        setFrame(sprite, 12 + Math.sin(t * 22) * 3.5 * (1 - t), size);
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }
  await wait(skip() ? 0 : 1200);
  // fly into the panel
  if (target && target.getBoundingClientRect().width > 0) {
    const a = sprite.getBoundingClientRect();
    const b = target.getBoundingClientRect();
    const dx = b.left + b.width / 2 - (a.left + a.width / 2);
    const dy = b.top + b.height / 2 - (a.top + a.height / 2);
    const s = b.width / a.width;
    sprite.style.transition = "transform .75s cubic-bezier(.65,0,.35,1), opacity .2s .65s";
    sprite.style.transform = `translate(${dx}px, ${dy}px) scale(${s})`;
    sprite.style.opacity = "0";
    cap.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, fill: "forwards" });
    await wait(780);
  } else {
    wrap.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 400, fill: "forwards" });
    await wait(400);
  }
  wrap.remove();
}

export const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
