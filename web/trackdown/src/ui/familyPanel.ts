// The family tree in the meme panel, in the panel's own list style: where the photograph came from,
// its branches and variants, and where the submission sits. The tree itself is drawn on the canvas.
import { BRANCH_NAME, dateBasis, dateLabel, familyOf, memberDate, memberDomain, type FamilyNode } from "../data/family";
import type { TraceCase } from "../types";
import { esc } from "./format";

const quote = (text: string) => `“${esc(text)}”`;

export function familySectionHtml(trace: TraceCase): string {
  const step = trace.lookup?.steps.find((candidate) => candidate.id === "family");
  const family = familyOf(trace.lookup);
  if (!family) {
    if (step && (step.state === "pending" || step.state === "running")) return `<h3>Family tree <small>how this meme was made</small></h3><p class="meme-scanning" role="status">Matching the photograph in every copy, reading captions and sources…</p>`;
    if (step && step.state !== "pending" && step.detail) return `<h3>Family tree <small>how this meme was made</small></h3><p class="meme-note">${esc(step.detail)}</p>`;
    return "";
  }
  const members = new Map(family.members.map((member) => [member.id, member]));
  const nodes = new Map(family.nodes.map((node) => [node.id, node]));
  const source = nodes.get("source");
  const sub = nodes.get("submitted");
  const first = (node: FamilyNode) => members.get(node.members[0]);
  // a family card on the canvas carries its earliest copy's id: the list opens that comparison
  const item = (node: FamilyNode) => {
    const member = first(node);
    const title = node.branch === "face_swap" || node.branch === "edit" ? esc(node.label) : quote(node.label);
    const sub = [memberDomain(member), memberDate(member), node.aimed_at ? `mocks ${node.aimed_at}` : "", node.members.length > 1 ? `+${node.members.length - 1}` : ""].filter(Boolean).join(" · ");
    return member ? `<button class="meme-list-item" data-meme-compare="${esc(member.id)}"><span>${title}</span><small>${esc(sub)}</small><span class="meme-list-arrow" aria-hidden="true">↗</span></button>` : "";
  };
  const milestones = family.nodes.filter((node) => node.kind === "milestone");
  const branches = family.nodes.filter((node) => node.kind === "branch");
  const lineage = family.lineage.map((id) => nodes.get(id)).filter((node): node is FamilyNode => !!node).map((node) =>
    node.kind === "milestone" ? dateLabel(node.date) : node.kind === "source" ? "source photo" : node.kind === "branch" ? node.label.toLowerCase() : node.kind === "variant" ? quote(node.label) : "your picture").join(" → ");
  const sourceMember = source ? first(source) : undefined;
  return `<h3>Family tree <small>how this meme was made</small></h3>
    <p class="meme-note">${esc(lineage)}</p>
    ${sub ? `<div class="meme-finding" style="--vc:${sub.new ? "#e2533c" : "#c9c4bb"}"><div class="meme-finding-word">${sub.new ? "NEW BRANCH" : "KNOWN BRANCH"}</div><p>${esc(sub.change ?? "")}${sub.aimed_at ? ` Aimed at ${esc(sub.aimed_at)}.` : ""}</p></div>` : ""}
    ${milestones.length ? `<div class="meme-group"><h4>Documented history <span>${milestones.length}</span></h4>${milestones.map((m) => `<a class="meme-list-item" href="${esc(m.quotes[0]?.url ?? "#")}" target="_blank" rel="noopener noreferrer"><span>${esc(dateLabel(m.date))}</span><small>${quote(m.label)} · ${esc([...new Set(m.quotes.map((q) => q.source))].join(" + "))}</small><span class="meme-list-arrow" aria-hidden="true">↗</span></a>`).join("")}</div>` : ""}
    <div class="meme-group"><h4>Source photo</h4>${sourceMember && source ? item(source) : '<p class="meme-note">No copy of the plain photograph found.</p>'}${sourceMember ? `<p class="meme-note">${esc(memberDate(sourceMember))}: ${esc(dateBasis(sourceMember.time_source))}.</p>` : ""}</div>
    ${branches.map((branch) => `<div class="meme-group"><h4>${esc(BRANCH_NAME[branch.branch ?? "caption"])} <span>${family.nodes.filter((n) => n.parent === branch.id && n.kind === "variant").length}</span></h4>${family.nodes.filter((n) => n.parent === branch.id && n.kind === "variant").map(item).join("")}</div>`).join("")}
    ${family.events.length ? `<details class="meme-different"><summary>Later, as documented · ${family.events.length}</summary>${family.events.map((m) => `<a class="meme-list-item" href="${esc(m.quotes[0]?.url ?? "#")}" target="_blank" rel="noopener noreferrer"><span>${esc(dateLabel(m.date))}</span><small>${quote(m.label)}</small><span class="meme-list-arrow" aria-hidden="true">↗</span></a>`).join("")}</details>` : ""}
    ${family.sources.length ? `<p class="meme-note">History quoted from ${family.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.source)}</a>`).join(" and ")}: their own picture matched this photograph.</p>` : ""}
    <p class="meme-note">Every family picture was downloaded during this lookup and feature-matched to the same photograph. Dates are upper bounds. Branches group copies by what they add; they don't show who copied whom.${family.notes.length ? " " + esc(family.notes.join(" ")) : ""}</p>`;
}
