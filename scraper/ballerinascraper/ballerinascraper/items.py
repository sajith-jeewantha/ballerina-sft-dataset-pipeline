# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

from dataclasses import dataclass

import scrapy

# @dataclass
class BallerinascraperItem(scrapy.Item):
    # define the fields for your item here like:
    # name: str | None = None

    # url: str = None
    # content: str = None
    # file_type: str = None
    # content_type: str = None
    # source_domain: str = None
    # content_hash: str = None
    # fetched_at: str = None

    url = scrapy.Field()
    content = scrapy.Field()
    file_type = scrapy.Field()  # "bal", "bal_embedded", "md", "html"
    content_type = scrapy.Field()  # raw HTTP Content-Type header
    source_domain = scrapy.Field()
    content_hash = scrapy.Field()  # set by DedupPipeline
    extraction_method = scrapy.Field()  # set by ExtractionPipeline
    fetched_at = scrapy.Field()
    license = scrapy.Field()  # best-effort, populated where known

