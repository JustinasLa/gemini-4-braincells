# Shill or Chill: submission demo video, detailed working prompt (v4)

Written 14 Nov 2026 from Kirill's `PROMPT-new-demo.md` plus everything learned while building v3.
This is the single source of truth for anyone (human or agent) improving the video.

## 1. Goal and the bar

Make the hackathon submission video for **Shill or Chill** (Junction 2026, Hostinger challenge "Decode the Noise").
Judges watch it once, probably at 1x, probably with sound. They score honesty and false positives above UI polish.

The work is done only when a strict reviewer can say **"Yes, this is a winning demo"**, meaning all of these hold:

1. **Hook**: by second 5 the viewer has a question they want answered; by second 30 they have been surprised once and smiled once.
2. **Clarity**: someone who has never seen the product can say after one viewing what it does, how it decides, and why to trust it.
3. **Honesty**: every number and claim on screen or spoken is in section 4. The blind spot is stated out loud.
4. **Proof**: real product footage (desktop and phone) and real data are on screen, not only motion graphics.
5. **Pace**: no stretch over ~2.5 s without something changing; nothing a viewer must read leaves before it can be read
   (0.5 s + 0.3 s per word).
6. **Craft**: no overlapping or clipped text, no wrong font fallback, no stray browser chrome or grey bars, captions never
   cover key content, QR code scannable and held at least 6 s.
7. **Sound**: narration has no stutters or mispronunciations; music fits a forensic, confident tone and never fights the
   voice; master is -14 LUFS, true peak at or under -1 dBTP.
8. **Format**: 1920x1080, 30 fps, English, burned-in word-level captions, at most 2:00 (target 1:45 to 1:59).

## 2. Deliverables

Everything goes to `/Users/macbookairm4/junction-espoo/demo-video/v3/`:

- `brag.mp4` (final video, poster baked as frame 0), `brag.jpg` (poster), `share-copy.txt`
- `brag-plan.md`, `composition-brief.md`, `vo-script.json`
- `composition/`: the HyperFrames project (`index.html`, `compositions/*.html`, `assets/`)

Keep the previous render as `brag.prev.mp4` before overwriting, so versions can be compared.

## 3. The story (current cut, 119 s)

| # | Scene (file) | Time | What happens |
|---|---|---|---|
| 1 | Game (`game.html`) | 0–31 s | Split screen A/B of real Bluesky posts, no labels. "One of them is a coordinated ring. Which one?" Countdown 3-2-1. Reveal: A = open weather service, Can't tell, NOT ACCUSED. B = cluster #10876, Shill. The joke: two accounts post the identical caption "this is just my chill page 😌". Odds ≈ 1 in 2,000,000. Then real footage of the cluster page on the live site. "Your gut was wrong. Ours isn't a gut. It's statistics." |
| 2 | Dürer (`durer.html`) | 31–47 s | 1515 woodcut, armor box, push into the horn on its back, wall of copies. "Wrong? Yes. Coordinated? No. Copying isn't a conspiracy. Coordination is. And it leaves receipts." |
| 3 | How it decides (`how.html`) | 47–68 s | Live firehose (real post lines) → accounts group by shared text/links/images/likes → four families (content, timing, network, identity) vs pure chance → verdict needs two families, p ≤ 0.01, no organic explanation → Gemini writes the explanation, never the verdict. |
| 4 | Trace anything (`trace.html`) | 68–84 s | Desktop + real iPhone footage. The woodcut's first line next to NVIDIA Nemotron 3.5 Super VL's verbatim reading: "Nach Chriffus gepurt.1513. Jar.2di.j.May. Hat man dem großmechtigen Kunig von Porzug." Oxylabs Web API finds every copy: earliest, every variation, a receipt each. |
| 5 | Red team (`redteam.html`) | 84–98 s | Gemini injected 108 fake campaigns into real traffic. Tactics: 12 languages, look-alike letters, swapped links, ring hidden in a crowd, reply floods, near-copies. Two holes found (12-language campaign, ring hidden in a crowd), both closed (8/8 now). Blind spot: slow campaigns from aged accounts, L3 0/35. |
| 6 | Grades (`grades.html`) | 98–111 s | 0/211 real organic bursts flagged (also 0/84 synthetic). Bluesky later took down 6.5% (60/922) of flagged accounts vs 0/3,290 random. |
| 7 | Outro (`outro.html`) | 111–119 s | "Shill or chill? We check the receipts." Big QR to https://noise-radar.srv2159172.hstgr.cloud, "SCAN · TRACE ONE YOURSELF", URL, "Live on Hostinger", credits. |

The hook cluster was changed on Kirill's request: **no sleeping-pill ring**. Use a recent real cluster that is also funny.
Current choice: #10876 (data: `https://noise-radar.srv2159172.hstgr.cloud/api/clusters/10876`).

Do not copy `reference_teammate.mp4` shot for shot. It opens on the rhino; ours opens on the game.

## 4. Facts that may appear (nothing else)

