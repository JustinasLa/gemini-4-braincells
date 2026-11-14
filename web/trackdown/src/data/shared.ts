// Text two posts have in common, and which posts say exactly the same thing.
import type { TraceCase, TraceNode } from "../types";

interface Token { norm: string; start: number; end: number }
/** the words of a text, without case or surrounding punctuation; a token of punctuation alone is no word */
const tokens = (text: string): Token[] =>
  [...text.matchAll(/\S+/g)].map((m) => ({ norm: m[0].toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, ""), start: m.index, end: m.index + m[0].length })).filter((t) => t.norm);

/** The parts of `text`, as written there, that repeat at least `min` words in a row of `reference`.
 *  Words compare without case or surrounding punctuation. */
export function sharedRuns(text: string, reference: string, min = 3): string[] {
  if (!text || !reference) return [];
  const a = tokens(text.slice(0, 6000));
  const b = tokens(reference.slice(0, 6000));
  const at = new Map<string, number[]>();
  b.forEach((t, j) => (at.get(t.norm) ?? at.set(t.norm, []).get(t.norm)!).push(j));
  const runs: string[] = [];
  for (let i = 0; i < a.length;) {
    let best = 0;
    for (const j of at.get(a[i].norm) ?? []) {
      let k = 0;
      while (i + k < a.length && j + k < b.length && a[i + k].norm === b[j + k].norm) k++;
      if (k > best) best = k;
    }
    if (best >= min) {
      runs.push(text.slice(a[i].start, a[i + best - 1].end));
      i += best;
    } else i++;
  }
  return [...new Set(runs)];
}

/** Marks, on every other post, the text it shares word for word with the submitted post (the
 *  earliest one when nothing was submitted), so a copy and what was added to it show at a glance. */
export function highlightShared(c: TraceCase): TraceCase {
  const ref = c.nodes.find((n) => n.id === c.submittedId && n.text) ?? c.nodes.find((n) => n.id === c.originId && n.text);
  if (!ref) return c;
  for (const n of c.nodes) {
    if (n === ref || !n.text || n.imageMatch) continue;
    const runs = sharedRuns(n.text, ref.text);
    if (runs.length) n.textMatch = [...new Set([...(n.textMatch ?? []), ...runs])];
  }
  return c;
}

/** Posts that say exactly the same thing share a key; they are drawn as one stack. Empty for a post
 *  that never stacks: a picture match, or a text too short to mean anything. */
export function sameTextKey(n: TraceNode): string {
  if (n.imageMatch) return "";
  const text = (n.text ?? "").replace(/\s+/g, " ").trim();
  return text.length >= 12 ? text : "";
}
