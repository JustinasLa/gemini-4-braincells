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
> screenshot: the caption, the pic and what got edited. one model for text and images, ~1.5 s a
> read, 10x faster than Gemini ([speed tests](docs/NEMOTRON-SPEED.md)). lowkey goated.

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
