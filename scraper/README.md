
`GITHUB_TOKEN` isn't strictly required, but without it GitHub's API rate
limits you heavily (10 req/min for code search vs. much higher with a
token). `GCS_BUCKET_NAME` is only needed once you switch to `GCSPipeline`.

## Generate seed URLs

```bash
python scripts/generate_seeds.py   # Common Crawl — known domains
python scripts/github_seeds.py     # GitHub org repos + .bal search

wc -l seeds/common_crawl_urls.txt  # sanity check — should be > 0
```

Re-run these periodically to refresh seeds; both scripts merge with
whatever's already in `ballerinascraper/seeds/common_crawl_urls.txt` rather than
overwriting it.

## Run the crawl

**Local testing** (default — writes to `ballerinascraper/output`):
```bash
scrapy crawl ballerinascraper -L INFO
```


**Switch to GCS** — in `settings.py`, swap the last pipeline stage:
```python
ITEM_PIPELINES = {
    "ballerinascraper.pipelines.ExtractionPipeline": 100,
    "ballerinascraper.pipelines.QualityFilterPipeline": 200,
    "ballerinascraper.pipelines.DedupPipeline": 300,
    # "ballerinascraper.pipelines.GCSPipeline": 400,   # was LocalFilePipeline
    "ballerinascraper.pipelines.LocalFilePipeline": 400,

}
```