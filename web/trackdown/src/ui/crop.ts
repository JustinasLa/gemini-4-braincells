// Avatars are detected patches of the painting: one of the ORB keypoints in assets/venus-cv.json.
// Days-old accounts get an empty, hatched box (nothing detected yet).
import cv from "../assets/venus-cv.json";
import bannerUrl from "../assets/venus-banner.webp";
import { hashStr } from "../data/build";
import { esc } from "./format";

const D = cv as { w: number; h: number; points: { x: number; y: number; s: number; score: number }[] };

export function cropStyle(seed: string, size: number): string {
  const p = D.points[hashStr(seed) % D.points.length];
  const side = Math.max(40, p.s * D.w);
  const k = size / side;
  const bx = -(p.x * D.w - side / 2) * k;
  const by = -(p.y * D.h - side / 2) * k;
  return `background-image:url(${bannerUrl});background-size:${(D.w * k).toFixed(1)}px ${(D.h * k).toFixed(1)}px;background-position:${bx.toFixed(1)}px ${by.toFixed(1)}px`;
}

/** Templated handles (glow_fan_0412…) share one patch, so a wave of look-alikes looks alike. */
export function cropSeed(handle: string, templated?: boolean) {
  return templated ? handle.replace(/\d+/g, "#") : handle;
}

export function cropHtml(handle: string, opts: { templated?: boolean; blank?: boolean; size?: number; title?: string } = {}) {
  const size = opts.size ?? 30;
  if (opts.blank) return `<span class="crop blank" style="width:${size}px;height:${size}px" title="${esc(opts.title ?? "new account")}"></span>`;
  return `<span class="crop" style="width:${size}px;height:${size}px;${cropStyle(cropSeed(handle, opts.templated), size)}"></span>`;
}

/** A thumbnail crop for a case (rail): a bigger region around a keypoint. */
export function thumbStyle(seed: string, size: number): { style: string; score: string } {
  const p = D.points[hashStr(seed) % D.points.length];
  const side = 150;
  const k = size / side;
  const bx = -Math.min(Math.max(p.x * D.w - side / 2, 0), D.w - side) * k;
  const by = -Math.min(Math.max(p.y * D.h - side / 2, 0), D.h - side) * k;
  return { style: `background-image:url(${bannerUrl});background-size:${(D.w * k).toFixed(1)}px ${(D.h * k).toFixed(1)}px;background-position:${bx.toFixed(1)}px ${by.toFixed(1)}px`, score: p.score.toFixed(4) };
}
