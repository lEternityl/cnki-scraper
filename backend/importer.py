"""知网官网导出题录的解析与导入。

支持三种知网导出格式：
- EndNote（%T 篇名、%A 作者、%J 刊名、%D 发表时间、%K 关键词、%X 摘要、%U 链接）
- Refworks（T1/A1/JO/JF/DA/YR/K1/AB/UL 标签）
- 自定义（中文字段名"篇名:xxx"逐行，字段间空行分隔）

导入的记录统一为系统 schema，按 (篇名, 刊名) 去重后合并到本地 NDJSON。
"""
from __future__ import annotations

import html
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
        "8": "发表时间",  # 报纸类记录的日期字段（%D 缺失时使用）
        "K": "关键词", "X": "摘要", "U": "链接",
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
        # %D（年份）与 %8（日期）都映射到发表时间，取第一个非空值
        pub_parts = [p for p in rec.get("发表时间", []) if p.strip()]
        out.append({
            "篇名": _clean(" ".join(rec.get("篇名", []))),
            "作者": normalize_people(_clean("; ".join(rec.get("作者", [])))),
            "刊名": _clean(" ".join(rec.get("刊名", []))),
            "发表时间": _clean(pub_parts[0]) if pub_parts else "",
            "关键词": normalize_terms(_clean("; ".join(rec.get("关键词", [])))),
            "摘要": _clean(" ".join(rec.get("摘要", []))),
            "链接": html.unescape(_clean(" ".join(rec.get("链接", [])))),
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


def _parse_gbt7714(text: str) -> list[dict[str, str]]:
    """GB/T 7714 格式（知网「导出参考文献」格式），每行一条：
    [1]作者. 篇名[J].刊名,年,(期):页码.  支持 [J]/[N]/[D]/[C]/[J/OL]/[EB/OL] 等类型。
    """
    out: list[dict[str, str]] = []
    line_re = re.compile(r"^\[(\d+)\]\s*(.+)$")
    for line in text.splitlines():
        stripped = line.strip()
        m = line_re.match(stripped)
        if not m:
            continue
        body = m.group(2)

        # 拆作者：第一个 "." 之前且不含 "[" 的段为作者；若第一个 "[" 前没有 "."，则视为无作者
        dot_pos = body.find(".")
        bracket_pos = body.find("[")
        if dot_pos != -1 and (bracket_pos == -1 or dot_pos < bracket_pos):
            authors_raw = body[:dot_pos].strip()
            rest = body[dot_pos + 1:].lstrip()
        else:
            authors_raw = ""
            rest = body

        # 拆篇名与文献类型：篇名[类型].后续
        m2 = re.match(r"^(.*?)\s*\[([A-Z]+(?:/[A-Z]+)?)\]\s*\.?\s*(.*)$", rest)
        if not m2:
            continue
        title, doc_type, tail = m2.group(1).strip(), m2.group(2), m2.group(3)

        # 后续部分：刊名,年,(期):页码 / 报纸,日期(版次) / 大学,年 / //会议.论文集.年:页码
        journal = ""
        pub = ""
        link = ""
        tail_wo_link = re.sub(r"https?://\S+", "", tail).strip()
        m_link = re.search(r"(https?://\S+?)[\s.]*$", tail)
        if m_link:
            link = m_link.group(1).rstrip(".")

        if tail_wo_link.startswith("//"):  # 会议论文
            seg = tail_wo_link[2:].split(".")
            journal = seg[1].strip() if len(seg) > 1 else seg[0].strip()
        else:
            # 刊名 = 首个逗号前（或全段）；发表时间取首个年/日期
            journal = tail_wo_link.split(",")[0].strip().rstrip(".")
        m_date = re.search(r"(\d{4}(?:-\d{2}-\d{2})?)", tail_wo_link)
        if m_date:
            pub = m_date.group(1)

        # 作者规范化："张三,李四,等" → "张三; 李四; 等"
        authors = ""
        if authors_raw:
            parts = [p.strip() for p in authors_raw.split(",") if p.strip()]
            authors = normalize_people("; ".join(parts))

        out.append({
            "篇名": _clean(title),
            "作者": authors,
            "刊名": _clean(journal),
            "发表时间": _clean(pub),
            "关键词": "",
            "摘要": "",
            "链接": html.unescape(link),
            "文献类型": doc_type,
        })
    return [r for r in out if r["篇名"]]


def parse_records(text: str) -> list[dict[str, str]]:
    """自动识别格式并解析，返回统一 schema 的记录列表。"""
    if re.search(r"^%\w+\s", text, re.M):
        return _parse_endnote(text)
    if re.search(r"^RT\s", text, re.M):
        return _parse_refworks(text)
    if re.search(r"^\[\d+\]", text, re.M):
        return _parse_gbt7714(text)
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
            "文献类型": p.get("文献类型", ""),
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


def export_gbt7714(records: list[dict[str, Any]]) -> str:
    """将本地记录导出为 GB/T 7714 格式引文文本。"""
    lines: list[str] = []
    for i, r in enumerate(records, 1):
        doc_type = (r.get("文献类型") or "J").split("/")[0] or "J"
        pub = (r.get("发表时间") or "").strip()
        if doc_type == "N" and pub:  # 报纸保留完整日期
            pub_part = pub
        else:  # 其余取年份
            m_year = re.match(r"(\d{4})", pub)
            pub_part = m_year.group(1) if m_year else pub
        authors = (r.get("作者") or "").strip()
        authors_part = authors.replace("; ", ",").replace(";", ",") if authors else ""
        pieces = []
        if authors_part:
            pieces.append(f"{authors_part}.")
        pieces.append(f"{r.get('篇名') or ''}".strip() + f"[{doc_type}].")
        journal = (r.get("刊名") or "").strip()
        if journal:
            pieces.append(f"{journal},{pub_part}." if pub_part else f"{journal}.")
        elif pub_part:
            pieces.append(f"{pub_part}.")
        link = (r.get("链接") or "").strip()
        if link:
            pieces.append(f"{link}.")
        lines.append(f"[{i}]" + " ".join(p.strip(" ") for p in pieces if p.strip(" .")))
    return "\n".join(lines) + ("\n" if lines else "")
