import type { ImageMatch } from "./imageTrace";
import type { LookupResult } from "./lookup";

const started = "2026-11-14T12:00:00Z";
function picture(caption: string, width = 480, height = 320, crop = false): string {
  return `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="${crop ? "60 40 360 240" : "0 0 480 320"}"><rect width="480" height="320" fill="#182a23"/><path d="M0 265 120 155 215 235 300 135 480 270V320H0" fill="#45634c"/><circle cx="355" cy="95" r="42" fill="#cce596"/><path d="M145 230v-92l25-34 28 34v92zm50 0v-70l28-35 28 35v70z" fill="#f0ebd6"/><circle cx="170" cy="151" r="5" fill="#182a23"/><circle cx="223" cy="172" r="5" fill="#182a23"/><rect x="15" y="16" width="450" height="52" fill="#08150f"/><text x="240" y="50" text-anchor="middle" fill="#eeffed" font-family="sans-serif" font-weight="bold" font-size="25">${caption}</text><text x="18" y="303" fill="#eeffed" font-family="monospace" font-size="14">MOCK / FICTIONAL OBSERVATORY</text></svg>`)}`;
}

export function memeFixture(flag = "meme", origin = "http://localhost:3005"): LookupResult {
  const submittedImage = `${origin}/__meme_mock__/upload.svg`;
  const base = (id: string, relation: string, overrides: ImageMatch = {}): ImageMatch => ({
    id, relation, engine: "tineye", image_url: picture("THE MOON IS OPEN"), thumb_url: picture("THE MOON IS OPEN", 240, 160),
    page_url: `https://archive.example/${id.replace(":", "-")}`, domain: "archive.example",
    first_seen: "2026-11-12T00:00:00Z", time_source: "crawl_date", width: 480, height: 320,
    relation_note: "Fictional observatory meme, original caption", distance: 0, receipt_id: 12, ...overrides,
  });
  const matches = [
    base("img:origin", "same"),
    base("img:same", "same", { domain: "mirror.example", first_seen: "2026-11-13T00:00:00Z", time_source: "page", relation_note: "Same picture and caption" }),
    base("img:resized", "resized", { domain: "small.example", width: 240, height: 160, image_url: picture("THE MOON IS OPEN", 240, 160), first_seen: "2026-11-13T04:00:00Z", relation_note: "Same picture, smaller dimensions", distance: 1 }),
    base("img:cropped", "cropped", { domain: "crop.example", image_url: picture("THE MOON IS OPEN", 360, 240, true), thumb_url: picture("THE MOON IS OPEN", 360, 240, true), width: 360, height: 240, first_seen: null, crop_box: [0.125, 0.125, 0.875, 0.875], relation_note: "Edges cropped from the observatory picture", distance: 4, changes: ["Outer edges are cropped."], change_kinds: ["cropped"] }),
    base("img:edited", "edited", { domain: "caption.example", image_url: picture("THE MOON IS CLOSED"), thumb_url: picture("THE MOON IS CLOSED", 240, 160), first_seen: "2026-11-13T12:00:00Z", time_source: "page", distance: 6, edited_region: [0.031, 0.05, 0.969, 0.213], changes: ["Caption changed from ‘THE MOON IS CLOSED’ to ‘THE MOON IS OPEN’."], change_kinds: ["caption_changed"], changes_from: "match", relation_note: "Observatory picture with a changed caption" }),
    base("img:unknown", "future_relation", { domain: "uncertain.example", first_seen: null, engine: "future_engine", relation_note: "Relation not established" }),
    base("img:different", "different", { domain: "other.example", image_url: picture("ANOTHER PICTURE"), relation_note: "Looks different; excluded from the variation map" }),
  ];
  if (flag === "meme-blocked") {
    const match = matches.find((candidate) => candidate.id === "img:edited")!;
    match.image_url = `${origin}/__meme_missing__/image.png`;
    match.thumb_url = `${origin}/__meme_missing__/thumb.png`;
  }
  const upload = flag === "meme-upload"; // an uploaded picture: no time of its own, only crawl-dated matches
  if (upload) for (const match of matches) if (match.first_seen) match.time_source = "crawl_date";
  return {
    id: "lk_mock_meme", state: "done", error: null, kind: upload ? "image" : "x_post", input: upload ? { url: null, text: null, image: { sha256: "0".repeat(64), mime: "image/svg+xml", bytes: 1 } } : { url: "https://social.example/observatory/status/100", text: null },
    started_at: started, finished_at: started, status: "insufficient", subject_id: "x:submitted", origin_id: null,
    posts: [{ id: "x:submitted", platform: "x", url: "https://social.example/observatory/status/100", author_handle: upload ? null : "observatory", author_name: upload ? null : "MOCK Observatory", text: upload ? "" : "MOCK · trace this fictional observatory meme", created_at: upload ? null : started, time_source: upload ? null : "snowflake", role: "submitted", similarity: null, metrics: {}, metrics_as_of: null, card: null, media_urls: [submittedImage], receipt_id: 11 }],
    accounts: [], pages: [], media: [], edges: [], clusters: [], spread: null,
    receipts: [{ id: 12, tool: "nemotron", endpoint: "mock/image-comparison", summary: "MOCK image comparisons; descriptions are fictional, not a real model call.", source_url: "https://archive.example/meme", query: null, request_id: "mock-meme", latency_ms: 0, cached: false, at: started }],
    assessment: null,
    steps: [
      { id: "reverse_image", tool: "oxylabs", label: "Search for earlier copies", state: "done", detail: "MOCK reverse-image results", found: matches.map((match) => match.id!), started_at: started, ended_at: started },
      { id: "variations", tool: "nemotron", label: "Compare variations", state: "done", detail: "MOCK image readings", found: [], started_at: started, ended_at: started },
    ],
    notes: ["DEVELOPMENT MOCK: fictional .example domains and self-contained images. No live lookup was made."],
    image_matches: flag === "meme-empty" || flag === "meme-failed" ? [] : matches,
    image_origin_id: flag === "meme-empty" || flag === "meme-failed" ? null : "img:origin",
  };
}

export async function runMemeMock(flag: string, onUpdate: (result: LookupResult) => void, signal: AbortSignal): Promise<LookupResult> {
  const pause = () => new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException("Cancelled", "AbortError")); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, flag === "meme-loading" ? 15000 : 1800);
    signal.addEventListener("abort", abort, { once: true });
  });
  for (let phase = 0; phase < 3; phase++) {
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    const result = memeFixture(flag, location.origin);
    if (phase < 2) {
      result.state = "running";
      result.finished_at = null;
      result.steps[0].state = phase === 0 ? "running" : "done";
      result.steps[1].state = phase === 0 ? "pending" : "running";
      if (phase === 0) { delete result.image_matches; delete result.image_origin_id; result.steps[0].found = []; }
      else result.image_matches = result.image_matches?.map((match) => ({ ...match, changes: [], change_kinds: [] }));
    } else if (flag === "meme-failed") {
      result.steps[0].state = "failed";
      result.steps[0].detail = "MOCK: reverse image service unavailable. The submitted post remains readable.";
      result.steps[1].state = "failed";
      result.steps[1].detail = "MOCK: no image pairs to compare.";
    }
    if (phase === 2) Object.assign(result, (await import("./mockSwarm")).mockLookupSwarm(new URLSearchParams(location.search).get("mockSwarm")));
    onUpdate(result);
    if (phase === 2) return result;
    await pause();
  }
  throw new Error("Mock did not finish");
}
