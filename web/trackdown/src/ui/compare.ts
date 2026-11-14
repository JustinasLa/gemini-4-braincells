import { boxFrame, domainOf, humanLabel, imageAlt, normalizedBox, relationOf, type ImageMatch } from "../data/imageTrace";
import { uploadUrl } from "../data/lookup";
import type { TraceCase } from "../types";
import { esc } from "./format";
import { icon } from "./icons";
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

const titleText = (match: ImageMatch) => {
  const relation = relationOf(match);
  if (relation === "template") return "Same template · other content · not a copy of this picture";
  return `${humanLabel(relation)} ${relation === "different" ? "image · not a variation" : "copy"}`;
};
const variantHeading = (match: ImageMatch) => `${relationOf(match) === "different" ? "Different image" : "Match"} · ${domainOf(match)}`;
/** what the variant's picture depends on: when this changes, the image (and its fallback chain) restarts */
const pictureKey = (match: ImageMatch) => JSON.stringify([match.image_url, match.thumb_url, match.page_url, match.domain, match.width, match.height]);

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

/** Live update of an open comparison, keeping the pictures and their labels current. */
export function refreshComparison(trace: TraceCase) {
  if (!active || active.dataset.trace !== trace.id) return;
  const match = trace.lookup?.image_matches?.find((candidate) => candidate.id === active!.dataset.match);
  if (!match) return showRemoved(active);
  if (active.dataset.removed) return; // its body is gone: a match that comes back stays "no longer a match"
  active.dataset.relation = relationOf(match);
  active.querySelector("#meme-compare-title")!.textContent = titleText(match);
  renderPictures(active, trace, match);
}

export function openCompare(trace: TraceCase, match: ImageMatch, opener: HTMLElement) {
  active?.close();
  const dialog = document.createElement("dialog");
  active = dialog;
  dialog.className = "meme-compare";
  dialog.dataset.trace = trace.id;
  dialog.dataset.match = match.id;
  dialog.dataset.relation = relationOf(match);
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "meme-compare-title");
  dialog.innerHTML = `<header class="meme-dialog-head"><div><p class="meme-eyebrow">${trace.mock ? "Mock · " : ""}Image comparison</p><h2 id="meme-compare-title">${esc(titleText(match))}</h2></div><button class="meme-close" aria-label="Close comparison" autofocus>${icon.close}</button></header>
    <div class="meme-compare-body">
      <div class="meme-comparison">
        <section class="meme-plane meme-upload"><h3>Submission</h3></section>
        <section class="meme-plane meme-variant"><h3></h3></section>
      </div>
    </div>`;
  renderPictures(dialog, trace, match);
  document.body.append(dialog);
  const comparison = dialog.querySelector<HTMLElement>(".meme-comparison")!;
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
  const observer = new ResizeObserver(fit);
  observer.observe(comparison);
  dialog.addEventListener("load", fit, true);
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
    dialog.remove();
    if (active === dialog) active = null;
    if (opener.isConnected && !opener.closest("[inert]")) opener.focus();
    else {
      const replacement = [...document.querySelectorAll<HTMLElement>("[data-meme-compare]")].find((element) => element.dataset.memeCompare === match.id && !element.closest("[inert]"));
      (replacement ?? document.querySelector<HTMLElement>("#peek") ?? document.querySelector<HTMLElement>("#stage"))?.focus();
    }
  }, { once: true });
  dialog.showModal();
  fit();
}
