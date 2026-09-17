"""JSON 文件存储与检索：records / state / summary / cookie 的读写。

沿用 1.py 的目录约定，所有产物仍写在 cnki_2010_2019/ 下，便于和原 CLI 互通。
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # 知网/
WORK_DIR = SCRIPT_DIR / "cnki_2010_2019"
COOKIE_FILE = SCRIPT_DIR / "cnki_cookies.json"
NDJSON_PATH = WORK_DIR / "cnki_records.ndjson"
STATE_PATH = WORK_DIR / "cnki_scrape_state.json"
SUMMARY_PATH = WORK_DIR / "cnki_scrape_summary.json"
JSON_PATH = WORK_DIR / "cnki_records.json"
PDF_DIR = WORK_DIR / "pdfs"

DEFAULT_JOURNALS = [
    "中国工业经济", "管理世界", "经济研究", "数量经济技术经济研究", "中国农村经济",
    "南开管理评论", "中国社会科学", "经济管理", "金融研究", "公共管理学报",
    "产业经济研究", "中国软科学", "财贸经济", "人口研究", "经济学（季刊）",
    "国际经济评论", "中国人口·资源与环境", "科学学研究", "经济学家",
    "中国人口科学", "社会学研究", "经济学动态", "经济地理",
]

_file_lock = threading.Lock()


def ensure_dirs() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)


def load_all_records() -> list[dict[str, str]]:
    """读取所有已抓取记录（优先 NDJSON，缺失则回退到 JSON）。"""
    if NDJSON_PATH.exists():
        records: list[dict[str, str]] = []
        with NDJSON_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records
    if JSON_PATH.exists():
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    return []


def save_json_records(records: list[dict[str, str]]) -> None:
    """全量写入 cnki_records.json（用于前端浏览/统计）。"""
    with _file_lock:
        JSON_PATH.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def append_ndjson(records: Iterable[dict[str, str]]) -> None:
    with _file_lock:
        with NDJSON_PATH.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"completed_pages": {}, "journal_counts": {}, "records_written": 0}


def save_state(state: dict[str, Any]) -> None:
    with _file_lock:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_summary() -> dict[str, Any]:
    if SUMMARY_PATH.exists():
        try:
            return json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def reset_progress() -> None:
    """清空抓取产物（保留 PDF 与 xlsx）。"""
    for path in (NDJSON_PATH, STATE_PATH, SUMMARY_PATH, JSON_PATH):
        if path.exists():
            path.unlink()


def search_records(
    records: list[dict[str, str]],
    *,
    keyword: str | None = None,
    journal: str | None = None,
    author: str | None = None,
    year: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, str]], int]:
    """对内存中的记录做关键词过滤+分页。返回 (当前页数据, 总命中)。"""
    keyword = (keyword or "").strip()
    journal = (journal or "").strip()
    author = (author or "").strip()
    year = (year or "").strip()

    filtered: list[dict[str, str]] = []
    for i, rec in enumerate(records):
        if journal and journal != rec.get("检索期刊") and journal != rec.get("刊名"):
            continue
        if year and not (rec.get("发表时间", "") or "").startswith(year):
            continue
        if author and author not in (rec.get("作者", "") or ""):
            continue
        if keyword:
            hay = " ".join(
                str(rec.get(k, ""))
                for k in ("篇名", "作者", "刊名", "摘要", "关键词")
            ).lower()
            if keyword.lower() not in hay:
                continue
        r = dict(rec)
        r["_idx"] = i  # 在全量记录中的位置，供按条下载（/api/pdf/start indices）使用
        filtered.append(r)

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    return filtered[start:end], total


def list_journals(records: list[dict[str, str]]) -> list[str]:
    seen: list[str] = []
    for rec in records:
        j = rec.get("检索期刊") or rec.get("刊名") or ""
        if j and j not in seen:
            seen.append(j)
    return seen


def cookie_status() -> dict[str, Any]:
    if not COOKIE_FILE.exists():
        return {"exists": False, "count": 0, "domains": []}
    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"exists": True, "count": 0, "domains": [], "invalid": True}
    domains = sorted({c.get("domain", "") for c in cookies if isinstance(c, dict)})
    return {"exists": True, "count": len(cookies), "domains": domains}


def save_cookie_upload(text: str) -> int:
    """校验并保存上传的 Cookie JSON 文本，返回条目数。"""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Cookie 文件应为 JSON 数组（浏览器导出格式）")
    COOKIE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(data)


def list_pdfs() -> list[dict[str, Any]]:
    if not PDF_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    paths: list[Path] = list(PDF_DIR.glob("*.caj")) + list(PDF_DIR.glob("*.pdf"))
    for p in sorted(paths, key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "path": str(p),
        })
    return items


def stats_aggregate(records: list[dict[str, str]]) -> dict[str, Any]:
    """聚合统计：按期刊/年份/作者/关键词的计数与趋势。"""
    by_journal: dict[str, int] = defaultdict(int)
    by_year: dict[str, int] = defaultdict(int)
    by_author: dict[str, int] = defaultdict(int)
    by_keyword: dict[str, int] = defaultdict(int)

    for rec in records:
        j = rec.get("检索期刊") or rec.get("刊名") or ""
        if j:
            by_journal[j] += 1
        year = (rec.get("发表时间", "") or "")[:4]
        if year:
            by_year[year] += 1
        for a in str(rec.get("作者", "")).split(";"):
            a = a.strip()
            if a:
                by_author[a] += 1
        for k in str(rec.get("关键词", "")).split(";"):
            k = k.strip()
            if k:
                by_keyword[k] += 1

    def top(d: dict[str, int], n: int = 20) -> list[dict[str, Any]]:
        return [{"name": k, "count": v} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]

    return {
        "total": len(records),
        "by_journal": top(by_journal, 50),
        "by_year": sorted(
            [{"year": y, "count": c} for y, c in by_year.items()],
            key=lambda x: x["year"],
        ),
        "by_author": top(by_author, 30),
        "by_keyword": top(by_keyword, 50),
    }
