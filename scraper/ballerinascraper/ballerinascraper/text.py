import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

# print(f"BASE_DIR: {BASE_DIR}")
# print(f'PROJECT_ROOT: {PROJECT_ROOT}')
#
# path = os.path.join(PROJECT_ROOT, 'proxy','proxy_list.txt')
# print(path)
#
# if 'ballerinascraper/proxy/proxy_list.txt' in path:
#     print(True)
# else:
#     print(False)
# ballerinascraper/proxy/proxy_list.txt


import trafilatura
from ballerinascraper.code import text


# def extract_clean_text(html: str) -> str | None:
#     """Pulls main article content, drops nav/footer/scripts/ads automatically."""
#     return trafilatura.extract(
#         html,
#         include_comments=False,
#         include_tables=True,
#         favor_recall=True,   # better for technical docs than favor_precision
#     )
#
# text  = extract_clean_text(text)
# print(text)

# clean_text = trafilatura.extract(text)
# print(clean_text)  # just the article text, no tags, no nav/footer junk

import re, json
import html as html_module

# def clean(text: str) -> str | None:
#     TAG_RE = re.compile(r"<[^>]+>", re.UNICODE)  # matches ANY tag, attributes and all
#     text = TAG_RE.sub(" ", text)
#     print(text)
#
# clean(text)

CODE_BLOCK_RE = re.compile(r"<pre\b[^>]*>.*?</pre>", re.DOTALL | re.IGNORECASE)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


def _clean_code_block(pre_html: str) -> str:
    text = re.sub(r"<[^>]+>", "", pre_html)
    return html_module.unescape(text).strip("\n")


def extract_page(raw_html: str) -> dict:
    m = NEXT_DATA_RE.search(raw_html)
    if not m:
        return {"title": "", "description": "", "code_blocks": []}
    props = json.loads(m.group(1))["props"]["pageProps"]
    frontmatter = props.get("frontmatter", {})
    code_blocks = [_clean_code_block(c) for c in props.get("codes", [])]
    return {
        "title": frontmatter.get("title", "").strip(),
        "description": frontmatter.get("description", "").strip(),
        "code_blocks": [c for c in code_blocks if c],
    }


# with open("/tmp/doc5.html", "r") as f:
#     raw = f.read()

page = extract_page(text)
# print("TITLE:", page["title"])
# print("DESCRIPTION:", page["description"])
# print("\nCODE:")
for c in page["code_blocks"]:
    print(c)
