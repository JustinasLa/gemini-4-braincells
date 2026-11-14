Real responses captured 2026-11-14 from the prod API container through Oxylabs Web API
`/scrape` (output markdown, `sources/oxylabs.py` scrape_markdown). Query images:
- `disaster`: https://upload.wikimedia.org/wikipedia/en/1/11/Disaster_Girl.jpg (captionless)
- `imgflip`: https://i.imgflip.com/233stg.jpg (Disaster Girl with an added caption)

`tineye_*.json`: TinEye result_json (sort=crawl_date, order=asc), trimmed to 3 backlinks per
match, without the overlay/transform/domains/tags fields. `yandex_*.md`: Yandex imageview;
`yandex_sites_*.md`: the same with cbir_page=sites. `README-*.json`: request URL and latency.
