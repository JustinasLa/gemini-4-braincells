import type { LookupAssessment, LookupPost, LookupResult } from "./lookup";

const started = "2026-11-13T12:20:00Z";
const post = (id: string, role: LookupPost["role"], createdAt: string | null, handle: string): LookupPost => ({
  id, platform: "x", url: `https://social.example/${handle}/status/${id.split(":")[1]}`,
  author_handle: handle, author_name: handle === "newsroom" ? "Example Newsroom" : "Example Reader",
  text: "Fictional example: the town observatory opens tonight.", created_at: createdAt, time_source: createdAt ? "snowflake" : null,
  role, similarity: 1, metrics: { views: 2900000, replies: 14, reposts: 108, likes: 205, bookmarks: 21, quotes: null },
  metrics_as_of: started, card: { title: "A fictional opening", domain: "news.example", url: "https://news.example/observatory" }, media_urls: [], receipt_id: 1,
});

export const mockLookupResult: LookupResult = {
  id: "lk_development_mock", state: "done", error: null,
  input: { url: "https://social.example/newsroom/status/100", text: null }, kind: "x_post",
  started_at: started, finished_at: "2026-11-13T12:20:06Z", status: "insufficient", subject_id: "x:100", origin_id: "x:99",
  posts: [post("x:100", "submitted", "2026-11-13T12:10:00Z", "newsroom"), post("x:99", "origin", "2026-11-13T12:00:00Z", "reader_one"), post("x:101", "copy", "2026-11-13T12:14:00Z", "reader_two"), { ...post("x:102", "quote", null, "reader_three"), similarity: 0.6, text: "A fictional quote with a different claim; time not returned." },
    { ...post("x:98", "screenshotted", "2026-11-13T11:30:00Z", "reader_four"), similarity: 0.83, card: null, receipt_id: 7, text: "Fictional example: the town observatory opens soon." }],
  accounts: [{ platform: "x", handle: "newsroom", name: "Example Newsroom", url: "https://social.example/newsroom", avatar_url: null, bio: "Fictional account for development only.", location: "Example Town", joined: "2026-11", age_days: 12, followers: 2500, following: 120, posts_count: 810, recent_posts_per_day: 4.5, receipt_id: 2 }],
  pages: [{ url: "https://news.example/observatory", final_url: "https://news.example/observatory", role: "linked", publisher: "Example Daily", author: "Example Reporter", published_at: null, published_hint: "2026-11-13", headline: "A fictional opening", claims: ["The observatory opens tonight (fictional)."], snippet: "This is mock news, not a real event.", receipt_id: 3 }],
  media: [{ media_url: "https://media.example/poster.png", post_id: "x:100", description: "A fictional observatory poster.", extracted_text: ["OPEN TONIGHT"], reuse_cues: ["Same fictional poster"], manipulation_cues: [], confidence: 0.7, model: "mock-nemotron", latency_ms: 1200, receipt_id: 4 }],
  edges: [{ from: "x:99", to: "x:100", rel: "copy", sim: 1, tool: "oxylabs", note: "Same fictional text" }, { from: "x:100", to: "x:101", rel: "copy", sim: 1, tool: "oxylabs", note: "Same fictional text" }, { from: "x:100", to: "x:102", rel: "quote", sim: 0.6, tool: "oxylabs", note: "Fictional quote" }, { from: "https://news.example/observatory", to: "x:100", rel: "link", sim: null, tool: "oxylabs", note: "Linked fictional article" }, { from: "x:100", to: "x:98", rel: "screenshot_of", sim: 0.83, tool: "nemotron", note: "differs" }],
  screenshots: [
    { media_url: "https://media.example/poster.png", media_hash: "mock:1", post_id: "x:100", platform: "x", handle: "reader_four", display_name: "Example Reader", shown_time: "11:30 AM · Nov 13, 2026", shown_text: "Fictional example: the town observatory opens <b>tonight</b>.", shown_counts: { replies: 3, reposts: 40, likes: 900, views: null }, ui_oddities: ["the like count uses a font unlike the rest (fictional)"], read_confidence: 0.8, read_model: "mock-nemotron",
      label: "differs", reason: "the real post says \"soon\", the screenshot \"tonight\"", original_id: "x:98", original_url: "https://social.example/reader_four/status/98", original_text: "Fictional example: the town observatory opens soon.", original_created_at: "2026-11-13T11:30:00Z", original_time_source: "snowflake", similarity: 0.83,
      diff: [{ op: "same", text: "Fictional example: the town observatory opens" }, { op: "shot_only", text: "<b>tonight</b>." }, { op: "post_only", text: "soon." }], differences: ["\"tonight\" in the screenshot, \"soon\" in the post"], time_check: "consistent", shown_time_earliest: "2026-11-12T21:30:00Z", shown_time_latest: "2026-11-14T01:30:59Z", time_note: "shown time is in the viewer's unknown time zone (within 14 h of UTC)",
      counts_now: { views: 12000, replies: 3, reposts: 41, likes: 88, bookmarks: 2, quotes: null }, counts_note: "Counts change as people like and repost; shown as context, never as a verdict.", candidates_checked: 2, receipt_ids: [8, 7], latency_ms: 2100 },
    { media_url: "https://media.example/second.png", media_hash: "mock:2", post_id: "x:100", platform: "bluesky", handle: "nobody.example", display_name: null, shown_time: "2h", shown_text: "A second fictional screenshot.", shown_counts: {}, ui_oddities: [], read_confidence: 0.6, read_model: "mock-nemotron",
      label: "not_found", reason: "no post with this text on the shown account", original_id: null, original_url: null, original_text: null, original_created_at: null, original_time_source: null, similarity: null,
      diff: [], differences: [], time_check: "unknown", shown_time_earliest: null, shown_time_latest: null, time_note: null, counts_now: null, counts_note: null, candidates_checked: 0, receipt_ids: [8], latency_ms: 900 },
  ],
  clusters: [], spread: { n_posts: 4, n_accounts: 4, n_verbatim: 3, first_at: "2026-11-13T12:00:00Z", last_at: "2026-11-13T12:14:00Z", span_s: 840, first_hour_accounts: 3, min_gap_s: 240, note: "MOCK: search results are a sample, not every copy." },
  receipts: [
    { id: 1, tool: "oxylabs", endpoint: "/scrape", summary: "Mock post and copy metrics.", source_url: "https://social.example/newsroom/status/100", query: null, request_id: "mock-post", latency_ms: 100, cached: false, at: started },
    { id: 2, tool: "oxylabs", endpoint: "/scrape", summary: "Mock profile history.", source_url: "https://social.example/newsroom", query: null, request_id: "mock-profile", latency_ms: 200, cached: false, at: started },
    { id: 3, tool: "oxylabs", endpoint: "/scrape", summary: "Mock linked article; exact time unknown.", source_url: "https://news.example/observatory", query: null, request_id: "mock-page", latency_ms: 300, cached: false, at: started },
    { id: 4, tool: "nemotron", endpoint: "vision", summary: "Mock image reading, not a real inference.", source_url: "https://media.example/poster.png", query: null, request_id: "mock-media", latency_ms: 1200, cached: false, at: started },
    { id: 5, tool: "x-id", endpoint: "snowflake", summary: "Mock post-ID timestamp.", source_url: "https://social.example/newsroom/status/100", query: null, request_id: null, latency_ms: 0, cached: false, at: started },
    { id: 6, tool: "bluesky", endpoint: "searchPosts", summary: "Mock Bluesky search with no matches.", source_url: "https://bsky.example/search", query: "fictional observatory", request_id: null, latency_ms: 100, cached: false, at: started },
    { id: 7, tool: "oxylabs", endpoint: "/scrape", summary: "Mock read of the real post shown in the screenshot.", source_url: "https://social.example/reader_four/status/98", query: null, request_id: "mock-shot-post", latency_ms: 300, cached: false, at: started },
    { id: 8, tool: "nemotron", endpoint: "vision", summary: "Mock screenshot reading, not a real inference.", source_url: "https://media.example/poster.png", query: null, request_id: "mock-shot", latency_ms: 2100, cached: false, at: started },
  ],
  assessment: null, // set below: the primary (Nemotron) reading
  readings: [],
  readings_disagree: true,
  steps: [
    { id: "post", tool: "oxylabs", label: "Read the mock post", state: "done", detail: "2.9M fictional views", found: ["x:100"], started_at: started, ended_at: started },
    { id: "copies_x", tool: "oxylabs", label: "Find mock copies", state: "done", detail: "Three same-text matches; one quote", found: ["x:99", "x:101", "x:102"], started_at: started, ended_at: started },
    { id: "assess", tool: "nemotron", label: "Nemotron reads the receipts", state: "done", detail: "No monitored match; coordination abstains", found: [], started_at: started, ended_at: started },
    { id: "second_reading", tool: "gemini", label: "Gemini's second reading", state: "done", detail: "Mock second reading", found: [], started_at: started, ended_at: started },
  ],
  notes: ["DEVELOPMENT MOCK: every account, page and metric is fictional.", "Unknown times are positioned for layout only; no exact timestamp is inferred from a date hint."],
};

