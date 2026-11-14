import { boxFrame, domainOf, engineLabel, firstSeenLabel, humanLabel, imageAlt, normalizedBox, pageUrl, relationOf, type ImageMatch } from "../data/imageTrace";
import { uploadUrl } from "../data/lookup";
import type { TraceCase } from "../types";
import { esc } from "./format";
import { memeImage } from "./memeImage";

let active: HTMLDialogElement | null = null;

export function comparisonSource(trace: TraceCase): string | undefined {
  const result = trace.lookup;
  if (!result) return undefined;
  if (result.kind === "image") return uploadUrl(result.id);
  return result.posts.find((post) => post.id === result.subject_id)?.media_urls?.[0];
}

/** The boxes drawn on one side. Fraction boxes (relate(upload, match)) live in the upload's frame; the
 *  edited region is also drawn on the match, which an "edited" relation has aligned with the upload.
 *  Pixel boxes are in the match's own frame. */
function boxes(match: ImageMatch, side: "upload" | "match"): string {
  const drawn = ([
    [match.edited_region, "edited", "Edited region", true],
    [match.crop_box, "crop", side === "upload" ? "Part the match shows" : "Crop", false],
  ] as const).filter(([raw, , , both]) => boxFrame(raw) === side || (both && boxFrame(raw) === "upload"));
  return drawn.map(([raw, kind, label]) => {
    const box = normalizedBox(raw, match.width, match.height);
    return box ? `<span class="meme-region meme-region-${kind}" style="left:${box[0]}%;top:${box[1]}%;width:${box[2]}%;height:${box[3]}%"><span>${label}</span></span>` : "";
  }).join("");
}

function changesHtml(trace: TraceCase, match: ImageMatch): string {
  const changes = (match.changes ?? []).filter((change) => typeof change === "string");
  const kinds = [...new Set((match.change_kinds ?? []).filter((kind) => typeof kind === "string"))]; // one chip per kind
  const step = trace.lookup?.steps.find((candidate) => candidate.id === "variations");
  const pending = step?.state === "pending" || step?.state === "running";
  return `<h3>What changed</h3>${pending ? '<p class="meme-scanning" role="status">Comparing variations…</p>' : ""}${changes.length ? `<ul>${changes.map((change) => `<li>${esc(change)}</li>`).join("")}</ul>` : !pending ? '<p class="meme-note">No change description returned.</p>' : ""}<div class="meme-kind-chips">${kinds.map((kind) => `<span>${esc(humanLabel(kind))}</span>`).join("")}</div><p class="meme-honesty">Changes are Nemotron’s model reading of the two images, not verified facts.</p>`;
}

const titleText = (match: ImageMatch) => {
  const relation = relationOf(match);
  return `${humanLabel(relation)} ${relation === "different" ? "image · not a variation" : "copy"}`;
};
const variantHeading = (match: ImageMatch) => `${relationOf(match) === "different" ? "Different image" : "Match"} · ${domainOf(match)}`;
/** what the variant's picture depends on: when this changes, the image (and its fallback chain) restarts */
const pictureKey = (match: ImageMatch) => JSON.stringify([match.image_url, match.thumb_url, match.page_url, match.domain, match.width, match.height]);

function factsHtml(match: ImageMatch): string {
  const page = pageUrl(match.page_url);
  const dimensions = Number.isFinite(match.width) && Number.isFinite(match.height) && match.width! > 0 && match.height! > 0 ? `${match.width} × ${match.height} px` : "Dimensions unknown";
  return `<span>${esc(humanLabel(relationOf(match)))}</span><span>${Number.isFinite(match.distance) ? `Perceptual distance ${match.distance}` : "Perceptual distance unknown"}</span><span>${esc(engineLabel(match))}</span><span>${dimensions}</span><span>${esc(firstSeenLabel(match))}</span><span>${Number.isFinite(match.receipt_id) ? `Receipt #${match.receipt_id}` : "Receipt not supplied"}</span>${page ? `<a href="${esc(page)}" target="_blank" rel="noopener noreferrer">Page on ${esc(domainOf(match))} ↗</a>` : ""}`;
}

