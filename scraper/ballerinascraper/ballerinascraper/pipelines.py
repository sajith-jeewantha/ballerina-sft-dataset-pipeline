# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
from itemadapter import ItemAdapter

import hashlib, json, os
import scrapy

from dataclasses import asdict

import datetime
from datetime import timezone

from scrapy.exceptions import DropItem

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from google.cloud import storage
except ImportError:
    storage = None


class BallerinascraperPipeline:
    def process_item(self, item, spider):
        return item


# class LocalFilePipeline:
#     @classmethod
#     def from_crawler(cls, crawler):
#         pipeline = cls()
#         pipeline.crawler = crawler
#         return pipeline
#
#     def open_spider(self):
#         os.makedirs("output", exist_ok=True)
#         self.seen_hashes = set()
#         self.out_file = open("output/ballerina_corpus_old.jsonl", "a", encoding="utf-8")
#
#     def process_item(self, item):
#         # content_hash = hashlib.sha256(
#         #     item["content"].encode("utf-8", errors="ignore")
#         # ).hexdigest()
#         #
#         # if content_hash in self.seen_hashes:
#         #     raise scrapy.exceptions.DropItem("duplicate content")
#         # self.seen_hashes.add(content_hash)
#         # item["content_hash"] = content_hash
#         #
#         # self.out_file.write(json.dumps(dict(item), ensure_ascii=False) + "\n")
#         # self.crawler.spider.logger.info(f"Saved: {item['url']} ({item['file_type']})")
#         # return item
#
#         content_hash = hashlib.sha256(item.content.encode("utf-8", errors="ignore")).hexdigest()
#         if content_hash in self.seen_hashes:
#             raise scrapy.exceptions.DropItem("duplicate content")
#         self.seen_hashes.add(content_hash)
#         item.content_hash = content_hash
#
#         self.out_file.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
#         self.crawler.spider.logger.info(f"Saved: {item.url} ({item.file_type})")
#         return item
#
#     def close_spider(self):
#         self.out_file.close()
#         self.crawler.spider.logger.info(
#             f"Total unique items saved: {len(self.seen_hashes)}"
#         )

# class GCSPipeline:
#     def open_spider(self, spider):
#         self.client = storage.Client()
#         self.bucket = self.client.bucket("your-bucket-name")
#         self.seen_hashes = set()
#         self.buffer = []
#         self.buffer_size = 100  # batch writes instead of one API call per item
#
#     def process_item(self, item, spider):
#         content_hash = hashlib.sha256(item["content"].encode("utf-8", errors="ignore")).hexdigest()
#         if content_hash in self.seen_hashes:
#             raise scrapy.exceptions.DropItem("duplicate")
#         self.seen_hashes.add(content_hash)
#         item["content_hash"] = content_hash
#         self.buffer.append(json.dumps(dict(item), ensure_ascii=False))
#
#         if len(self.buffer) >= self.buffer_size:
#             self._flush(spider)
#         return item
#
#     def _flush(self, spider):
#         if not self.buffer:
#             return
#         blob_name = f"raw/{spider.name}-{spider.crawler.stats.get_value('start_time').timestamp()}.jsonl"
#         blob = self.bucket.blob(blob_name)
#         blob.upload_from_string("\n".join(self.buffer) + "\n", content_type="application/json")
#         self.buffer = []
#
#     def close_spider(self, spider):
#         self._flush(spider)


