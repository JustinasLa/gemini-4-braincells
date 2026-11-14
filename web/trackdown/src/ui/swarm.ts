// Meme swarms on screen: the strip on a meme result (a dot timeline, the two-word answer, a few
// chips), the "Pushed right now" wall on the home page and the page of one live picture.
// Pictures first, almost no words: a verdict describes the group of posts, never one account, so
// no verdict or flag ever sits next to a handle.
import { axisLabels, fetchSwarmDetail, fetchSwarmWall, firstWords, GROUP_FILLS, layoutTimeline, swarmAria, swarmChips, VERDICT, type SwarmCard, type SwarmDetail, type SwarmReport } from "../data/swarm";
import { IMAGE_TYPES } from "../data/lookup";
import { esc } from "./format";
import "./swarm.css";

const PITCH = 10; // viewBox units per dot column / row
const COLS = 48;
const COLS_NARROW = 32; // a phone: fewer, bigger dots, while they fill at most half the grid (a shape, not a solid block)
const ROWS = 10;
const columns = (n: number) => (n <= (COLS_NARROW * ROWS) / 2 && typeof matchMedia === "function" && matchMedia("(max-width: 600px)").matches ? COLS_NARROW : COLS);

/** a link to the page of one live picture; the dev mock flag rides along */
export function swarmHref(id: string): string {
  const params = new URLSearchParams({ swarm: id });
  const here = new URLSearchParams(location.search);
  const mock = here.get("mockSwarm") ?? (here.has("mockLookup") ? "" : null);
  if (import.meta.env.DEV && mock != null) params.set("mockSwarm", mock);
  return `?${params}`;
}

const epoch = (iso: string | null | undefined) => (iso ? Date.parse(iso) / 1000 : NaN);

/** the dot timeline: one dot per sighting, the burst on a band of the verdict's colour */
function plot(report: SwarmReport): string {
  const points = report.timeline ?? [];
  if (!points.length) return "";
  const cols = columns(points.length);
  const { dots, burst } = layoutTimeline(points, { cols, rows: ROWS });
  // as tall as the tallest stack: a swarm stands up as a column, a spread of people lies flat
  const used = Math.max(3, ...dots.map((d) => d.row + 1));
  const w = cols * PITCH, h = used * PITCH;
  const cx = (col: number) => (col + 0.5) * PITCH;
  const cy = (row: number) => h - (row + 0.5) * PITCH;
  const fill = (d: (typeof dots)[0]) => (d.group >= 0 ? GROUP_FILLS[Math.min(d.group, GROUP_FILLS.length - 1)] : d.burst ? "var(--vc)" : "var(--sw-dot)");
  // own words first, so copy-paste groups and the burst draw on top
  const marks = dots.map((d) => d.boost
    ? `<circle cx="${cx(d.col)}" cy="${cy(d.row)}" r="${PITCH * 0.26}" fill="none" stroke="${fill(d)}" stroke-width="${PITCH * 0.13}"/>`
    : `<circle cx="${cx(d.col)}" cy="${cy(d.row)}" r="${PITCH * 0.38}" fill="${fill(d)}"/>`).join("");
  const top = burst ? Math.max(...dots.filter((d) => d.col >= burst[0] && d.col <= burst[1]).map((d) => d.row + 1)) : 0;
  const band = burst ? `<rect class="sw-band" x="${burst[0] * PITCH}" y="${h - top * PITCH}" width="${(burst[1] - burst[0] + 1) * PITCH}" height="${top * PITCH}"/>` : "";
  const times = points.map((p) => p.t).filter(Number.isFinite);
  const first = Number.isFinite(epoch(report.first_at)) ? epoch(report.first_at) : Math.min(...times);
  const last = Number.isFinite(epoch(report.last_at)) ? epoch(report.last_at) : Math.max(...times);
  const [from, to] = axisLabels(first, last);
  return `<figure class="sw-plot">
    <svg viewBox="0 -2 ${w} ${h + 4}" role="img" aria-label="${esc(swarmAria(report))}" preserveAspectRatio="xMidYMax meet">${band}<line class="sw-base" x1="0" x2="${w}" y1="${h + 1}" y2="${h + 1}"/>${marks}</svg>
    <figcaption class="sw-axis" aria-hidden="true"><span>${esc(from)}</span><span>${esc(to)}</span></figcaption>
  </figure>`;
}

