const MEDIA_STATES = new Set(["complete", "pending", "failed", "malformed", "missing"]);

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function list(value) {
  if (Array.isArray(value)) return value.filter((item) => typeof item === "string" && item.trim());
  return typeof value === "string" && value.trim() ? [value.trim()] : [];
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>\'"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[char]);
}

export function safeHttpsUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || !url.hostname) return null;
    return url.href;
  } catch {
    return null;
  }
}

function isValidComplete(record, detail) {
  if (detail.type === "ocr_template") {
    const hashes = list(detail.media_hashes);
    return detail.media_hash === "ocr-template" && hashes.length >= 2 &&
      (text(record.summary) || list(detail.shared_tokens).length > 0);
  }
  return Boolean(text(detail.media_hash) && text(detail.description));
}

function evidenceStatus(record) {
  const detail = isObject(record?.detail) ? record.detail : {};
  const status = record?.status ?? detail.status;
  if (status === "failed" || status === "pending" || status === "malformed") return status;
  if (status === "complete" || status == null) {
    return isValidComplete(record, detail) ? "complete" : "malformed";
  }
  return "malformed";
}

function rowTime(record) {
  const value = record?.created_at ?? record?.detail?.analyzed_at;
  const parsed = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : -1;
}

function evidenceItem(record, status = evidenceStatus(record)) {
  return {
    status,
    record,
    detail: isObject(record?.detail) ? record.detail : {},
    mediaHash: text(record?.detail?.media_hash),
    sourceUrl: record?.source_url ?? record?.detail?.post_urls?.[0] ?? null,
  };
}

export function mediaEvidenceItems(posts, evidence) {
  const references = new Map();
  for (const post of Array.isArray(posts) ? posts : []) {
    const hashes = Array.isArray(post?.media_hashes) ? post.media_hashes : [];
    for (const candidate of hashes) {
      const hash = text(candidate);
      if (hash && !references.has(hash)) references.set(hash, post);
    }
  }

  const byHash = new Map();
  const templates = new Map();
  const malformed = [];
  for (const record of Array.isArray(evidence) ? evidence : []) {
    if (record?.kind && record.kind !== "media") continue;
    const detail = isObject(record?.detail) ? record.detail : {};
    const hash = text(detail.media_hash);
    const isTemplate = detail.type === "ocr_template" && hash === "ocr-template";
    if (!hash) {
      malformed.push(evidenceItem(record, "malformed"));
      continue;
    }
    const bucket = isTemplate ? templates : byHash;
    const previous = bucket.get(hash);
    if (!previous || rowTime(record) >= rowTime(previous)) bucket.set(hash, record);
  }

  const items = [];
  for (const [hash, post] of references) {
    const record = byHash.get(hash);
    if (record) {
      items.push(evidenceItem(record));
      byHash.delete(hash);
    } else {
      items.push({ status: "pending", record: null, detail: {}, mediaHash: hash, sourceUrl: post?.url });
    }
  }
  for (const record of byHash.values()) items.push(evidenceItem(record));
  for (const record of templates.values()) items.push(evidenceItem(record));
  items.push(...malformed);
  return items.length ? items : [{ status: "missing", record: null, detail: {}, mediaHash: "" }];
}

