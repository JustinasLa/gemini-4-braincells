import { sharedRuns } from "../data/shared";
import type { TraceCase, TraceNode } from "../types";
import { markRuns } from "./format";

/** Keep the existing highlights; make one navigable only when another post contains it. */
export function sharedTextHtml(node: TraceNode, c: TraceCase): string {
  const peers = c.nodes.filter((peer) => peer.id !== node.id && peer.text);
  // Shared highlights normally come from the submitted post. Prefer that destination.
  peers.sort((a, b) => Number(b.id === c.submittedId) - Number(a.id === c.submittedId));
  return markRuns(node.text, node.textMatch, (run) => {
    const peer = peers.find((candidate) => sharedRuns(run, candidate.text).includes(run));
    return peer ? { id: peer.id, name: peer.author.name } : undefined;
  });
}
