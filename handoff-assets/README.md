# Handoff assets for the demo video

- `current-demo.mp4`: the current rendered demo video (1:59, 1920x1080, -14 LUFS), `current-demo-poster.jpg` its poster.
- `video-project/composition/`: the HyperFrames project that renders it (`index.html` mounts the scenes in `compositions/`;
  footage, voice, music, SFX, fonts and images are in `assets/`). Render: `cd handoff-assets/video-project/composition && npx hyperframes check && npx hyperframes render --output ../../../out/demo-final.mp4`.
- `video-project/composition/assets/mix.wav`: the pre-mixed audio (voice + music + SFX). It is built outside HyperFrames with ffmpeg from `assets/vo/*.t.wav`, `assets/music/` and `assets/sfx/`; to change music or levels, rebuild this file.
- `video-project/composition/assets/vo/*.words.json`: word timings used for the captions and for timing every reveal.
- `video-project/PROMPT-v4-detailed.md`, `brag-plan.md`, `vo-script.json`: the previous brief, plan and narration script (all facts used are listed there).
- `video-project/composition/assets/img/meme.png`: the meme currently used in the trace scene (not the Hostinger meme).

## Missing (could not be found on Kirill's machine)
- The Hostinger meme screenshot (with the years 2024 / 2025 / 2026).
- The stacked duplicate-cards screenshots (app front page -> Search -> Map). Capture them from the live app.

The ElevenLabs API key used for the narration is not included. New narration needs a key from Kirill.
