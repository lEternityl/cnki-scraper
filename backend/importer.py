"""知网官网导出题录的解析与导入。

支持三种知网导出格式：
- EndNote（%T 篇名、%A 作者、%J 刊名、%D 发表时间、%K 关键词、%X 摘要、%U 链接）
- Refworks（T1/A1/JO/JF/DA/YR/K1/AB/UL 标签）
- 自定义（中文字段名"篇名:xxx"逐行，字段间空行分隔）

导入的记录统一为系统 schema，按 (篇名, 刊名) 去重后合并到本地 NDJSON。
"""
from __future__ import annotations

import re
from typing import Any

from . import storage
from .scraper import compact_text, normalize_people, normalize_terms, safe_key


def _clean(value: str) -> str:
    return compact_text(value.strip().lstrip(";,，；"))


def _parse_endnote(text: str) -> list[dict[str, str]]:
    """EndNote 格式：%标签 值，同标签多行合并。"""
    field_map = {
        "T": "篇名", "A": "作者", "J": "刊名", "D": "发表时间",
        "K": "关键词", "X": "摘要", "U": "链接", "URL": "链接",
    }
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    last_key: str | None = None

    for line in text.splitlines():
        m = re.match(r"^%(\w+)\s*(.*)$", line.strip())
        if m:
            tag, value = m.group(1), m.group(2)
            if tag == "0":  # %0 Journal Article = 新记录开始
                if current and current.get("篇名"):
                    records.append(current)
                current = {}
                last_key = None
                continue
            key = field_map.get(tag.upper())
            if key and current is not None:
                current.setdefault(key, []).append(value.strip())
                last_key = key
                continue
            last_key = None
        elif current is not None and last_key and line.strip():
            current[last_key].append(line.strip())  # 摘要等长文本折行
    if current and current.get("篇名"):
        records.append(current)

    out: list[dict[str, str]] = []
    for rec in records:
        out.append({
            "篇名": _clean(" ".join(rec.get("篇名", []))),
            "作者": normalize_people(_clean("; ".join(rec.get("作者", [])))),
            "刊名": _clean(" ".join(rec.get("刊名", []))),
            "发表时间": _clean(" ".join(rec.get("发表时间", []))),
            "关键词": normalize_terms(_clean("; ".join(rec.get("关键词", [])))),
            "摘要": _clean(" ".join(rec.get("摘要", []))),
            "链接": _clean(" ".join(rec.get("链接", []))),
        })
    return [r for r in out if r["篇名"]]


def _parse_refworks(text: str) -> list[dict[str, str]]:
    """Refworks 格式：RT 开头新记录，ER 结束，两字母标签。"""
    field_map = {
        "T1": "篇名", "A1": "作者", "JO": "刊名", "JF": "刊名",
        "DA": "发表时间", "YR": "发表时间", "FD": "期",
        "K1": "关键词", "AB": "摘要", "N1": "摘要", "UL": "链接",
    }
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None

    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s*(.*)$", line.strip())
        if not m:
            continue
        tag, value = m.group(1), m.group(2)
        if tag == "RT":
            if current and current.get("篇名"):
                records.append(current)
            current = {}
            continue
        if tag == "ER":
            if current and current.get("篇名"):
                records.append(current)
            current = None
            continue
        key = field_map.get(tag)
        if key and current is not None and value:
            current.setdefault(key, []).append(value.strip())

    if current and current.get("篇名"):
        records.append(current)

    out = []
    for rec in records:
        out.append({
            "篇名": _clean(" ".join(rec.get("篇名", []))),
            "作者": normalize_people(_clean("; ".join(rec.get("作者", [])))),
            "刊名": _clean(" ".join(rec.get("刊名", []))),
            "发表时间": _clean(" ".join(rec.get("发表时间", []))),
            "关键词": normalize_terms(_clean("; ".join(rec.get("关键词", [])))),
            "摘要": _clean(" ".join(rec.get("摘要", []))),
            "链接": _clean(" ".join(rec.get("链接", []))),
        })
    return [r for r in out if r["篇名"]]


def _parse_custom(text: str) -> list[dict[str, str]]:
    """自定义格式：中文"字段名:值"逐行，空行分隔记录。"""
    out: list[dict[str, str]] = []
    current: dict[str, list[str]] = {}
    last_key: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current.get("篇名"):
                out.append(current)
            current = {}
            last_key = None
            continue
        m = re.match(r"^([^:：]{1,12})[:：]\s*(.*)$", stripped)
        if m:
            key_raw, value = m.group(1).strip(), m.group(2).strip()
            if key_raw in ("篇名", "题名", "标题"):
                if current.get("篇名"):
                    out.append(current)
                current = {}
                last_key = "篇名"
                current["篇名"] = [value]
                continue
            alias = {
                "作者": "作者", "刊名": "刊名", "来源": "刊名", "文献来源": "刊名",
                "发表时间": "发表时间", "时间": "发表时间", "日期": "发表时间",
                "关键词": "关键词", "摘要": "摘要", "链接": "链接", "网址": "链接",
                "URL": "链接",
            }
            key = alias.get(key_raw)
            if key:
                current.setdefault(key, []).append(value)
                last_key = key
                continue
            last_key = None
        elif last_key and current.get(last_key):
            current[last_key].append(stripped)  # 摘要折行

    if current.get("篇名"):
        out.append(current)

    result: list[dict[str, str]] = []
    for rec in out:
        result.append({
            "篇名": _clean(" ".join(rec.get("篇名", []))),
            "作者": normalize_people(_clean("; ".join(rec.get("作者", [])))),
            "刊名": _clean(" ".join(rec.get("刊名", []))),
            "发表时间": _clean(" ".join(rec.get("发表时间", []))),
            "关键词": normalize_terms(_clean("; ".join(rec.get("关键词", [])))),
            "摘要": _clean(" ".join(rec.get("摘要", []))),
            "链接": _clean(" ".join(rec.get("链接", []))),
        })
    return [r for r in result if r["篇名"]]


def parse_records(text: str) -> list[dict[str, str]]:
    """自动识别格式并解析，返回统一 schema 的记录列表。"""
    if re.search(r"^%\w+\s", text, re.M):
        return _parse_endnote(text)
    if re.search(r"^RT\s", text, re.M):
        return _parse_refworks(text)
    if re.search(r"^(篇名|题名|标题)[:：]", text, re.M):
        return _parse_custom(text)
    return []


def import_records(text: str, source: str = "官网导入") -> dict[str, Any]:
    """解析 → 去重 → 合并到本地记录。返回统计信息。"""
    parsed = parse_records(text)
    existing = storage.load_all_records()
    seen: set[tuple[str, str]] = set()
    for r in existing:
        seen.add((safe_key(r.get("篇名", "")), safe_key(r.get("刊名", ""))))

    fresh: list[dict[str, str]] = []
    for p in parsed:
        key = (safe_key(p["篇名"]), safe_key(p["刊名"]))
        if key in seen:
            continue
        seen.add(key)
        fresh.append({
            "检索期刊": source,
            "篇名": p["篇名"],
            "作者": p.get("作者", ""),
            "刊名": p.get("刊名", ""),
            "发表时间": p.get("发表时间", ""),
            "链接": p.get("链接", ""),
            "下载链接": "",
            "摘要": p.get("摘要", ""),
            "关键词": p.get("关键词", ""),
        })

    if fresh:
        storage.append_ndjson(fresh)
        storage.save_json_records(storage.load_all_records())
    return {
        "total_in_file": len(parsed),
        "imported": len(fresh),
        "skipped_dup": len(parsed) - len(fresh),
        "records_total": len(existing) + len(fresh),
    }
