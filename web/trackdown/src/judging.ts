const esc = (value: unknown) => String(value ?? "").replace(/[&<>"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
}[char]!));

const page = (title: string, body: string) => {
  const element = document.createElement("main");
  element.id = "judging-view";
  Object.assign(element.style, {
    position: "fixed", inset: "0", zIndex: "2000", overflow: "auto",
    background: "#111", color: "#f4f0e8", padding: "20px",
    fontFamily: "system-ui", lineHeight: "1.55",
  });
  element.innerHTML = '<a href="/">← Trackdown</a><h1>' + title + '</h1><div id="content">' + body + "</div>";
  document.body.append(element);
  return element.querySelector<HTMLElement>("#content")!;
};

const showMethodology = () => {
  page("Methodology & limits", '<p>Evaluation replays recorded Jetstream hours through the live detector entry point. Recall uses injected synthetic campaigns and a 95% Wilson interval. False-positive rate uses synthetic hard negatives and organic bursts selected without looking at detector output.</p><p>Odd UTC hours are held out; even hours are for development. Calibration is measured on synthetic cases only. Live precision is not reported until live clusters have hand labels.</p><h2>Known limits</h2><ul><li>Slow-drip campaigns with aged accounts and paraphrased text mostly come out “Can’t tell”.</li><li>Only timing has a null model; content and identity base rates are not modelled.</li><li>Declared automation can look coordinated; guards catch some cases, not all.</li><li>Rings pushing already viral items are invisible because popular items are dropped.</li><li>Very short texts and unspaced scripts may not produce text keys.</li><li>Confidence is a heuristic until calibrated on labelled data.</li></ul><p>Coordination evidence is not proof of who is behind it or why.</p>');
};

export async function showJudgingView(view: string) {
  if (view === "methodology") { showMethodology(); return; }

  const feed = view === "feed";
  const content = page(feed ? "Live cluster feed" : "Measured accuracy", "");
  const loading = feed ? "Loading live clusters..." : "Loading metrics...";

  async function load() {
    content.innerHTML = '<p role="status">' + loading + "</p>";
    try {
      const response = await fetch(feed ? "/api/clusters?include_synthetic=false&limit=50" : "/api/metrics", {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("The API returned HTTP " + response.status + ".");
      const rows: unknown = await response.json();
      if (!Array.isArray(rows)) throw new Error("The API returned data in an unexpected format.");

      if (feed) {
        content.innerHTML = '<p>Newest real detector clusters. Synthetic evaluation clusters excluded.</p><p><a href="/api/feed.atom">Open Atom feed</a></p>' +
          (rows.length ? rows.map((row: any) =>
            '<article><b>' + esc(row.status) + " · Cluster #" + esc(row.id) + '</b><p>' +
            (row.size == null ? "Account count unavailable" : esc(row.size) + " accounts") +
            " · score " + (typeof row.score === "number" && Number.isFinite(row.score) ? row.score.toFixed(2) : "not reported") +
            " · " + esc(row.window_end) + '</p><p>' +
            esc((row.top_signals ?? []).map((signal: any) => signal.summary).filter(Boolean).join(" · ")) +
            '</p><a href="/?cluster=' + encodeURIComponent(String(row.id)) + '">Open receipts</a></article>',
          ).join("") : "<p>The live API returned no clusters.</p>");
      } else {
        content.innerHTML = rows.length ? rows.map((row: any) =>
          '<article><h2>' + esc(row.dataset) + " · " + esc(row.split) + '</h2><p>Precision ' +
          (row.precision == null ? "not reported" : (row.precision * 100).toFixed(1) + "%") +
          " · Recall " + (row.recall == null ? "not reported" : (row.recall * 100).toFixed(1) + "%") +
          " · F1 " + (row.f1 == null ? "not reported" : (row.f1 * 100).toFixed(1) + "%") +
          " · False-positive rate " + (row.fpr == null ? "not reported" : (row.fpr * 100).toFixed(1) + "%") +
          '</p><p>n=' + esc(row.n) + " · " + esc(row.created_at) + "</p></article>",
        ).join("") : "<p>No evaluation run has been published to this deployment yet.</p>";
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Live API data is unavailable right now.";
      content.innerHTML = '<div class="judge-error"><p role="alert">' + esc(message) +
        ' Try again when the API is available.</p><button class="btn" type="button" data-retry>Try again</button></div>';
      content.querySelector<HTMLButtonElement>("[data-retry]")!.addEventListener("click", () => void load());
    }
  }

  await load();
}