class ExtractionPipeline:
    MIN_HTML_LEN = 200

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    def process_item(self, item):
        file_type = item.get("file_type")

        if file_type == "html":
            if trafilatura is None:
                self.crawler.spider.logger.warning(
                    "trafilatura not installed — storing raw HTML for %s", item.get("url")
                )
                return item

            raw = item.get("content", "")
            if len(raw) < self.MIN_HTML_LEN:
                raise DropItem(f"HTML too short to extract: {item.get('url')}")

            extracted = trafilatura.extract(
                raw,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
            if not extracted:
                raise DropItem(f"No extractable content: {item.get('url')}")

            item["content"] = extracted
            item["extraction_method"] = "trafilatura"
        else:
            item["extraction_method"] = "raw"

        return item


class QualityFilterPipeline:
    MIN_CONTENT_LEN = {
        "html": 300,
        "md": 100,
        "bal": 20,
        "bal_embedded": 20,
    }

    def process_item(self, item):
        content = item.get("content", "") or ""
        content = content.strip()

        if not content:
            raise DropItem(f"Empty content: {item.get('url')}")

        file_type = item.get("file_type", "html")
        min_len = self.MIN_CONTENT_LEN.get(file_type, 200)
        if len(content) < min_len:
            raise DropItem(
                f"Content too short ({len(content)} < {min_len}) for {item.get('url')}"
            )

        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if lines:
            unique_ratio = len(set(lines)) / len(lines)
            if unique_ratio < 0.3 and len(lines) > 10:
                raise DropItem(f"Looks like boilerplate/repetitive content: {item.get('url')}")

        item["content"] = content
        return item


class DedupPipeline:
    def open_spider(self):
        self.seen_hashes = set()

    def process_item(self, item):
        content = item.get("content", "")
        content_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

        if content_hash in self.seen_hashes:
            raise DropItem(f"Duplicate content: {item.get('url')}")

        self.seen_hashes.add(content_hash)
        item["content_hash"] = content_hash
        return item

from pathlib import Path

class LocalFilePipeline:
    #Todo: change this to 50
    BATCH_SIZE = 1 #was 50
    # OUTPUT_DIR = "output"
    BASE_DIR = Path(__file__).resolve().parents[3]
    OUTPUT_DIR = BASE_DIR / "data" / "scraper"

    def __init__(self):
        self.buffer = []
        self.total_saved = 0

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    def open_spider(self, spider):
        self.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.output_path = self.OUTPUT_DIR / "ballerina_corpus.jsonl"
        self.out_file = open(self.output_path, "a", encoding="utf-8")
        spider.logger.info(f"Writing local output to: {self.output_path}")

    def process_item(self, item):
        item["fetched_at"] = item.get(
            "fetched_at", datetime.datetime.now(timezone.utc).isoformat()
        )
        self.buffer.append(json.dumps(dict(item), ensure_ascii=False))
        self.total_saved += 1

        if len(self.buffer) >= self.BATCH_SIZE:
            self._flush()

        return item

    def _flush(self):
        if not self.buffer:
            return
        self.out_file.write("\n".join(self.buffer) + "\n")
        self.out_file.flush()
        self.crawler.spider.logger.info(f"Wrote batch of {len(self.buffer)} items")
        self.buffer = []

    def close_spider(self):
        self._flush()
        self.out_file.close()
        self.crawler.spider.logger.info(
            f"Total items saved this run: {self.total_saved} -> {self.output_path}"
        )


class GCSPipeline:
    BATCH_SIZE = 100

    def __init__(self):
        self.bucket_name = os.environ.get("GCS_BUCKET_NAME", "your-bucket-name")
        self.buffer = []
        self.total_saved = 0

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    def open_spider(self):
        spider = self.crawler.spider
        if storage is None:
            spider.logger.error(
                "google-cloud-storage not installed — GCSPipeline cannot write. "
                "Falling back to local output/ballerina_corpus.jsonl"
            )
            self.client = None
            os.makedirs("output", exist_ok=True)
            self.local_file = open("output/ballerina_corpus.jsonl", "a", encoding="utf-8")
        else:
            self.client = storage.Client()
            self.bucket = self.client.bucket(self.bucket_name)
            self.local_file = None

        self.run_id = datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def process_item(self, item):
        item["fetched_at"] = item.get(
            "fetched_at", datetime.datetime.now(timezone.utc).isoformat()
        )
        self.buffer.append(json.dumps(dict(item), ensure_ascii=False))
        self.total_saved += 1

        if len(self.buffer) >= self.BATCH_SIZE:
            self._flush()

        return item

    def _flush(self):
        if not self.buffer:
            return
        spider = self.crawler.spider
        payload = "\n".join(self.buffer) + "\n"

        if self.client is not None:
            blob_name = f"raw/{spider.name}/{self.run_id}/batch-{self.total_saved}.jsonl"
            blob = self.bucket.blob(blob_name)
            blob.upload_from_string(payload, content_type="application/json")
            spider.logger.info(f"Uploaded batch of {len(self.buffer)} items to gs://{self.bucket_name}/{blob_name}")
        else:
            self.local_file.write(payload)
            spider.logger.info(f"Wrote batch of {len(self.buffer)} items locally")

        self.buffer = []

    def close_spider(self):
        self._flush()
        if self.local_file:
            self.local_file.close()
        self.crawler.spider.logger.info(f"Total items saved this run: {self.total_saved}")