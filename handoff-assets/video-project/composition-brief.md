# Hyperframes Composition Brief: Shill or Chill (demo v3)

- Composition: `composition/` (index.html mounts 7 scenes + captions from `compositions/`), 1920x1080, 30 fps, 119 s.
- Storyboard and facts: `brag-plan.md`. Narration: `vo-script.json`, audio and word timings in `composition/assets/vo/`.
- Identity: #050506 ground, #f2efe9 text, CV green #3fe07a, Shill #e2533c, Chill #5fc9a8, Can't tell #9aa6bf;
  Michroma (wordmark), Inter, JetBrains Mono. Machine-vision corner boxes are the motion language.
- Real material: live-site recording of cluster #10876 (Playwright, 14 Nov 2026), desktop image trace, iPhone Safari,
  Dürer woodcut, QR, verdict coins. Post text and handles come from the public /api/clusters endpoints.
- Audio: one pre-mixed master `assets/mix.wav` (voice + ducked music + SFX, -14 LUFS).
- Wording guard: only a cluster's verdict is "Shill"; accounts are described by behaviour.
- Gate: `npx hyperframes check` passes with zero errors.
