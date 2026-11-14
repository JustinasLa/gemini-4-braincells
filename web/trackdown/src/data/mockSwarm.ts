// Development mock of the meme swarm API (`?mockSwarm`, dev server only): a wall, three detail
// pages (a clear swarm, an organic spread, too few to tell) and the swarm fields of a lookup.
// Every picture is drawn here as a data URL; every account and link is fictional.
import type { SwarmAction, SwarmCard, SwarmDetail, SwarmPost, SwarmReport, SwarmSignal, SwarmTimelinePoint, SwarmVerdict, SwarmWall } from "./swarm";

type Case = "swarm" | "organic" | "unclear";

function rng(seed: number) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const PALETTES = [
  ["#2b1d3a", "#e2a6c8", "#f7e3b0"], ["#10263a", "#6fb3d9", "#f2efe9"], ["#33240f", "#e4b36a", "#fff3d6"],
  ["#1a2f22", "#7fd19b", "#eaf7d8"], ["#3a1414", "#ef7d62", "#ffe1c9"], ["#1c1c28", "#a99cf0", "#f0ecff"],
];
const CAPTIONS = ["ME AT 3 AM", "MONDAY AGAIN", "IT'S FINE", "TRUST ME BRO", "SKILL ISSUE", "TOUCH GRASS", "ONE MORE EPISODE", "VIBES ONLY", "NOT THIS AGAIN", "CERTIFIED CLASSIC", "NEW PHONE WHO DIS", "BIG IF TRUE", "WAIT FOR IT", "NO THOUGHTS"];

