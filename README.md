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
> **NVIDIA Nemotron 3.5 Super VL** — the eyes, self-hosted on Verda. It reads every meme and
> screenshot: the caption, the picture and what changed between edits. One model for text and
> image, about 1.5 s per reading, ten times faster than Gemini.

- **Google Gemini** — the investigator. Picks what to search and read, then explains each verdict
  citing only real evidence. Its embeddings catch reworded copies.
- **Oxylabs Web API** — the receipts. Reads pages that block scrapers, searches for copies and runs
  TinEye and Yandex image search.
- **Hostinger** — the live site, every service on one VPS.
- **OpenAI Codex** — GPT Astra built features and GPT Sol reviewed them.
- **Entire** — Sessions, Graph, Brain and Trails for every agent on the team.

## Credits

Built at Junction 2026 by:

- [Justinas Launikonis](https://github.com/JustinasLa)
- [Temuulen "Tibi" Norovpel](https://github.com/akmet777)
- [Kirill Jegosin](https://github.com/Ar7ti)
- [Valtteri Pertila](https://github.com/valtteripertila)
