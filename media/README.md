# Media analysis

`media.nemotron.NemotronClient` calls Nemotron 3.5 Super VL through NVIDIA's OpenAI-compatible
API. Pass either a public image URL or downloaded bytes in `MediaInput`; video is represented by
one or more extracted frames. Successful results are cached in memory by `media_hash` and expose
`evidence_detail()` for the shared `evidence.detail` JSON field.

```python
from media import MediaInput, NemotronClient

client = NemotronClient(api_key=settings.nvidia_api_key, base_url=settings.nvidia_base_url)
analysis = client.analyze(MediaInput("sha256:...", "image/jpeg", url="https://..."))
```

The client retries rate-limit and server failures with bounded exponential backoff. It does not
make claims about political intent; the returned description, OCR, reuse cues, and manipulation
cues are evidence for the signals and analyst components to interpret.