/** The swarm strip: the dot timeline, the verdict word, up to four chips, the basis note (not on the
 *  live picture's own page, `stack`: there every post is the live feed's, and the chips already count them). */
export function swarmStrip(report: SwarmReport, opts: { live?: string | null; stack?: boolean } = {}): string {
  const v = VERDICT[report.verdict] ?? VERDICT.unclear;
  const chips = swarmChips(report);
  const why = report.verdict === "unclear" && report.why_unclear?.trim() ? `<p class="sw-why">${esc(report.why_unclear.trim())}</p>` : "";
  const note = !opts.stack && report.basis_note?.trim() ? `<p class="sw-note">${esc(report.basis_note.trim())}</p>` : "";
  return `<section class="st-sec sw${opts.stack ? " stack" : ""}" data-verdict="${esc(report.verdict)}" style="--vc:${v.css}" aria-labelledby="sw-word">
    <h2 class="sw-word" id="sw-word">${esc(v.word)}</h2>
    ${plot(report)}
    <div class="sw-say">
      ${chips.length ? `<ul class="sw-chips">${chips.map((c) => `<li>${c.label ? `<b>${esc(c.label)}</b>` : ""}${esc(c.summary)}</li>`).join("")}</ul>` : why}
      ${note}
      ${opts.live ? `<a class="sw-live" href="${esc(swarmHref(opts.live))}">Live on Bluesky →</a>` : ""}
    </div>
  </section>`;
}

// ---------------------------------------------------------------- home: the wall

function tile(card: SwarmCard): string {
  const v = VERDICT[card.verdict] ?? VERDICT.unclear;
  return `<a class="sw-tile" href="${esc(swarmHref(card.id))}" style="--vc:${v.css}" aria-label="${esc(`${v.word}: ${card.n_accounts} accounts`)}">
    <img src="${esc(card.thumb)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" draggable="false">
    <b>${card.n_accounts}</b>
  </a>`;
}

/** "Pushed right now": square thumbnails of pictures many Bluesky accounts posted. Nothing at all
 *  when the list is empty or the API does not answer. */
export async function mountSwarmWall(home: HTMLElement): Promise<void> {
  const cards = await fetchSwarmWall(24);
  if (!cards.length || !home.isConnected || home.classList.contains("out")) return;
  const el = document.createElement("section");
  el.className = "sw-wall";
  el.setAttribute("aria-labelledby", "sw-wall-h");
  el.innerHTML = `<h2 id="sw-wall-h">Pushed right now</h2><div class="sw-tiles">${cards.map(tile).join("")}</div>`;
  // a picture that will not load takes its tile with it; no tiles left, no wall
  el.addEventListener("error", (event) => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    img.closest(".sw-tile")?.remove();
    if (!el.querySelector(".sw-tile")) { el.remove(); home.classList.remove("has-wall"); }
  }, true);
  home.appendChild(el);
  home.classList.add("has-wall");
}

// ---------------------------------------------------------------- the page of one live picture

/** a picture this page can read as bytes (to trace it): its own origin, or a data/blob address.
 *  Bluesky's image CDN sends no CORS header, so its pictures cannot be fetched from here. */
function readable(url: string): boolean {
  if (/^(data|blob):/i.test(url)) return true;
  try { return new URL(url, location.href).origin === location.origin; } catch { return false; }
}

/** the picture as a file the upload path takes (JPEG, PNG, WebP, GIF); anything else is redrawn as a PNG */
async function pictureFile(url: string): Promise<File> {
  const blob = await (await fetch(url)).blob();
  if (IMAGE_TYPES.includes(blob.type)) return new File([blob], "picture", { type: blob.type });
  const img = new Image();
  img.src = URL.createObjectURL(blob);
  try {
    await img.decode();
    const canvas = Object.assign(document.createElement("canvas"), { width: img.naturalWidth || 600, height: img.naturalHeight || 600 });
    canvas.getContext("2d")!.drawImage(img, 0, 0, canvas.width, canvas.height);
    const png = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!png) throw new Error("unreadable picture");
    return new File([png], "picture.png", { type: "image/png" });
  } finally {
    URL.revokeObjectURL(img.src);
  }
}

