# Shill or Chill

> Shill or chill? We check the receipts.

Traces memes to their source and spots coordinated spread.

Live: **https://noise-radar.srv2159172.hstgr.cloud**

## Features

- **Meme trace** — earliest copy and every edit.
- **Link lookup** — receipts for any post.
- **Meme swarms** — pictures pushed by many accounts.

## Tech stack

- **NVIDIA Nemotron 3.5 Super VL** (self-hosted on Verda with vLLM) — the eyes. It reads every meme
  and screenshot: transcribes the caption, describes the picture, says what changed between a meme
  and its edited copy, and writes the cited reading of a lookup. One vision-language model handles
  both the text and the image, and with guided JSON and reasoning off it writes the reading in
  about 1.5 s, ten times faster than the second opinion from Gemini.
- **Google Gemini** — the investigator. In a function-calling loop it decides what to search and
  which pages to open through Oxylabs, then explains a verdict citing only real evidence. It
  digs on news (about 3 tool calls per cluster) and skips the web on obvious spam, with 100% valid
  JSON and citations in our tests. Its embeddings also catch reworded copies of the same text.
- **Oxylabs Web API** — the receipts. It reads X posts, profiles and web pages that block plain
  scrapers, searches the web, X and Bluesky for copies, and runs reverse image search on TinEye
  and Yandex. Every fact comes back with the URL it was read from.
- **Bluesky Jetstream** — the live feed. The whole network's posts, reposts, likes and follows in
  real time, public and read-only: exactly what spotting accounts that move together needs.
- **FxTwitter** — X posts in under a second, with exact counts, full long-post text and posts X
  hides from logged-out visitors. Oxylabs takes over when it fails.
- **imagehash + OpenCV ORB** — tells copies apart. Perceptual hashes sort a copy into same,
  resized, cropped or edited; ORB keypoint matching still finds the original photo under new
  captions, frames and pasted faces, in 20-90 ms per pair.
- **MinHash LSH, SciPy and Leiden** — the detector's math. MinHash finds near-identical text at
  firehose speed, Leiden splits the account graph into groups, and a permutation test checks
  whether a burst beats chance, so breaking news isn't mistaken for a bot ring.
- **PostgreSQL + FastAPI** — one database for everything and an API that streams each lookup step
  to the page (server-sent events) the moment it lands.
- **TypeScript + Vite** — Trackdown, the web app: every copy is a card on an infinite canvas over
  Botticelli's *Birth of Venus*.
- **Hostinger VPS, Docker Compose and Caddy** — every service in one compose file on one box,
  deployed with a single script.
- **Entire** — links every agent session to the commit it produced, so the team can see why code
  changed.

## Credits

Built at Junction 2026 by:

- [Justinas Launikonis](https://github.com/JustinasLa)
- [Temuulen "Tibi" Norovpel](https://github.com/akmet777)
- [Kirill Jegosin](https://github.com/Ar7ti)
- [Valtteri Pertila](https://github.com/valtteripertila)
