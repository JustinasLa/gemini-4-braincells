// Small helpers to author simulated trackdowns deterministically.
import type { Author, Media, Platform, Relation, Role, Tool, TraceEdge, TraceNode } from "../types";

function rng(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return ((s >>> 0) % 1_000_000) / 1_000_000;
  };
}

export function hashStr(str: string): number {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export const T = (iso: string) => Date.parse(iso);
export const SEC = 1000;
export const MIN = 60 * SEC;
export const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;

const FIRST = ["mira", "jonas", "aino", "leo", "sara", "eli", "noor", "tomas", "ilse", "ravi", "hanna", "otto", "lumi", "kai", "vera", "sami", "ada", "bruno", "iris", "niko", "mae", "theo", "juno", "pia", "rasmus", "liv", "emil", "nella", "aksel", "dora"];
const LAST = ["koski", "lind", "berg", "salo", "rowe", "hart", "vale", "moss", "kade", "ness", "ruiz", "park", "dahl", "frost", "holm", "sun", "wren", "lake", "fern", "stone"];
const WORDS = ["truth", "wave", "daily", "patriot", "real", "voice", "wake", "free", "signal", "nordic", "news", "watch", "insider", "facts", "loud", "clear"];

export class Builder {
  nodes: TraceNode[] = [];
  edges: TraceEdge[] = [];
  r: () => number;
  private n = 0;
  constructor(seed: number) {
    this.r = rng(seed);
  }
  pick<T>(a: T[]): T {
    return a[Math.floor(this.r() * a.length)];
  }
  int(a: number, b: number) {
    return Math.floor(a + this.r() * (b - a + 1));
  }
  person(opts: Partial<Author> = {}): Author {
    const f = this.pick(FIRST);
    const l = this.pick(LAST);
    const name = f[0].toUpperCase() + f.slice(1) + " " + l[0].toUpperCase() + l.slice(1);
    return {
      name,
      handle: `${f}${this.r() < 0.5 ? "." + l : l.slice(0, 3)}.bsky.social`,
      hue: this.int(0, 359),
      ageDays: this.int(300, 2600),
      followers: this.int(80, 4200),
      ...opts,
    };
  }
  /** Fresh, low-follower account with a word-salad handle. Describes behaviour only. */
  fresh(opts: Partial<Author> = {}): Author {
    const w1 = this.pick(WORDS);
    const w2 = this.pick(WORDS.filter((w) => w !== w1));
    const num = this.int(10, 9999);
    const f = this.pick(FIRST);
    return {
      name: this.r() < 0.5 ? `${w1[0].toUpperCase() + w1.slice(1)} ${w2[0].toUpperCase() + w2.slice(1)}` : f[0].toUpperCase() + f.slice(1) + " " + num,
      handle: `${this.r() < 0.5 ? w1 + "." + w2 : f + "_" + num}.bsky.social`,
      hue: this.int(340, 380) % 360,
      ageDays: this.int(1, 9),
      followers: this.int(0, 14),
      ...opts,
    };
  }
  add(p: {
    t: number;
    platform: Platform;
    role: Role;
    author: Author;
    text: string;
    media?: Media;
    url?: string;
    reposts?: number;
    likes?: number;
    cluster?: string;
    flags?: string[];
    id?: string;
  }): string {
    const id = p.id ?? `n${++this.n}`;
    this.nodes.push({ ...p, id });
    return id;
  }
  link(from: string, to: string, rel: Relation, opts: { sim?: number; tool?: Tool; note?: string } = {}) {
    this.edges.push({ from, to, rel, ...opts });
  }
}

