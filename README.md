# Shill or Chill

> Shill or chill? We check the receipts.

Shill or Chill traces memes and posts back to the earliest copy it can find and shows whether their
spread looks coordinated. It judges how accounts act together, never what a post says, and every
call links to the posts behind it. When the evidence is thin it says "Can't tell".

Live at **https://noise-radar.srv2159172.hstgr.cloud**, where the web app, Trackdown, runs.

## Features

- **Meme trace** — upload a meme or screenshot to find its earliest known copy, the edited versions
  and what changed in each, with dates from X post ids, archive snapshots and Know Your Meme.
- **Link lookup** — paste an X or Bluesky post, a profile or a web page for live receipts: the post,
  its author's history and every copy found on the web.
- **Screenshot check** — a screenshot of a post is compared with the real post: matches, differs or
  not found.
- **Meme swarms** — a wall of pictures posted by many Bluesky accounts in the last day, each called
  Shill, Chill or Can't tell from how it was posted.
- **Live detector** — watches the Bluesky firehose for accounts acting together and tests every
  cluster against a null model and organic explanations.
- **Explained verdicts** — Gemini and Nemotron explain each call and cite only real evidence;
  statistics decide, models explain.
- **Measured accuracy** — recall and false-positive rates come from held-out data and are served at
  [`/api/metrics`](https://noise-radar.srv2159172.hstgr.cloud/api/metrics), never typed in.

## Credits

Built at Junction 2026 in Espoo by:

- **Justinas Launikonis** ([@JustinasLa](https://github.com/JustinasLa))
- **Temuulen "Tibi" Norovpel** ([@akmet777](https://github.com/akmet777))
- **Kirill Jegosin** ([@Ar7ti](https://github.com/Ar7ti))
- **Valtteri Pertila** ([@valtteripertila](https://github.com/valtteripertila))

Powered by Google Gemini, NVIDIA Nemotron 3.5 Super VL on Verda, the Oxylabs Web API, Hostinger and
Bluesky's public Jetstream, with Entire for agent sessions. Written with help from Claude, Codex and
GitHub Copilot. Lookups also read FxTwitter, TinEye, Yandex, Know Your Meme, Wikipedia and the
Internet Archive. Trackdown's backdrop is Botticelli's *The Birth of Venus*.
