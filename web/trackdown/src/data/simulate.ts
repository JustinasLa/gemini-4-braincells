// Offline fallback for "Trace a post": reuse the shape of a simulated case that fits the input
// and drop the user's text in. Always flagged synthetic.
import type { TraceCase } from "../types";
import { simulatedCases } from "./cases";

export function simulateFromInput(input: string): TraceCase {
  const text = input.trim() || "Shared without context";
  const all = simulatedCases();
  const pick = /\$|token|coin|crypto|pump|partner/i.test(text)
    ? "glow"
    : /photo|image|flood|picture|pic|video|old/i.test(text)
      ? "flood"
      : /breaking|bridge|crash|accident|fire|police|storm/i.test(text)
        ? "bridge"
        : "tap-water";
  const base = all.find((c) => c.id === pick)!;
  const clone: TraceCase = JSON.parse(JSON.stringify(base));
  const sub = clone.nodes.find((n) => n.id === clone.submittedId)!;
  const burst = sub.cluster ? new Set(clone.clusters.find((c) => c.id === sub.cluster)?.members ?? []) : new Set<string>();
  const headline = text.replace(/https?:\/\/\S+/g, "").split(/\s+/).slice(0, 9).join(" ");
  for (const n of clone.nodes) {
    if (n.id === sub.id || (burst.has(n.id) && n.role !== "source")) n.text = text;
    else if (n.role === "origin" || n.role === "mutation") n.text = n.role === "origin" ? headline : text;
    // the user's claim is what travels: put it in every copied screenshot / link card on the trail
    if (n.media && n.role !== "source" && n.media.kind !== "photo" && n.media.kind !== "video") n.media = { ...n.media, title: headline };
  }
  sub.author = { name: "Your post", handle: "you.bsky.social", hue: 0, ageDays: sub.author.ageDays, followers: sub.author.followers };
  const words = text.replace(/https?:\/\/\S+/g, "").split(/\s+/).filter(Boolean);
  clone.id = `sim-${Date.now()}`;
  clone.title = words.slice(0, 6).join(" ") + (words.length > 6 ? "…" : "") || "Your post";
  clone.kicker = `Simulated · ${base.kicker}`;
  clone.summary = `Simulated run: your text dropped into the shape of “${base.title}” to show the flow. The live API wasn't reachable, so this is not a verdict on your post.`;
  return clone;
}