function link(label, value) {
  const safeUrl = safeHttpsUrl(value);
  if (safeUrl) {
    return `<a class="source-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  }
  if (value) return `<div class="muted">${label} omitted: URL is not a safe HTTPS link.</div>`;
  return `<div class="muted">${label} unavailable.</div>`;
}

function timestamp(record, detail, formatDate) {
  const value = record?.created_at ?? detail?.analyzed_at;
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return "Timestamp unavailable";
  try {
    return formatDate(value);
  } catch {
    return "Timestamp unavailable";
  }
}

function statusHeader(status, recordedAt) {
  const labels = {
    complete: "Analysis complete",
    pending: "Analysis pending",
    failed: "Analysis failed",
    malformed: "Malformed media evidence",
    missing: "Media evidence unavailable",
  };
  return `<div class="row"><span class="evidence-kind">${labels[status]}</span><span class="muted">${escapeHtml(recordedAt)}</span></div>`;
}

function mediaCard(status, body, recordedAt, hash = "") {
  const hashLine = hash ? `<div class="evidence-detail"><strong>Media CID:</strong> ${escapeHtml(hash)}</div>` : "";
  return `<div class="evidence media-evidence media-${status}" data-media-status="${status}">${statusHeader(status, recordedAt)}${hashLine}${body}</div>`;
}

export function renderMediaEvidence(item, formatDate = (value) => value) {
  const status = MEDIA_STATES.has(item?.status) ? item.status : "malformed";
  const record = item?.record;
  const detail = isObject(item?.detail) ? item.detail : {};
  const hash = text(item?.mediaHash ?? detail.media_hash);
  const recordedAt = timestamp(record, detail, formatDate);

  if (status === "missing") {
    return mediaCard(status, "<p>No media hashes or media evidence were returned for this cluster.</p>", recordedAt);
  }
  if (status === "pending") {
    return mediaCard(
      status,
      `<p>No analysis result is stored for this media item yet. This is not a media finding.</p>${link("Open source post", item?.sourceUrl)}`,
      recordedAt,
      hash,
    );
  }
  if (status === "failed") {
    const code = /^[A-Za-z0-9_.-]{1,60}$/.test(text(detail.error_code)) ? detail.error_code : "unavailable";
    const stage = ["fetch", "analysis"].includes(detail.stage) ? detail.stage : "unknown";
    return mediaCard(
      status,
      `<p>Processing failed during ${escapeHtml(stage)} (${escapeHtml(code)}). No media assessment is available.</p>${link("Open source post", item?.sourceUrl ?? record?.source_url)}`,
      recordedAt,
      hash,
    );
  }
  if (status === "malformed" || !isValidComplete(record ?? {}, detail)) {
    return mediaCard(
      "malformed",
      "<p>The stored media record is incomplete or malformed. No media assessment is displayed.</p>",
      recordedAt,
      hash,
    );
  }

  const summary = text(record?.summary);
  const assessment = text(detail.description) ||
    (detail.type === "ocr_template" ? summary : "");
  const mediaType = detail.type === "ocr_template" ? "OCR template" :
    text(detail.media_type) || text(detail.media_kind) || "Media type unavailable";
  const model = text(record?.model) || text(detail.model) || text(detail.model_requested);
  const provider = text(detail.provider) || (model.toLowerCase().includes("nemotron") ? "NVIDIA" : "Provider unavailable");
  const confidence = typeof detail.confidence === "number" && Number.isFinite(detail.confidence) &&
    detail.confidence >= 0 && detail.confidence <= 1
    ? `${Math.round(detail.confidence * 100)}%`
    : "Confidence unavailable";
  const assessmentBlock = `<div class="evidence-detail"><strong>Nemotron assessment:</strong> ${escapeHtml(assessment)}</div>`;
  const summaryBlock = summary && summary !== assessment
    ? `<p><strong>Evidence summary:</strong> ${escapeHtml(summary)}</p>`
    : "";
  const textDetected = list(detail.extracted_text);
  const ocrBlock = detail.type === "ocr_template"
    ? `<div class="evidence-detail"><strong>OCR template:</strong> ${escapeHtml(list(detail.shared_tokens).join(" ") || "shared text unavailable")}</div><div class="evidence-detail"><strong>Variable OCR text:</strong> ${escapeHtml(list(detail.varying_tokens).join(", ") || "none recorded")}</div>`
    : `<div class="evidence-detail"><strong>Text detected (OCR):</strong> ${escapeHtml(textDetected.join(" / ") || "No OCR text extracted")}</div>`;
  const similarity = typeof detail.similarity === "number" && Number.isFinite(detail.similarity) &&
    detail.similarity >= 0 && detail.similarity <= 1
    ? `<div class="evidence-detail"><strong>Template similarity:</strong> ${Math.round(detail.similarity * 100)}%</div>`
    : "";
  const reuse = list(detail.reuse_cues);
  const manipulation = list(detail.manipulation_cues);
  const cueBlock = [
    reuse.length ? `<div class="evidence-detail"><strong>Reuse cues:</strong> ${escapeHtml(reuse.join("; "))}</div>` : "",
    manipulation.length ? `<div class="evidence-detail"><strong>Context cues:</strong> ${escapeHtml(manipulation.join("; "))}</div>` : "",
  ].join("");
  const attribution = model
    ? `<div class="muted">Model/provider: ${escapeHtml(provider)} / ${escapeHtml(model)}</div>`
    : `<div class="muted">Model/provider attribution unavailable.</div>`;
  const preview = link("Open media preview", detail.media_url);
  const source = link("Open source post", item?.sourceUrl ?? record?.source_url);
  const body = `${summaryBlock}${assessmentBlock}<div class="evidence-detail"><strong>Media type:</strong> ${escapeHtml(mediaType)}</div><div class="evidence-detail"><strong>Confidence:</strong> ${confidence}</div>${ocrBlock}${similarity}${cueBlock}${attribution}${preview}${source}`;
  return mediaCard("complete", body, recordedAt, hash);
}
