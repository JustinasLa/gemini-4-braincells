// Procedural coats of arms (EU4-style) from a seed string. Follows the rule of tincture:
// a metal field gets colour charges and vice versa. Fresh accounts get a blank shield.
import { hashStr } from "../data/build";

const METALS = ["#d9b45a", "#e6e1d3"]; // or, argent
const COLOURS = ["#a8302a", "#2c4f9a", "#2d6e4c", "#22252a", "#5e3a7f"]; // gules, azure, vert, sable, purpure
const SHIELD = "M3 3 H45 V24 C45 40 33 50 24 55 C15 50 3 40 3 24 Z";

function rnd(seed: number) {
  let s = seed || 1;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return ((s >>> 0) % 10000) / 10000;
  };
}

const mullet = (cx: number, cy: number, r: number, fill: string) => {
  const p: string[] = [];
  for (let i = 0; i < 10; i++) {
    const a = -Math.PI / 2 + (i * Math.PI) / 5;
    const rr = i % 2 ? r * 0.42 : r;
    p.push(`${(cx + Math.cos(a) * rr).toFixed(1)},${(cy + Math.sin(a) * rr).toFixed(1)}`);
  }
  return `<polygon points="${p.join(" ")}" fill="${fill}"/>`;
};

/** SVG markup for a 48x58 shield. `blank` = an account with no history: no arms yet. */
export function arms(seed: string, opts: { blank?: boolean; size?: number } = {}): string {
  const size = opts.size ?? 28;
  const w = size;
  const h = Math.round(size * 1.2);
  const id = `cl${hashStr(seed).toString(36)}`;
  if (opts.blank) {
    return `<svg class="arms blank" width="${w}" height="${h}" viewBox="0 0 48 58"><path d="${SHIELD}" fill="#2a3138" stroke="#7d8790" stroke-width="2" stroke-dasharray="4 3"/><text x="24" y="34" text-anchor="middle" font-family="Chakra Petch, sans-serif" font-size="18" font-weight="600" fill="#9aa3ab">?</text></svg>`;
  }
  const r = rnd(hashStr(seed));
  const metalField = r() < 0.5;
  const pick = (a: string[]) => a[Math.floor(r() * a.length)];
  const field = metalField ? pick(METALS) : pick(COLOURS);
  const charge = metalField ? pick(COLOURS) : pick(METALS);
  const second = metalField ? pick(COLOURS.filter((c) => c !== charge)) : pick(METALS.filter((c) => c !== charge));
  const kind = Math.floor(r() * 10);
  let body = "";
  switch (kind) {
    case 0: body = `<rect x="0" y="0" width="24" height="58" fill="${charge}"/>`; break; // per pale
    case 1: body = `<rect x="0" y="0" width="48" height="26" fill="${charge}"/>`; break; // per fess
    case 2: body = `<polygon points="-4,6 6,-4 52,42 42,52" fill="${charge}"/>`; break; // bend
    case 3: body = `<polyline points="2,46 24,20 46,46" fill="none" stroke="${charge}" stroke-width="9"/>`; break; // chevron
    case 4: body = `<rect x="19" y="0" width="10" height="58" fill="${charge}"/><rect x="0" y="20" width="48" height="10" fill="${charge}"/>`; break; // cross
    case 5: body = `<line x1="0" y1="0" x2="48" y2="58" stroke="${charge}" stroke-width="8"/><line x1="48" y1="0" x2="0" y2="58" stroke="${charge}" stroke-width="8"/>`; break; // saltire
    case 6: body = `<rect x="0" y="0" width="48" height="17" fill="${charge}"/>${mullet(24, 36, 9, charge)}`; break; // chief + mullet
    case 7: body = `<rect x="0" y="0" width="24" height="27" fill="${charge}"/><rect x="24" y="27" width="24" height="31" fill="${charge}"/>`; break; // quarterly
    case 8: body = `<circle cx="24" cy="27" r="11" fill="${charge}"/>${mullet(24, 27, 6, second)}`; break; // roundel
    default: body = `<rect x="0" y="22" width="48" height="11" fill="${charge}"/>${mullet(13, 12, 6, charge)}${mullet(35, 12, 6, charge)}`; // fess + mullets
  }
  return `<svg class="arms" width="${w}" height="${h}" viewBox="0 0 48 58"><defs><clipPath id="${id}"><path d="${SHIELD}"/></clipPath><linearGradient id="${id}g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset=".55" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".3"/></linearGradient></defs><g clip-path="url(#${id})"><rect width="48" height="58" fill="${field}"/>${body}<rect width="48" height="58" fill="url(#${id}g)"/></g><path d="${SHIELD}" fill="none" stroke="#c9a65e" stroke-width="2.4"/><path d="${SHIELD}" fill="none" stroke="#1a1208" stroke-width="0.8" transform="translate(2.2 2.4) scale(.91)"/></svg>`;
}

/** Seed for an author: templated handles (glow_fan_0412) share one coat of arms. */
export function armsSeed(handle: string, templated?: boolean) {
  return templated ? handle.replace(/\d+/g, "#") : handle;
}
