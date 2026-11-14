# HANDOFF → Justin: overnight run of the demo video fixes

You are a coding agent running UNATTENDED overnight on Justin's laptop.
Nobody will answer questions until morning. Do not stop to ask. When
something is unclear, make the most reasonable choice, write it down in
`HANDOFF-NOTES.md` and keep going.

## 0. Setup (do this first)
1. Find the demo video project in this repo (the code or motion project
   that renders our hackathon demo video, e.g. Remotion, HyperFrames,
   ffmpeg scripts, or similar). Find how it renders and where the current
   output video is.
2. Create a new git branch: `demo-video-fixes-overnight`. Commit often
   with clear messages. Never push to main, never deploy, never delete
   user files.
3. Assets Justin was given (screenshots, meme screenshot, current video)
   are in: `./handoff-assets/`. If that folder is missing, search the repo
   for them. If you still can't find them, do everything else and list
   what's missing in the notes.
4. Read the live web app's styling (CSS variables, Tailwind config,
   theme files, fonts, colors) and use it as the ONLY source of truth for
   the video's look.

## 1. Global rules (every screen)
- Use OUR app's styling exactly: same colors, fonts and UI look as the
  web and mobile app. Don't invent a new style.
- Use about 80% less text. Short headlines only, no paragraphs. Viewers
  have seconds per screen, so show the main idea.
- NEVER use the word "trace". Say "search" (e.g. "Search it yourself").
  "Paper trail" is allowed.
- NEVER use "receipt". Use "trail", "post" or "link" instead.
- Remove EVERY "live" word ("live site", "live Bluesky", "live web
  search", "live demo"). The date already shows it's live.
- Remove the flashing red "live" dot everywhere.
- No beta labels, no version numbers.
- No generic AI-looking graphics or tilted/rotated cards. Use real app
  screens, real images and motion.
- Backgrounds: keep the app's look, but do NOT reuse the Renaissance
  painting of the woman (it's already in the app). Use a DIFFERENT
  public-domain Renaissance painting, so viewers can tell the video
  from the app itself.
- Music: replace the current track (bad and too loud) with a better,
  royalty-free track mixed much quieter, under the visuals.

## 2. Scene changes

### Opening (first ~2 seconds)
- Redesign it: add images, use the app's colors and style, cut the text.

### Second screen
- Cut about 80% of the text and keep only the core idea.
- Replace the boring graphics with our real UI (web and mobile).

### NEW: meme proof-of-concept scene
- Show the app searching an old meme back to its original (Know Your
  Meme style). This is a proof of concept: "Today it finds the original
  of a meme. With more time, it will do the same for political posts."
- Use Hostinger's own meme (screenshot in assets). Do NOT use the
  screenshot itself. RECREATE the meme cleanly with the same image and
  the same text, including the years 2024 / 2025 / 2026. Remove the
  poster's profile at the top and the sideways Hostinger logo.

### Duplicate-posts scene (poster frame)
- Use the real example from the app (front page → Search → Map) where
  the same post appears as about 15 stacked cards: the same text posted
  by different accounts ("Ken", "Sarah", random accounts) and by the same
  person at different times.
- Motion: the cards fly out quickly to the side, one by one, then
  highlight that every card has the exact same text and that the app
  grouped them together.

### By timestamp (current cut)
- 0:45: the line ends with "...and it leaves a paper trail."
  Don't make that text green.
- "Reads the live Bluesky" → "Reads Bluesky".
- 0:55, top right: the cluster phrase is hard to read because the pale
  x-axis covers it. Move the phrase or the axis so it's clearly readable.
- 1:07: remove the red line crossing out "decides the verdict". Replace
  the two cards ("writes the explanation" / "decides the verdict") with a
  VERY simple system diagram that circles which model does what.
  Default: Gemini writes the explanation, Nemotron decides the verdict.
  Check the code to confirm and log what you found.
- 1:22: remove "live web search". Keep only "Oxylabs Web API".
- 1:23: change the green word "receipt" to "link"
  (→ "link: en.wikipedia.org").

### Last screen
- ONLY the QR code and "Search it yourself". The QR may animate gently.
- Remove "Live on Hostinger" and the green URL text (nobody types a
  URL; people only scan the QR).
- Bottom credits line, ONLY: `Junction 2026 · Hostinger · [QR] · The Noise`.
  Remove the music credit (Sascha Ende, "Happy Beats", "Business Moves",
  CC BY 4.0) and the footage/date note.

## 3. Open points: use these defaults and log them
- Length: an early note said 30 seconds, but the current cut is about
  2 minutes. Keep the current structure and tighten every scene. Don't
  cut whole scenes. Log the final length.
- App name in the credits: assumed "The Noise". Change it if the repo
  uses a different product name.
- If any instruction above conflicts with what the code really does,
  follow the code's truth and log it.

## 4. Finish (required)
1. Render the final video to `./out/demo-final.mp4` (keep the old one).
2. Export still frames at 0:02, 0:45, 0:55, 1:07, 1:22, 1:23 and the
   last screen to `./out/frames/`, and check each one yourself:
   - no "trace", "receipt", "live" or red dot anywhere
   - text is readable and not covered
   - colors match the app
3. Grep all on-screen text in the project for banned words
   (trace, receipt, live, beta, v1/v2) and fix any hits.
4. Write `HANDOFF-NOTES.md`: what you changed, every assumption you made,
   anything you couldn't do and why, and how to re-render.
5. Commit everything to `demo-video-fixes-overnight`.