/** a square placeholder meme: a flat scene, a face, a caption bar */
export function mockPicture(i: number, size = 600): string {
  const [bg, mid, light] = PALETTES[i % PALETTES.length];
  const caption = CAPTIONS[i % CAPTIONS.length];
  const r = rng(i * 97 + 3);
  const cx = 200 + r() * 200, cy = 330 + r() * 60;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 600 600">
<rect width="600" height="600" fill="${bg}"/>
<path d="M0 ${470 + r() * 40} ${150 + r() * 80} ${380 + r() * 40} ${330 + r() * 60} ${450 + r() * 30} ${470 + r() * 60} ${360 + r() * 40} 600 ${430 + r() * 40}V600H0z" fill="${mid}" opacity=".55"/>
<circle cx="${cx}" cy="${cy}" r="${95 + r() * 30}" fill="${light}"/>
<circle cx="${cx - 34}" cy="${cy - 18}" r="11" fill="${bg}"/><circle cx="${cx + 34}" cy="${cy - 18}" r="11" fill="${bg}"/>
<path d="M${cx - 40} ${cy + 34}q40 ${r() > 0.5 ? 30 : -18} 80 0" stroke="${bg}" stroke-width="9" fill="none" stroke-linecap="round"/>
<rect x="24" y="28" width="552" height="86" fill="#000" opacity=".82"/>
<text x="300" y="86" text-anchor="middle" fill="#fff" font-family="Impact, 'Arial Black', sans-serif" font-size="48" letter-spacing="1">${caption}</text>
<text x="24" y="580" fill="${light}" opacity=".7" font-family="monospace" font-size="16">MOCK · FICTIONAL</text>
</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

interface Sighting extends SwarmTimelinePoint { text: string }
const WORDS = ["lmao this is me", "why is this so accurate", "sending this to everyone", "not me laughing at this", "ok this one got me", "the way I screamed", "every single time", "who made this", "I feel attacked", "this is the funniest thing today"];
const COPY = ["", "This is what they don't want you to see. Share before it's gone!", "Everyone needs to see this right now 🔥"];

function timeline(kind: Case, now: number): Sighting[] {
  const r = rng(kind === "swarm" ? 11 : kind === "organic" ? 23 : 37);
  const out: Sighting[] = [];
  let next = 0;
  const post = (t: number, k: SwarmAction = "post", g = 0, burst = false, a = next++) =>
    out.push({ t: Math.round(t), a, k, g, burst, text: g ? COPY[g] : WORDS[Math.floor(r() * WORDS.length)] });
  const boost = (t: number, burst = false) => post(t, (["repost", "like", "like", "quote", "reply"] as const)[Math.floor(r() * 5)], 0, burst);
  if (kind === "swarm") {
    const start = now - 20 * 3600;
    for (let i = 0; i < 12; i++) { const t = start + r() * 8 * 3600; post(t); if (r() < 0.5) boost(t + r() * 1800); }
    const b0 = start + 9 * 3600;
    for (let i = 0; i < 40; i++) post(b0 + i * 6 + r() * 4, "post", i % 3 === 0 && i < 36 ? 1 : i % 5 === 1 ? 2 : 0, true, 12 + (i % 31));
    for (let i = 0; i < 52; i++) boost(b0 + 20 + r() * 260, true);
    for (let i = 0; i < 8; i++) post(b0 + 1800 + r() * 10 * 3600);
  } else if (kind === "organic") {
    const start = now - 18 * 3600;
    let t = start;
    for (let i = 0; i < 70; i++) {
      t += -Math.log(1 - r()) * 900;
      const dense = i >= 30 && i < 36;
      post(dense ? start + 7 * 3600 + (i - 30) * 250 : t, "post", 0, dense);
      if (r() < 0.35) boost(t + r() * 3600);
    }
  } else {
    const start = now - 3 * 3600;
    [0, 1, 0, 2, 1].forEach((a, i) => post(start + i * 2400 + r() * 600, "post", i === 1 || i === 4 ? 1 : 0, false, a));
    boost(start + 2000); boost(start + 7000);
  }
  return out.sort((a, b) => a.t - b.t);
}

const sig = (family: SwarmSignal["family"], name: string, score: number, summary: string, polarity: SwarmSignal["polarity"] = "for"): SwarmSignal => ({ family, name, score, polarity, summary, numbers: {}, examples: [] });
const iso = (t: number) => new Date(t * 1000).toISOString();

function report(kind: Case, basis: "live" | "sample", now: number): SwarmReport {
  const points = timeline(kind, now);
  const posts = points.filter((p) => p.k === "post");
  const burstPts = points.filter((p) => p.burst);
  const accounts = new Set(posts.map((p) => p.a)).size;
  const verdict: SwarmVerdict = kind === "swarm" ? "swarm" : kind === "organic" ? "organic" : "unclear";
  const signals: SwarmSignal[] = kind === "swarm"
    ? [sig("timing", "burst", 0.93, "31 accounts in 4 min"), sig("copy", "same_text", 0.86, "same words ×12"), sig("accounts", "new_accounts", 0.72, "24 accounts made this week"), sig("timing", "regular_gaps", 0.61, "posts 6 s apart"), sig("boost", "boost_ring", 0.58, "52 boosts in 5 min"), sig("accounts", "old_accounts", 0.18, "5 accounts over 2 years old", "against")]
    : kind === "organic"
      ? [sig("timing", "burst", 0.22, "6 accounts in 25 min"), sig("timing", "spread", 0.81, "spread over 15 h", "against"), sig("copy", "own_words", 0.76, "64 different captions", "against"), sig("accounts", "old_accounts", 0.63, "most accounts 3+ years old", "against"), sig("boost", "many_boosters", 0.41, "boosts from 23 accounts", "against")]
      : [sig("copy", "same_text", 0.31, "same words ×2")];
  return {
    verdict, why_unclear: kind === "unclear" ? "only 3 accounts" : "",
    score: kind === "swarm" ? 0.94 : kind === "organic" ? 0.12 : 0.3, guard: kind === "organic" ? 0.81 : 0.18, p_value: kind === "swarm" ? 0.0004 : null,
    families: kind === "swarm" ? { timing: 0.93, copy: 0.86, accounts: 0.72, boost: 0.58 } : { timing: 0.22 },
    fired: kind === "swarm" ? ["timing", "copy", "accounts", "boost"] : [],
    signals, n_posts: posts.length, n_boosts: points.length - posts.length, n_accounts: accounts, n_accounts_known: accounts,
    first_at: iso(points[0].t), last_at: iso(points[points.length - 1].t),
    burst: burstPts.length ? { start: iso(burstPts[0].t), end: iso(burstPts[burstPts.length - 1].t), n_posts: burstPts.filter((p) => p.k === "post").length, n_accounts: new Set(burstPts.filter((p) => p.k === "post").map((p) => p.a)).size } : null,
    timeline: points.map(({ t, a, k, g, burst }) => ({ t, a, k, g, burst })),
    basis,
    basis_note: basis === "live" ? `${posts.length} posts our Bluesky feed saw in 24 h` : `Search sample: ${posts.length} posts, copies missing`,
    version: "mock",
  };
}

const HANDLES = ["ana", "brightfox", "milo", "sunnyday", "kettle", "orbit", "pixelpine", "juniper", "nightowl", "wren", "quill", "harbor", "tulip", "ember", "cobalt"];
function posts(kind: Case, now: number): SwarmPost[] {
  return timeline(kind, now).filter((p) => p.k === "post").slice(0, 30).map((p, i) => ({
    url: `https://bsky.example/profile/mock-${HANDLES[p.a % HANDLES.length]}${p.a}/post/${i}`, platform: "bluesky",
    handle: `${HANDLES[p.a % HANDLES.length]}${p.a}.bsky.example`, at: iso(p.t), text: p.text,
  }));
}

const CASE_OF: Record<string, Case> = { "mock-swarm": "swarm", "mock-organic": "organic", "mock-unclear": "unclear" };
const nowS = () => Math.floor(Date.now() / 1000);

function card(id: string, i: number, kind: Case, accounts: number, now: number, thumb = mockPicture(i, 300)): SwarmCard {
  const r = report(kind, "live", now);
  return { id, thumb, verdict: r.verdict, score: r.score, n_posts: Math.round(accounts * 1.4), n_accounts: accounts, first_at: r.first_at, last_at: r.last_at, families: r.families, headline: r.signals[0]?.summary ?? "", kind: "meme" };
}

export function mockWall(): SwarmWall {
  const now = nowS();
  const cards = [
    card("mock-swarm", 0, "swarm", 43, now), card("mock-w1", 1, "swarm", 38, now), card("mock-w2", 2, "swarm", 31, now),
    card("mock-w3", 3, "swarm", 27, now), card("mock-w4", 4, "swarm", 22, now),
    card("mock-broken", 5, "swarm", 19, now, `${location.origin}/__meme_missing__/thumb.png`), // a dead image: its tile hides
    card("mock-unclear", 6, "unclear", 3, now), card("mock-w7", 7, "unclear", 6, now),
    card("mock-organic", 8, "organic", 64, now), card("mock-w9", 9, "organic", 51, now), card("mock-w10", 10, "organic", 40, now),
    card("mock-w11", 11, "organic", 33, now), card("mock-w12", 12, "organic", 25, now), card("mock-w13", 13, "organic", 18, now),
  ];
  return { items: cards, as_of: iso(now), window_h: 24 };
}

export function mockDetail(id: string): SwarmDetail | null {
  if (!id.startsWith("mock-") || id === "mock-broken") return null;
  const kind = CASE_OF[id] ?? "swarm";
  const now = nowS();
  const i = Math.max(0, mockWall().items.findIndex((c) => c.id === id));
  const r = report(kind, "live", now);
  return { ...card(id, i, kind, r.n_accounts, now), n_posts: r.n_posts, picture: mockPicture(i), report: r, posts: posts(kind, now), caption: CAPTIONS[i % CAPTIONS.length] };
}

/** the swarm fields of a mock meme lookup: `?mockSwarm=organic|unclear|none`, a swarm otherwise */
export function mockLookupSwarm(flag: string | null): { swarm: SwarmReport | null; swarm_live: SwarmDetail | null; swarm_sample: SwarmReport | null } {
  if (flag === "none") return { swarm: null, swarm_live: null, swarm_sample: null };
  const kind: Case = flag === "organic" || flag === "unclear" ? flag : "swarm";
  const live = kind === "swarm" ? mockDetail("mock-swarm") : null;
  const sample = report(kind, "sample", nowS());
  return { swarm: live?.report ?? sample, swarm_live: live, swarm_sample: sample };
}
