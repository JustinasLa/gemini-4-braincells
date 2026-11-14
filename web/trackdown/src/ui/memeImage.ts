import { domainOf, imageAlt, imageUrl, pageUrl, type ImageMatch } from "../data/imageTrace";
import { esc } from "./format";

export function memeImage(match: ImageMatch, thumbnail = false): string {
  const primary = imageUrl(thumbnail ? match.thumb_url : match.image_url) ?? imageUrl(match.image_url) ?? imageUrl(match.thumb_url);
  const fallback = imageUrl(thumbnail ? match.image_url : match.thumb_url);
  const link = pageUrl(match.page_url) ?? pageUrl(match.image_url);
  // reserve the frame's shape before the image loads (or fails), so cards measured once by the graph keep their size
  const sized = [match.width, match.height].every((side) => typeof side === "number" && Number.isFinite(side) && side > 0);
  const ratio = sized ? `${match.width} / ${match.height}` : "3 / 2";
  return `<div class="meme-image" style="--ar:${ratio}"${primary ? "" : ' data-unavailable="true"'}>
    ${primary ? `<img data-meme-image src="${esc(primary)}" data-fallback="${esc(fallback && fallback !== primary ? fallback : "")}" alt="${esc(imageAlt(match))}" referrerpolicy="no-referrer" draggable="false">` : ""}
    <span class="meme-image-fallback"${primary ? " hidden" : ""}>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open image on ${esc(domainOf(match))}</a>` : "Image unavailable · no image link supplied"}</span>
  </div>`;
}

export function installImageFallbacks() {
  document.addEventListener("error", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.hasAttribute("data-meme-image")) return;
    const fallback = image.dataset.fallback;
    delete image.dataset.fallback;
    if (fallback) { image.src = fallback; return; }
    image.hidden = true;
    const frame = image.closest<HTMLElement>(".meme-image")!;
    frame.dataset.unavailable = "true";
    frame.querySelector<HTMLElement>(".meme-image-fallback")!.hidden = false;
  }, true);
}