/** Replace a region's markup only when it changed, keeping focus on an equivalent control. */
function swap(region: HTMLElement, html: string) {
  if (region.innerHTML === html) return;
  const focused = document.activeElement instanceof HTMLElement && region.contains(document.activeElement) ? document.activeElement : null;
  const href = focused?.getAttribute("href");
  region.innerHTML = html;
  if (focused) (region.querySelector<HTMLElement>(href ? `a[href="${CSS.escape(href)}"]` : "a,button") ?? region.closest<HTMLElement>("dialog")?.querySelector<HTMLElement>(".meme-close"))?.focus();
}

/** Replace a plane's picture when what it shows changed. A replaced frame that held focus hands it to
 *  its new fallback link when one is showing, else to the dialog's close button, never to the page. */
function setPicture(plane: HTMLElement, key: string, html: string) {
  if (plane.dataset.picture === key) return false;
  const old = plane.querySelector(".meme-image");
  const focused = !!old && old.contains(document.activeElement);
  plane.dataset.picture = key;
  old?.remove();
  plane.insertAdjacentHTML("beforeend", html);
  if (focused) (plane.querySelector<HTMLElement>(".meme-image-fallback:not([hidden]) a") ?? plane.closest("dialog")?.querySelector<HTMLElement>(".meme-close"))?.focus();
  return true;
}

/** Both pictures (the submission's source can arrive after the dialog opened) and the boxes over them. */
function renderPictures(dialog: HTMLElement, trace: TraceCase, match: ImageMatch) {
  const upload = dialog.querySelector<HTMLElement>(".meme-upload")!;
  const source = comparisonSource(trace);
  setPicture(upload, source ?? "", memeImage({ image_url: source, relation_note: "Submitted image", domain: "submitted source" }));
  const plane = dialog.querySelector<HTMLElement>(".meme-variant")!;
  plane.querySelector("h3")!.textContent = variantHeading(match);
  if (!setPicture(plane, pictureKey(match), memeImage({ ...match, relation_note: imageAlt(match) }))) {
    const image = plane.querySelector("img");
    if (image) image.alt = imageAlt(match);
  }
  for (const [selector, side] of [[".meme-variant", "match"], [".meme-upload", "upload"]] as const) {
    const frame = dialog.querySelector(`${selector} .meme-image`)!;
    frame.querySelectorAll(".meme-region").forEach((region) => region.remove());
    frame.insertAdjacentHTML("beforeend", boxes(match, side));
  }
}

/** The server drops candidates that turn out to look different: say so instead of a stale comparison. */
function showRemoved(dialog: HTMLElement) {
  if (dialog.dataset.removed) return;
  dialog.dataset.removed = "true";
  const body = dialog.querySelector<HTMLElement>(".meme-compare-body")!;
  const focused = body.contains(document.activeElement);
  dialog.querySelector("#meme-compare-title")!.textContent = "No longer a match";
  body.innerHTML = '<p class="meme-note" role="status">The image comparison removed this candidate from the results: it looks different from the submission, so it is not counted as a variation.</p>';
  if (focused) dialog.querySelector<HTMLElement>(".meme-close")?.focus();
}

/** Live update of an open comparison: every field that depends on the match, without touching the
 * layout mode, the slider position or (where the control survives) focus. */
export function refreshComparison(trace: TraceCase) {
  if (!active || active.dataset.trace !== trace.id) return;
  const match = trace.lookup?.image_matches?.find((candidate) => candidate.id === active!.dataset.match);
  if (!match) return showRemoved(active);
  active.querySelector("#meme-compare-title")!.textContent = titleText(match);
  renderPictures(active, trace, match);
  swap(active.querySelector<HTMLElement>(".meme-comparison-facts")!, factsHtml(match));
  swap(active.querySelector<HTMLElement>(".meme-changes")!, changesHtml(trace, match));
}

