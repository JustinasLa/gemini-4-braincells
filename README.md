# Shill or Chill

> Shill or chill? We check the receipts.

Traces memes to their source and spots coordinated spread.

Live: **https://noise-radar.srv2159172.hstgr.cloud**

## Features

- **Meme trace** — earliest copy and every edit.
- **Link lookup** — receipts for any post.
- **Meme swarms** — pictures pushed by many accounts.

## Tech stack

> [!IMPORTANT]
> **NVIDIA Nemotron 3.5 Super VL** — the eyes, running on our own Verda box. reads every meme and
> screenshot: the caption, the pic and what got edited. one model for text and images. lowkey goated.

Nemotron vs Gemini on 60 real clusters (2026-11-14, [all speed tests](docs/NEMOTRON-SPEED.md)):

| Model | Mean | Slowest | Failed calls | Coordinated, taken down (of 28) |
|---|---|---|---|---|
| Gemini Hydrogen, default thinking | 12.0 s | 22.8 s | 1 | 17 |
| Gemini Hydrogen, LOW thinking | 6.2 s | 10.2 s | 1 | 15 |
| **Nemotron** | **1.5 s** | **2.3 s** | **0** | 12 |
| Cascade (Nemotron first, Gemini on disagreement) | 5.2 s | – | – | **18** |

- **Google Gemini** — the detective. decides what to search and what to open, then explains the
  verdict with real receipts only, no making stuff up. also catches copies that got reworded.
- **Oxylabs Web API** — the receipts guy. gets into pages that block bots, hunts down copies and
  runs reverse image search on TinEye and Yandex.
- **Hostinger** — where it lives. the whole thing runs on one VPS, live rn.
- **OpenAI Codex** — Astra built stuff, Sol roasted it until it was right.
- **Entire** — Sessions, Graph, Brain and Trails, so every agent on the team knows what's going on.

## Credits

Built at Junction 2026 by:

- [Justinas Launikonis](https://github.com/JustinasLa)
- [Temuulen "Tibi" Norovpel](https://github.com/akmet777)
- [Kirill Jegosin](https://github.com/Ar7ti)
- [Valtteri Pertila](https://github.com/valtteripertila)