const primary: LookupAssessment = { model: "mock-nemotron-3.5-super-vl", summary: "MOCK: three accounts repeat fictional text. This is a copy sample, not a coordination verdict.", origin: "The earliest mock copy predates the submitted post.", corroboration: "Only a fictional article is linked.", spread_pattern: "Three same-text matches across 14 minutes.", counter_evidence: ["Independent people can quote the same news."], would_change: ["A match to a monitored cluster with measured behavioural evidence."], cites: [1, 3, 4], llm_reading: "unclear", latency_ms: 10, cost_usd: 0 };
const second: LookupAssessment = { ...primary, model: "mock-gemini-4-hydrogen", summary: "MOCK second reading: the same-text copies within minutes look like a copy campaign; still no monitored match.", llm_reading: "copy_campaign_looking", counter_evidence: [], cites: [1, 3] };
mockLookupResult.assessment = primary;
mockLookupResult.readings = [primary, second];

/** Development mock of the live job: collect, primary reading, then a later second reading that disagrees. */
export async function runMockLookup(onUpdate: (result: LookupResult) => void, signal: AbortSignal): Promise<LookupResult> {
  const last = mockLookupResult.steps.length; // phase `last` is the finished job
  for (let phase = 0; phase <= last; phase++) {
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    const result = structuredClone(mockLookupResult);
    if (phase < last) {
      result.state = "running";
      result.finished_at = null;
      result.readings_disagree = false;
      // the primary reading lands with the assess step; the second one only with the finished job
      result.readings = phase === last - 1 ? [primary] : [];
      result.assessment = phase === last - 1 ? primary : null;
      result.posts = phase === 0 ? [] : phase === 1 ? result.posts.slice(0, 1) : result.posts;
      if (phase < 2) { result.accounts = []; result.pages = []; result.media = []; result.screenshots = []; result.edges = []; result.spread = null; result.receipts = phase ? result.receipts.slice(0, 1) : []; }
      result.steps = result.steps.map((step, index) => ({ ...step, state: index < phase ? "done" : index === phase ? "running" : "pending", detail: index < phase ? step.detail : null, found: index < phase ? step.found : [] }));
    }
    onUpdate(result);
    if (phase === last) return result;
    await new Promise<void>((resolve) => setTimeout(resolve, phase === last - 1 ? 2500 : 700));
  }
  return mockLookupResult;
}