export function openCompare(trace: TraceCase, match: ImageMatch, opener: HTMLElement) {
  active?.close();
  const dialog = document.createElement("dialog");
  active = dialog;
  dialog.className = "meme-compare";
  dialog.dataset.trace = trace.id;
  dialog.dataset.match = match.id;
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "meme-compare-title");
  dialog.innerHTML = `<header class="meme-dialog-head"><div><p class="meme-eyebrow">// ${trace.mock ? "MOCK · " : ""}IMAGE COMPARISON</p><h2 id="meme-compare-title">${esc(titleText(match))}</h2></div><button class="meme-close" aria-label="Close comparison" autofocus>×</button></header>
    <div class="meme-compare-body">
      <div class="meme-modes" role="group" aria-label="Comparison layout"><button data-view="side" aria-pressed="true">Side by side</button><button data-view="overlay" aria-pressed="false">Slider overlay</button></div>
      <div class="meme-comparison" data-view="side" style="--reveal:50%">
        <section class="meme-plane meme-upload"><h3>Submission</h3></section>
        <section class="meme-plane meme-variant"><h3></h3></section>
        <div class="meme-slider-line" aria-hidden="true"></div>
      </div>
      <label class="meme-slider" hidden>Reveal match <output>50%</output><input type="range" min="0" max="100" value="50" aria-label="Reveal matched image" aria-valuetext="50 percent match revealed"></label>
      <div class="meme-box-key"><span>Solid green: edited region</span><span>Dashed white: the part of the submission a cropped match shows</span></div>
      <p class="meme-note">Boxes come from the pixel comparison of the two images. Slider alignment is visual only, not an image registration.</p>
      <div class="meme-comparison-facts">${factsHtml(match)}</div>
      <section class="meme-changes" aria-live="polite">${changesHtml(trace, match)}</section>
    </div>`;
  renderPictures(dialog, trace, match);
  document.body.append(dialog);
  const comparison = dialog.querySelector<HTMLElement>(".meme-comparison")!;
  const slider = dialog.querySelector<HTMLElement>(".meme-slider")!;
  const phone = matchMedia("(max-width: 600px)");
  const fit = () => {
    for (const plane of dialog.querySelectorAll<HTMLElement>(".meme-plane")) {
      const frame = plane.querySelector<HTMLElement>(".meme-image")!;
      const image = frame.querySelector("img");
      if (!image?.naturalWidth) continue;
      const scale = Math.min(plane.clientWidth / image.naturalWidth, Math.max(1, plane.clientHeight - 34) / image.naturalHeight);
      frame.style.width = `${image.naturalWidth * scale}px`;
      frame.style.height = `${image.naturalHeight * scale}px`;
    }
  };
  const mode = (value: string) => {
    comparison.dataset.view = value;
    slider.hidden = value !== "overlay";
    dialog.querySelectorAll<HTMLElement>("[data-view]").forEach((button) => { if (button.tagName === "BUTTON") button.setAttribute("aria-pressed", String(button.dataset.view === value)); });
    fit();
  };
  const resize = () => { if (phone.matches) mode("overlay"); fit(); };
  const observer = new ResizeObserver(fit);
  observer.observe(comparison);
  dialog.addEventListener("load", fit, true);
  phone.addEventListener("change", resize);
  dialog.querySelectorAll<HTMLButtonElement>("button[data-view]").forEach((button) => button.addEventListener("click", () => mode(button.dataset.view!)));
  dialog.querySelector("input")!.addEventListener("input", (event) => {
    const input = event.target as HTMLInputElement;
    comparison.style.setProperty("--reveal", `${input.value}%`);
    dialog.querySelector("output")!.textContent = `${input.value}%`;
    input.setAttribute("aria-valuetext", `${input.value} percent match revealed`);
  });
  dialog.querySelector(".meme-close")!.addEventListener("click", () => dialog.close());
  dialog.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Escape") { event.preventDefault(); dialog.close(); return; }
    if (event.key !== "Tab") return;
    const items = [...dialog.querySelectorAll<HTMLElement>("button,a[href],input")].filter((item) => item.getClientRects().length && !item.closest("[hidden]"));
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  dialog.addEventListener("close", () => {
    observer.disconnect();
    phone.removeEventListener("change", resize);
    dialog.remove();
    if (active === dialog) active = null;
    if (opener.isConnected && !opener.closest("[inert]")) opener.focus();
    else {
      const replacement = [...document.querySelectorAll<HTMLElement>("[data-meme-compare]")].find((element) => element.dataset.memeCompare === match.id && !element.closest("[inert]"));
      (replacement ?? document.querySelector<HTMLElement>("#peek") ?? document.querySelector<HTMLElement>("#stage"))?.focus();
    }
  }, { once: true });
  dialog.showModal();
  resize();
}