Verified by Kirill on 14 Nov 2026:
- 23 accounts in cluster 9890 (weather service; verdict Can't tell)
- 0/211 real organic bursts flagged; 0/84 synthetic organic
- 6.5% (60/922) of flagged accounts later taken down by Bluesky vs 0/3,290 random accounts
- 6.7% for the strongest Can't-tell clusters
- 108 red-team cases; 2 holes closed; 8/8 now
- L1 35/35, L2 34/35, L3 0/35
- Ingest about 1 s behind live
- 1 in 10,000,000 (p = 9.7e-08) belongs to cluster 9634 only (the pill ring, no longer shown)

Read from the live public API on 14 Nov 2026 for cluster #10876 (flagged to Kirill as outside his list):
- status coordinated (Shill), 6 accounts, p = 5.25e-07 → "about 1 in 2,000,000"
- 5 of 6 accounts younger than 30 days, median age 2 days; 4 of 6 posted the same link; window 17:11–18:05 UTC
- handles: moondaisybb, thelunaadivinex, lilmadzpetalxo, lunamessyx, madslunnyx, madsposhxo

Open question for Kirill: the live site now shows 11 accounts for #9890; his verified list and the old recording say 23.

## 5. Wording guard

- Never call an account or a person "a shill" or "a bot". Only a **cluster's verdict** is Shill.
- Describe behaviour: "6 accounts, same link, mostly two days old".
- Use "coordinated ring", not "bot farm", for the game question.
- Gemini writes explanations; statistics decide verdicts. Never imply an LLM decides.

## 6. Look

- Background #050506, text #f2efe9, CV green #3fe07a, Shill #e2533c, Chill #5fc9a8, Can't tell #9aa6bf, muted #b4b0a8.
- Fonts: Michroma (wordmark and labels, file in `assets/fonts/`, must be the **latin** subset), Inter, JetBrains Mono.
- Motion language: machine-vision corner brackets, boxes with tags, scan lines. Arrivals on long ease-outs, titles dead
  still once landed, each reveal timed to the spoken word.
- Reading text at least 28 px; captions 44 px Inter Bold on a dark scrim, bottom 52 px, one group at a time.

## 7. Voice

- ElevenLabs, voice "Normal English from USA" `Jef7rEheVYNs9jHPVQEV`, model `eleven_v4`, stability 0.5, similarity 0.8,
  style 0, speaker boost on, speed 1.0, output `mp3_44100_128` (192 kbps is not on this plan).
- One file per scene via `/with-timestamps`, no `previous_text`/`next_text`. No ellipses, no dashes for pauses. Short sentences.
- Key file: `/Users/macbookairm4/.t3/userdata/secrets/files/5554274de6c84f8d99c6b3872a45e68e` (never print it).
- Generator: scratchpad `tts.py <vo-script.json> <out-dir> [scene ids]`. After any regeneration, transcribe with
  `npx hyperframes transcribe <file> --model small.en --json` and compare to the script word for word.
- Word timings: `composition/assets/vo/<scene>.words.json`. Scene offsets on the root timeline:
  s1 0.3, s2 8.5, s3 31.2, s4 47.4, s5 68.4, s6 84.0, s7 98.6, s8 111.0. If a scene's audio changes length,
  shift every later offset, the scene hosts in `index.html`, the captions and the mix.

## 8. Music and mix

- Music changed (v4): "Rising Tide" by Kevin MacLeod, CC BY 4.0. v3 used "Happy Beats / Business Moves Vol. 12", too cheerful.
- Wanted: tense, minimal, confident pulse; investigative or tech-documentary feel; no vocals; steady enough to sit under
  speech for two minutes; a lift available for the verdict (≈22 s) and the outro (≈111 s).
- Licence must allow use in a public hackathon video with attribution at most (CC0, CC BY, or equivalent). Record the
  title, author, licence and source URL; the credit line in the outro must match.
- Mix: voice on top, music sidechain-ducked under it, sparse SFX only on visible events, master -14 LUFS, TP ≤ -1 dBTP.
  The mix is one pre-rendered file `composition/assets/mix.wav` built with ffmpeg.

## 9. Build and check loop

1. Edit scenes in `composition/compositions/`.
2. `cd composition && npx hyperframes check --json` must report `ok: true` with zero errors.
3. `npx hyperframes snapshot --at <times> --no-end --output <scratchpad dir>` and look at the frames.
4. `npx hyperframes render --output ../brag.mp4`, then verify duration, loudness and a contact sheet of the real file.
5. Pick the poster at a settled, strong frame; bake it as frame 0.
6. Independent review against section 1. Fix everything rated blocker or major. Repeat until the reviewer says
   "Yes, this is a winning demo" with no blockers and no majors.

## 10. Constraints

- Do not clone or run the hackathon repo on this Mac. Only browse the public site and its public API.
- Re-record desktop with Playwright + system Chrome at 1920x1080; phone with the iOS Simulator.
- Playwright recordings come out with a grey band at the bottom: crop to the top 990 px and rescale.
- Do not publish, upload or push anything. Local files only.