const clock = (iso: string | null) => {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` : "";
};
function postRow(post: SwarmDetail["posts"][0]): string {
  const words = firstWords(post.text, 9);
  const handle = post.handle ? `<span class="sw-h">${esc(post.handle)}</span>` : "";
  // a picture posted without words: the handle takes the line (no lone dot far from its handle)
  const line = words ? `<span class="sw-t">${esc(words)}</span>${handle}` : handle || `<span class="sw-t">·</span>`;
  return `<li><a href="${esc(post.url)}" target="_blank" rel="noopener noreferrer"><time>${esc(clock(post.at))}</time>${line}</a></li>`;
}

export function swarmPageHtml(detail: SwarmDetail, canTrace: boolean): string {
  const lines = (detail.caption ?? "").split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 4);
  const posts = (detail.posts ?? []).filter((post) => post && /^https?:\/\//i.test(post.url)).slice(0, 30);
  const SHOWN = 8;
  // the list holds at most 30 links; the count is every post of the picture, as the chips count them
  const total = Math.max(posts.length, Number.isFinite(detail.n_posts) ? detail.n_posts : 0);
  return `<div class="st-wrap">
    <section class="st-hero sw-hero">
      <figure class="st-mine"><img src="${esc(detail.picture)}" alt="${esc(lines.join(" ") || "The picture")}" referrerpolicy="no-referrer" draggable="false"></figure>
      <div class="st-say">
        ${swarmStrip(detail.report, { stack: true })}
        ${canTrace ? `<div class="st-actions"><button class="btn solid" type="button" data-swarm-trace>Trace it</button></div>` : ""}
      </div>
    </section>
    ${posts.length ? `<section class="st-sec" aria-labelledby="sw-posts-h"><h2 id="sw-posts-h">Posts<span>${total}</span></h2>
      <ol class="sw-posts">${posts.slice(0, SHOWN).map(postRow).join("")}</ol>
      ${posts.length > SHOWN ? `<details class="sw-more"><summary>+${posts.length - SHOWN}</summary><ol class="sw-posts">${posts.slice(SHOWN).map(postRow).join("")}</ol></details>` : ""}
    </section>` : ""}
  </div>`;
}

/** The page of one live picture over the map, in the story's layer. `onTrace` gets the picture as a file. */
export function mountSwarmPage(app: HTMLElement, onTrace: (file: File) => void) {
  const el = document.createElement("section");
  el.className = "story sw-page";
  el.setAttribute("aria-label", "Live picture");
  el.hidden = true;
  el.tabIndex = -1;
  app.appendChild(el);
  let shown: SwarmDetail | null = null;
  el.addEventListener("click", async (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-swarm-trace]");
    if (!button || !shown) return;
    button.disabled = true;
    try { onTrace(await pictureFile(shown.picture)); }
    catch { button.closest(".st-actions")?.remove(); } // unreadable after all: no button rather than an error
  });
  el.addEventListener("error", (event) => {
    if (!(event.target instanceof HTMLImageElement)) return;
    event.target.replaceWith(Object.assign(document.createElement("span"), { className: "st-noimg", textContent: "No picture" }));
    el.querySelector("[data-swarm-trace]")?.closest(".st-actions")?.remove(); // nothing left to trace
  }, true);
  return {
    /** false when the picture is not there (404, API down): the caller shows the home instead */
    async open(id: string): Promise<boolean> {
      const detail = await fetchSwarmDetail(id);
      if (!detail) return false;
      shown = detail;
      el.innerHTML = swarmPageHtml(detail, readable(detail.picture));
      el.hidden = false;
      app.dataset.story = "story";
      el.scrollTop = 0;
      el.focus();
      return true;
    },
    hide() {
      if (el.hidden) return;
      el.hidden = true;
      el.innerHTML = "";
      shown = null;
      if (app.dataset.story === "story" && !app.querySelector(".story:not(.sw-page):not([hidden])")) delete app.dataset.story;
    },
  };
}
