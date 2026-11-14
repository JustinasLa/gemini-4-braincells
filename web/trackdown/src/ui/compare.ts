import { domainOf, engineLabel, firstSeenLabel, humanLabel, imageAlt, normalizedBox, pageUrl, relationOf, type ImageMatch } from "../data/imageTrace";
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

function boxes(match: ImageMatch): string {
  return ([ [match.edited_region, "edited", "Edited region"], [match.crop_box, "crop", "Crop of the original"] ] as const).map(([raw, kind, label]) => {
    const box = normalizedBox(raw, match.width, match.height);
    return box ? `<span class="meme-region meme-region-${kind}" style="left:${box[0]}%;top:${box[1]}%;width:${box[2]}%;height:${box[3]}%"><span>${label}</span></span>` : "";
  }).join("");
}

function changesHtml(trace: TraceCase, match: ImageMatch): string {
  const changes = (match.changes ?? []).filter((change) => typeof change === "string");
  const kinds = (match.change_kinds ?? []).filter((kind) => typeof kind === "string");
  const step = trace.lookup?.steps.find((candidate) => candidate.id === "variations");
  const pending = step?.state === "pending" || step?.state === "running";
  return `<h3>What changed</h3>${pending ? '<p class="meme-scanning" role="status">Comparing variations…</p>' : ""}${changes.length ? `<ul>${changes.map((change) => `<li>${esc(change)}</li>`).join("")}</ul>` : !pending ? '<p class="meme-note">No change description returned.</p>' : ""}<div class="meme-kind-chips">${kinds.map((kind) => `<span>${esc(humanLabel(kind))}</span>`).join("")}</div><p class="meme-honesty">Changes are Nemotron’s model reading of the two images, not verified facts.</p>`;
}

export function refreshComparison(trace: TraceCase) {
  if (!active || active.dataset.trace !== trace.id) return;
  const match = trace.lookup?.image_matches?.find((candidate) => candidate.id === active!.dataset.match);
  if (!match) return;
  const changes = active.querySelector<HTMLElement>(".meme-changes")!;
  const html = changesHtml(trace, match);
  if (changes.innerHTML !== html) changes.innerHTML = html;
  const frame = active.querySelector(".meme-variant .meme-image")!;
  frame.querySelectorAll(".meme-region").forEach((region) => region.remove());
  frame.insertAdjacentHTML("beforeend", boxes(match));
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
  const relation = relationOf(match);
  const page = pageUrl(match.page_url);
  const dimensions = Number.isFinite(match.width) && Number.isFinite(match.height) && match.width! > 0 && match.height! > 0 ? `${match.width} × ${match.height} px` : "Dimensions unknown";
  dialog.innerHTML = `<header class="meme-dialog-head"><div><p class="meme-eyebrow">// ${trace.mock ? "MOCK · " : ""}IMAGE COMPARISON</p><h2 id="meme-compare-title">${humanLabel(relation)} ${relation === "different" ? "image · not a variation" : "copy"}</h2></div><button class="meme-close" aria-label="Close comparison" autofocus>×</button></header>
    <div class="meme-compare-body">
      <div class="meme-modes" role="group" aria-label="Comparison layout"><button data-view="side" aria-pressed="true">Side by side</button><button data-view="overlay" aria-pressed="false">Slider overlay</button></div>
      <div class="meme-comparison" data-view="side" style="--reveal:50%">
        <section class="meme-plane meme-upload"><h3>Submission</h3>${memeImage({ image_url: comparisonSource(trace), relation_note: "Submitted image", domain: "submitted source" })}</section>
        <section class="meme-plane meme-variant"><h3>${relation === "different" ? "Different image" : "Match"} · ${esc(domainOf(match))}</h3>${memeImage({ ...match, relation_note: imageAlt(match) })}</section>
        <div class="meme-slider-line" aria-hidden="true"></div>
      </div>
      <label class="meme-slider" hidden>Reveal match <output>50%</output><input type="range" min="0" max="100" value="50" aria-label="Reveal matched image" aria-valuetext="50 percent match revealed"></label>
      <div class="meme-box-key"><span>Solid green: edited region</span><span>Dashed white: crop of the original</span></div>
      <p class="meme-note">Boxes use the matched image’s coordinates. Slider alignment is visual only, not an image registration.</p>
      <div class="meme-comparison-facts"><span>${esc(humanLabel(relation))}</span><span>${Number.isFinite(match.distance) ? `Perceptual distance ${match.distance}` : "Perceptual distance unknown"}</span><span>${esc(engineLabel(match))}</span><span>${dimensions}</span><span>${esc(firstSeenLabel(match))}</span><span>${Number.isFinite(match.receipt_id) ? `Receipt #${match.receipt_id}` : "Receipt not supplied"}</span>${page ? `<a href="${esc(page)}" target="_blank" rel="noopener noreferrer">Page on ${esc(domainOf(match))} ↗</a>` : ""}</div>
      <section class="meme-changes" aria-live="polite">${changesHtml(trace, match)}</section>
    </div>`;
  dialog.querySelector(".meme-variant .meme-image")!.insertAdjacentHTML("beforeend", boxes(match));
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
