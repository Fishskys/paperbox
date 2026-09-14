"""Build evals/arxiv_ids.txt from the arXiv API (three coherent topics)."""
import json
import re
import urllib.parse
import urllib.request

TOPICS = [
    ("low-power-sram", "all:SRAM AND all:circuit"),
    ("finfet-circuits", "all:FinFET"),
    ("data-converters", "all:\"time-to-digital converter\" OR all:\"time-interleaved ADC\""),
    ("efficient-attention", "all:\"efficient transformer attention\" OR all:\"attention is all you need\""),
    ("rag-retrieval", "all:\"retrieval augmented generation\" OR all:\"dense passage retrieval\""),
]
PER_TOPIC = 12

rows = []
for tag, query in TOPICS:
    url = (
        "https://export.arxiv.org/api/query?"
        + urllib.parse.urlencode(
            {"search_query": query, "start": 0, "max_results": PER_TOPIC, "sortBy": "relevance"}
        )
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        xml = response.read().decode("utf-8", "replace")
    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
    for entry in entries:
        ident = re.search(r"<id>http://arxiv.org/abs/([^<]+)</id>", entry)
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        if not ident:
            continue
        aid = ident.group(1).strip()
        base = aid.split("v")[0]
        clean_title = " ".join((title.group(1) if title else "").split())
        rows.append((tag, base, clean_title))

seen, out = set(), []
for tag, base, title in rows:
    if base in seen:
        continue
    seen.add(base)
    out.append(f"{base}\t{tag}\t{title[:90]}")

with open("evals/arxiv_ids.txt", "w", encoding="utf-8") as handle:
    handle.write("# arXiv id\t主题\t标题（由 scripts/build_arxiv_ids.py 生成，2026-09-12）\n")
    handle.write("# 实际导入使用第一列；脚本 scripts/bulk_ingest.py 按行读取，忽略 # 与空行\n")
    for line in out:
        handle.write(line + "\n")

print(json.dumps({"total": len(out), "by_topic": {t: sum(1 for r in out if f"\t{t}\t" in r) for t, _ in TOPICS}}, ensure_ascii=False))
