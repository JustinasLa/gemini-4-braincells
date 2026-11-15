# Nemotron speed tests

Why NVIDIA Nemotron 3.5 Super VL (self-hosted on Verda, vLLM) is the first model on the fast
paths. We timed it against Gemini twice, on real data, on 2026-11-14. Nemotron was 5 to 10
times faster both times.

## 1. Reading a looked-up link

The lookup's cited reading of a post or page. Five real inputs (an X post, an X post with an
image, an AP News Bluesky post, an AP article, a sentence) on the live endpoints. Details:
[lookup guide, Timings](../lookup/README.md#timings-2026-11-14-live-endpoints-seconds-from-the-request).

| Model | Time per reading | Valid output |
|---|---|---|
| Gemini (gemini-4-hydrogen) | 10-21 s | not recorded |
| Nemotron, reasoning on | 7.5-15 s | 1 reading came back empty (all 8192 tokens spent reasoning) |
| **Nemotron, reasoning off (shipped)** | **1.2-2.0 s** | **20 of 20 valid JSON, cited only real receipts** |

The median time until the reading shows on screen fell from 32.5 s to 14.6 s. Nemotron
reached the same verdict as Gemini on 3 of the 4 lookups both read.

## 2. Cluster verdicts

A verdict for a candidate cluster: 60 real clusters, the same single-shot prompt for every
model, no web. The last column counts clusters the model called coordinated where Bluesky took
down a member account within 24 h. There are no hand labels yet, so this is the closest thing
to a correctness score (see [analyst guide](../analyst/README.md#correctness-scored-against-blueskys-own-takedowns)).

| Model | Mean | Failed calls | Coordinated, taken down (of 28) |
|---|---|---|---|
| Gemini Hydrogen, default thinking | 12.0 s | 1 | 17 |
| Gemini Hydrogen, LOW thinking | 6.2 s | 1 | 15 |
| **Nemotron** | **1.5 s** | **0** | 12 |
| Cascade (Nemotron first, Gemini on disagreement) | 5.2 s | – | **18** |

The model timings are computed from the raw runs in
[`analyst/gate_takedown_results.json`](../analyst/gate_takedown_results.json) (`options`),
leaving out the one failed Gemini call. The cascade row is the separate live run in the same
file (`cascade_live`).

## Why a cascade

Nemotron alone is fast but less accurate (12 of 28 against Hydrogen's 17), because it mostly
repeats the detector. So on clusters where a web check can't change the call, Nemotron answers
first in about 2 s, and its verdict stands only when it agrees with Gemini's decision-1 status.
Otherwise Gemini Hydrogen takes over ([decision gate](../analyst/README.md#decision-gate-fast-where-the-web-cannot-matter-cross-checked-where-it-is-fast)).
On the live run Gemini took over on 12 of 59 clusters, and the cascade matched or beat every
single model at $0.0055 of Gemini per cluster.
