"""从 1.py 重构而来的 CNKI CSSCI 抓取器。

与原 CLI 的差异：
- 不使用 argparse 与全局变量；参数在 Scraper.start() 注入；
- 每条进度/日志通过 log(...) 回调上抛，便于 FastAPI 通过 SSE 推送到前端；
- 在翻页与下载点之间检查 self._stopped，可被外部中断；
- 所有产物路径来自 storage 模块，保持与原 CLI 互通。
"""
from __future__ import annotations

import html
import json
import math
import random
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import storage

LogFn = Callable[[str], None]


class BlockedError(RuntimeError):
    """CNKI 触发安全验证时抛出，前端可据此提示重新上传 Cookie。"""


# 与 1.py 保持一致的检索字段
SELECT_FIELDS = "TI,AU,LY,PT,KY,AB,ABSTRACT,DI,YE,QI,"

FIELD_MAP = {
    "Title-题名": "篇名",
    "Author-作者": "作者",
    "Organ-单位": "机构",
    "Source-文献来源": "刊名",
    "PubTime-发表时间": "发表时间",
    "URL-网址": "导出链接",
    "Summary-摘要": "摘要",
    "Keyword-关键词": "关键词",
    "DOI-DOI": "DOI",
    "Year-年": "年份",
    "Period-期": "期",
}

BASE = "https://kns--cnki--net.share.sclib.cn"

# 高级检索字段元数据：CNKI 字段代码 → (中文标题, 占位提示)
# 与 kns.cnki.net/kns8s/AdvSearch 选项一致
FIELD_META: list[tuple[str, str, str]] = [
    ("SU", "主题", "篇名/关键词/摘要中含"),
    ("TI", "篇名", "文章标题"),
    ("KY", "关键词", "关键词"),
    ("AU", "作者", "作者姓名"),
    ("RT", "作者单位", "作者机构"),
    ("LY", "来源期刊", "期刊名称"),
    ("AB", "摘要", "摘要正文"),
    ("FT", "基金", "基金项目"),
    ("CLC", "中图分类号", "中图分类号"),
    ("DOI", "DOI", "DOI 编号"),
    ("RF", "参考文献", "参考文献内容"),
]

# 来源类别：代码 → 标题（与 AdvSearch 页面 .extend-tit-checklist 一致）
SOURCE_CATEGORIES: list[tuple[str, str]] = [
    ("CSI", "CSSCI"),
    ("SST", "北大核心"),
    ("CST", "SCI/EI/SSCI/AHCI"),
    ("FST", "CSCD"),
    ("NST", "CSTPCD"),
]

# 逻辑：0=AND, 1=OR, 2=NOT
LOGIC_AND = 0
LOGIC_OR = 1
LOGIC_NOT = 2


def compact_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    return value.strip()


def normalize_people(value: str) -> str:
    parts = [compact_text(p) for p in re.split(r"[;；]", value) if compact_text(p)]
    return "; ".join(parts)


def normalize_terms(value: str) -> str:
    parts = [compact_text(p) for p in re.split(r"[;；]", value) if compact_text(p)]
    return "; ".join(parts)


def safe_key(text: str) -> str:
    return re.sub(r"\s+", "", compact_text(text))


def query_json(journal: str, start_year: str, end_year: str) -> dict[str, Any]:
    """构造高级检索 JSON。年份通过参数传入，不再使用全局变量。"""
    return {
        "Platform": "",
        "Resource": "JOURNAL",
        "Classid": "YSTT4HG0",
        "Products": "CJFQ,CAPJ,ZHYX,CJTL",
        "QNode": {
            "QGroup": [
                {
                    "Key": "Subject",
                    "Title": "",
                    "Logic": 0,
                    "Items": [],
                    "ChildItems": [
                        {
                            "Key": "input[data-tipid=gradetxt-1]",
                            "Title": "期刊名称",
                            "Logic": 0,
                            "Items": [
                                {
                                    "Key": "input[data-tipid=gradetxt-1]",
                                    "Title": "期刊名称",
                                    "Logic": 0,
                                    "Field": "LY",
                                    "Operator": "DEFAULT",
                                    "Value": journal,
                                    "Value2": "",
                                }
                            ],
                            "ChildItems": [],
                        }
                    ],
                },
                {
                    "Key": "ControlGroup",
                    "Title": "",
                    "Logic": 0,
                    "Items": [],
                    "ChildItems": [
                        {
                            "Key": ".tit-startend-yearbox",
                            "Title": "",
                            "Logic": 0,
                            "Items": [
                                {
                                    "Key": ".tit-startend-yearbox",
                                    "Title": "出版年度",
                                    "Logic": 0,
                                    "Field": "YE",
                                    "Operator": 7,
                                    "Value": start_year,
                                    "Value2": end_year,
                                }
                            ],
                            "ChildItems": [],
                        },
                        {
                            "Key": ".extend-tit-checklist",
                            "Title": "",
                            "Logic": 0,
                            "Items": [
                                {
                                    "Key": 0,
                                    "Title": "CSSCI",
                                    "Logic": 1,
                                    "Field": "CSI",
                                    "Operator": "DEFAULT",
                                    "Value": "Y",
                                    "Value2": "",
                                }
                            ],
                            "ChildItems": [],
                        },
                    ],
                },
            ]
        },
        "ExScope": "1",
        "SearchType": 1,
        "Rlang": "CHINESE",
        "KuaKuCode": "",
        "Expands": {},
        "View": "changeDBCh",
        "SearchFrom": 1,
    }


def build_query_json(
    conditions: list[dict[str, Any]],
    start_year: str | None = None,
    end_year: str | None = None,
    source_categories: list[str] | None = None,
) -> dict[str, Any]:
    """通用高级检索 QueryJson 构造器。

    conditions: [{"field": "SU", "value": "数字经济", "logic": 0}]
      logic: 0=AND, 1=OR, 2=NOT（默认 AND）
    start_year/end_year: 出版年度范围；为空则不限。
    source_categories: ["CSI"] 等；为空则不限来源类别。
    """
    field_title = {code: title for code, title, _ in FIELD_META}
    src_title = {code: title for code, title in SOURCE_CATEGORIES}

    subject_groups: list[dict[str, Any]] = []
    for idx, cond in enumerate(conditions, 1):
        field = cond.get("field", "SU")
        value = (cond.get("value") or "").strip()
        if not value:
            continue
        logic = int(cond.get("logic", LOGIC_AND))
        title = field_title.get(field, field)
        tipkey = f"input[data-tipid=gradetxt-{idx}]"
        subject_groups.append({
            "Key": "Subject",
            "Title": "",
            "Logic": logic,
            "Items": [],
            "ChildItems": [
                {
                    "Key": tipkey,
                    "Title": title,
                    "Logic": 0,
                    "Items": [
                        {
                            "Key": tipkey,
                            "Title": title,
                            "Logic": 0,
                            "Field": field,
                            "Operator": "DEFAULT",
                            "Value": value,
                            "Value2": "",
                        }
                    ],
                    "ChildItems": [],
                }
            ],
        })

    control_children: list[dict[str, Any]] = []
    if start_year and end_year:
        control_children.append({
            "Key": ".tit-startend-yearbox",
            "Title": "",
            "Logic": 0,
            "Items": [
                {
                    "Key": ".tit-startend-yearbox",
                    "Title": "出版年度",
                    "Logic": 0,
                    "Field": "YE",
                    "Operator": 7,
                    "Value": start_year,
                    "Value2": end_year,
                }
            ],
            "ChildItems": [],
        })
    if source_categories:
        items = []
        for i, code in enumerate(source_categories):
            items.append({
                "Key": i,
                "Title": src_title.get(code, code),
                "Logic": 1,
                "Field": code,
                "Operator": "DEFAULT",
                "Value": "Y",
                "Value2": "",
            })
        control_children.append({
            "Key": ".extend-tit-checklist",
            "Title": "",
            "Logic": 0,
            "Items": items,
            "ChildItems": [],
        })

    qgroup = list(subject_groups)
    if control_children:
        qgroup.append({
            "Key": "ControlGroup",
            "Title": "",
            "Logic": 0,
            "Items": [],
            "ChildItems": control_children,
        })

    return {
        "Platform": "",
        "Resource": "JOURNAL",
        "Classid": "YSTT4HG0",
        "Products": "CJFQ,CAPJ,ZHYX,CJTL",
        "QNode": {"QGroup": qgroup},
        "ExScope": "1",
        "SearchType": 1,
        "Rlang": "CHINESE",
        "KuaKuCode": "",
        "Expands": {},
        "View": "changeDBCh",
        "SearchFrom": 1,
    }


def build_aside(conditions, start_year, end_year, source_categories) -> str:
    """构造 grid 请求的 aside 描述字符串（纯展示用，不影响检索逻辑）。"""
    field_title = {code: title for code, title, _ in FIELD_META}
    parts: list[str] = []
    for cond in conditions:
        f = cond.get("field", "SU")
        v = (cond.get("value") or "").strip()
        if not v:
            continue
        parts.append(f"{field_title.get(f, f)}：{v}")
    return f"（{' AND '.join(parts)}）" if parts else ""


def build_search_from(conditions, start_year, end_year, source_categories) -> str:
    search_from = "资源范围：学术期刊;  中英文扩展;  "
    if start_year and end_year:
        search_from += f"时间范围：出版年度：{start_year} 到 {end_year},更新时间：不限;  "
    if source_categories:
        src_title = {code: title for code, title in SOURCE_CATEGORIES}
        cats = "、".join(src_title.get(c, c) for c in source_categories)
        search_from += f"来源类别：{cats}; "
    return search_from


def search_grid(
    session: requests.Session,
    query: dict[str, Any],
    aside: str,
    search_from: str,
    page: int,
    page_size: int,
    turnpage: str = "",
    log: LogFn = lambda _msg: None,
) -> tuple[int, list[dict[str, str]], str]:
    """通用 grid 检索。返回 (总条数, 本页行, 下页 turnpage)。"""
    bool_search = page == 1
    data = {
        "boolSearch": "true" if bool_search else "false",
        "QueryJson": json.dumps(query, ensure_ascii=False, separators=(",", ":")),
        "pageNum": str(page),
        "pageSize": str(page_size),
        "sortField": "FFD",
        "sortType": "DESC",
        "dstyle": "listmode",
        "boolSortSearch": "false",
        "sentenceSearch": "false",
        "productStr": "",
        "aside": aside,
        "searchFrom": search_from,
        "CurPage": str(page),
        "turnpage": turnpage,
    }
    if not bool_search:
        data.pop("CurPage", None)
    response = post_with_retries(
        session, f"{BASE}/kns8s/brief/grid", data, f"search grid page {page}", log
    )
    soup = BeautifulSoup(response.text, "html.parser")
    count_el = soup.select_one("#countPageDiv em")
    if not count_el:
        count_el = soup.find(string=re.compile(r"共找到"))
    total = int(
        re.sub(r"\D", "", count_el.get_text() if hasattr(count_el, "get_text") else str(count_el)) or "0"
    )

    rows: list[dict[str, str]] = []
    for tr in soup.select("table.result-table-list tbody tr"):
        checkbox = tr.select_one("input.cbItem")
        title_el = tr.select_one("td.name a.fz14")
        if not checkbox or not title_el:
            continue
        collect = tr.select_one("a.icon-collect")
        # sclib.cn 代理的 grid 行里有 a.downloadlink，href 指向
        # bar--cnki--net.share.sclib.cn/bar/download/order?id=...，
        # 直接 GET 即可下载全文（多为 CAJ 格式），无需访问详情页
        dl_el = tr.select_one("a.downloadlink")
        download_link = ""
        if dl_el and dl_el.get("href"):
            download_link = dl_el["href"]
        rows.append({
            "cid": checkbox.get("value", ""),
            "篇名": compact_text(title_el.get_text(" ", strip=True)),
            "链接": title_el.get("href", ""),
            "下载链接": download_link,
            "发表时间": compact_text(
                tr.select_one("td.date").get_text(" ", strip=True)
                if tr.select_one("td.date") else ""
            ),
            "作者": normalize_people(
                tr.select_one("td.author").get_text(";", strip=True)
                if tr.select_one("td.author") else ""
            ),
            "刊名": compact_text(
                tr.select_one("td.source").get_text(" ", strip=True)
                if tr.select_one("td.source") else ""
            ),
            "filename": collect.get("data-filename", "") if collect else "",
            "dbname": collect.get("data-dbname", "") if collect else "",
        })
    if total > 0 and not rows:
        raise RuntimeError(f"search grid page {page}: no rows parsed")
    turn_el = soup.select_one("#hidTurnPage")
    next_turnpage = turn_el.get("value", "") if turn_el else turnpage
    return total, rows, next_turnpage


def _abs_download_link(href: str) -> str:
    """把 grid 行里 a.downloadlink 的 href 转成绝对地址。

    sclib.cn 代理下 downloadlink 多为绝对 URL（bar--cnki--net...），
    但也可能是 /bar/download/order 这类相对路径，需补全 host。
    """
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/bar/"):
        return "https://bar--cnki--net.share.sclib.cn" + href
    return urljoin(BASE, href)


def resolve_download_link(
    session: requests.Session, title: str, log: LogFn = lambda _msg: None
) -> str:
    """按篇名回查 CNKI grid，返回匹配文章的下载链接（绝对地址）。

    用于旧记录（没有 下载链接 字段）在下载时即时补全：
    用 TI=篇名 做一次 grid 检索，找到标题完全匹配的行，
    返回其 a.downloadlink 的绝对地址。找不到返回空串。
    """
    conditions = [{"field": "TI", "value": title, "logic": LOGIC_AND}]
    query = build_query_json(conditions)
    aside = build_aside(conditions, None, None, None)
    search_from = build_search_from(conditions, None, None, None)
    try:
        _total, rows, _tp = search_grid(
            session, query, aside, search_from, 1, 20, log=log
        )
    except Exception as exc:
        log(f"  [backfill] 回查 {title[:30]} 失败: {exc}")
        return ""
    if not rows:
        return ""
    title_key = safe_key(title)
    # 精确匹配
    for row in rows:
        if safe_key(row.get("篇名", "")) == title_key:
            return _abs_download_link(row.get("下载链接", ""))
    # 包含匹配（标题可能被截断或带副标题）
    for row in rows:
        rk = safe_key(row.get("篇名", ""))
        if rk and (title_key in rk or rk in title_key):
            return _abs_download_link(row.get("下载链接", ""))
    return ""


def check_blocked(response: requests.Response, context: str) -> None:
    """识别 CNKI 的安全验证与 sclib.cn 代理的登录跳转。

    sclib.cn 代理在 Cookie 失效时不会返回 401，而是把响应体置空、
    在响应头里塞一个 X-Redirect 指向 www.sclib.cn/page/.../show?vpnurl=...
    同时 final URL 会变成 login.share.sclib.cn/index.php?pre=...
    """
    text = response.text[:5000]
    final_url = response.url.lower()
    x_redirect = (response.headers.get("X-Redirect") or "").lower()
    blocked_by_url = "verify/home" in final_url
    blocked_by_page = "安全验证" in text or "<title>安全验证" in text
    login_redirect = (
        "login.share.sclib.cn" in final_url
        or "sclib.cn/page/" in final_url
        or "login.share.sclib.cn" in x_redirect
        or "sclib.cn/page/" in x_redirect
    )
    empty_body_with_redirect = (len(text) == 0 and x_redirect)
    status_blocked = response.status_code in {401, 403, 429}
    if (
        status_blocked
        or blocked_by_url
        or blocked_by_page
        or login_redirect
        or empty_body_with_redirect
    ):
        if login_redirect or empty_body_with_redirect:
            raise BlockedError(
                f"{context}: Cookie 已失效，sclib.cn 代理跳转到了登录页，"
                "请重新登录 sclib.cn 并在 Cookie 管理页上传新的 cnki_cookies.json"
            )
        # 报出具体触发条件，便于诊断 export/verify 类拦截
        triggers = []
        if status_blocked:
            triggers.append(f"HTTP {response.status_code}")
        if blocked_by_url:
            triggers.append("verify/home 重定向")
        if blocked_by_page:
            triggers.append("页面含『安全验证』")
        raise BlockedError(
            f"{context}: 被CNKI拦截（{', '.join(triggers)}），"
            "可能是反爬验证或请求过快，建议放慢速度或重新登录"
        )


def load_session() -> requests.Session:
    cookies = json.loads(storage.COOKIE_FILE.read_text(encoding="utf-8"))
    session = requests.Session()
    for cookie in cookies:
        domain = cookie.get("domain") or ""
        if "sclib.cn" not in domain:
            continue
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=domain,
            path=cookie.get("path") or "/",
        )
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,text/plain,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": f"{BASE}/kns8s/AdvSearch",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def post_with_retries(
    session: requests.Session,
    url: str,
    data: dict[str, Any],
    context: str,
    log: LogFn,
    timeout: int = 45,
    retries: int = 3,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.post(url, data=data, timeout=timeout)
            check_blocked(response, context)
            if response.status_code >= 500:
                raise RuntimeError(f"{context}: HTTP {response.status_code}")
            return response
        except BlockedError:
            raise
        except Exception as exc:
            last_error = exc
            wait = min(12.0, 1.8 * attempt + random.random() * 1.5)
            log(f"[retry {attempt}/{retries}] {context}: {exc}; sleep {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"{context}: failed after retries: {last_error}")


def fetch_grid(
    session: requests.Session,
    journal: str,
    page: int,
    page_size: int,
    start_year: str,
    end_year: str,
    turnpage: str = "",
    log: LogFn = lambda _msg: None,
) -> tuple[int, list[dict[str, str]], str]:
    bool_search = page == 1
    data = {
        "boolSearch": "true" if bool_search else "false",
        "QueryJson": json.dumps(
            query_json(journal, start_year, end_year),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "pageNum": str(page),
        "pageSize": str(page_size),
        "sortField": "FFD",
        "sortType": "DESC",
        "dstyle": "listmode",
        "boolSortSearch": "false",
        "sentenceSearch": "false",
        "productStr": "",
        "aside": f"（期刊名称：{journal}(精确)）",
        "searchFrom": (
            "资源范围：学术期刊;  中英文扩展;  "
            f"时间范围：出版年度：{start_year} 到 {end_year},更新时间：不限;  来源类别：CSSCI; "
        ),
        "CurPage": str(page),
        "turnpage": turnpage,
    }
    if not bool_search:
        data.pop("CurPage", None)
    response = post_with_retries(
        session, f"{BASE}/kns8s/brief/grid", data, f"grid {journal} page {page}", log
    )
    soup = BeautifulSoup(response.text, "html.parser")
    count_el = soup.select_one("#countPageDiv em")
    if not count_el:
        count_el = soup.find(string=re.compile(r"共找到"))
    total = int(
        re.sub(r"\D", "", count_el.get_text() if hasattr(count_el, "get_text") else str(count_el)) or "0"
    )

    rows: list[dict[str, str]] = []
    for tr in soup.select("table.result-table-list tbody tr"):
        checkbox = tr.select_one("input.cbItem")
        title_el = tr.select_one("td.name a.fz14")
        if not checkbox or not title_el:
            continue
        collect = tr.select_one("a.icon-collect")
        dl_el = tr.select_one("a.downloadlink")
        download_link = dl_el.get("href", "") if dl_el else ""
        rows.append(
            {
                "cid": checkbox.get("value", ""),
                "篇名": compact_text(title_el.get_text(" ", strip=True)),
                "article_link": title_el.get("href", ""),
                "download_link": download_link,
                "发表时间": compact_text(
                    tr.select_one("td.date").get_text(" ", strip=True)
                    if tr.select_one("td.date")
                    else ""
                ),
                "list_作者": normalize_people(
                    tr.select_one("td.author").get_text(";", strip=True)
                    if tr.select_one("td.author")
                    else ""
                ),
                "list_刊名": compact_text(
                    tr.select_one("td.source").get_text(" ", strip=True)
                    if tr.select_one("td.source")
                    else ""
                ),
                "filename": collect.get("data-filename", "") if collect else "",
                "dbname": collect.get("data-dbname", "") if collect else "",
            }
        )
    if total > 0 and not rows:
        raise RuntimeError(f"grid {journal} page {page}: no rows parsed")
    turn_el = soup.select_one("#hidTurnPage")
    next_turnpage = turn_el.get("value", "") if turn_el else turnpage
    return total, rows, next_turnpage


def parse_export_item(li: Any) -> dict[str, str]:
    record: dict[str, str] = {}
    current_label: str | None = None
    current_value: list[str] = []

    def flush() -> None:
        nonlocal current_label, current_value
        if current_label is None:
            return
        mapped = FIELD_MAP.get(current_label, current_label)
        record[mapped] = compact_text(" ".join(current_value))
        current_label = None
        current_value = []

    text = li.get_text("\n", strip=True)
    for raw_line in text.split("\n"):
        line = compact_text(raw_line)
        if not line:
            continue
        match = re.match(r"^([^:：]+)[:：]\s*(.*)$", line)
        if match and match.group(1).strip() in FIELD_MAP:
            flush()
            current_label = match.group(1).strip()
            current_value = [match.group(2).strip()]
        elif current_label:
            current_value.append(line)
    flush()

    if "作者" in record:
        record["作者"] = normalize_people(record["作者"])
    if "关键词" in record:
        record["关键词"] = normalize_terms(record["关键词"])
    return record


def fetch_export(
    session: requests.Session,
    rows: list[dict[str, str]],
    journal: str,
    page: int,
    log: LogFn = lambda _msg: None,
) -> list[dict[str, str]]:
    if not rows:
        return []
    data = {
        "FileName": ",".join(row["cid"] for row in rows if row.get("cid")),
        "DisplayMode": "selfDefine",
        "OrderParam": "0",
        "OrderType": "desc",
        "SelectField": SELECT_FIELDS,
        "PageIndex": "1",
        "PageSize": str(max(len(rows), 20)),
        "language": "CHS",
        "uniplatform": "NZKPT",
        "subject": "",
    }
    response = post_with_retries(
        session, f"{BASE}/dm8/api/ShowExport", data, f"export {journal} page {page}", log
    )
    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select("ul.literature-list > li")
    if len(items) > len(rows):
        raise RuntimeError(
            f"export {journal} page {page}: expected {len(rows)} items, "
            f"got {len(items)} (more than list)"
        )
    if len(items) == 0 and rows:
        raise RuntimeError(
            f"export {journal} page {page}: expected {len(rows)} items, got 0"
        )
    if len(items) < len(rows):
        log(
            f"  [warn] export {journal} page {page}: got {len(items)} of "
            f"{len(rows)} items — continuing with available"
        )
    return [parse_export_item(li) for li in items]


def merge_records(
    journal: str, list_rows: list[dict[str, str]], export_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    by_title_time: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    by_title: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in list_rows:
        by_title_time[(safe_key(row["篇名"]), row["发表时间"])].append(row)
        by_title[safe_key(row["篇名"])].append(row)

    merged: list[dict[str, str]] = []
    for exported in export_rows:
        title = exported.get("篇名", "")
        pub_time = exported.get("发表时间", "")
        match_row: dict[str, str] | None = None
        exact_matches = by_title_time.get((safe_key(title), pub_time), [])
        if exact_matches:
            match_row = exact_matches.pop(0)
        else:
            title_matches = by_title.get(safe_key(title), [])
            if title_matches:
                match_row = title_matches.pop(0)

        link = ""
        if match_row:
            link = match_row.get("article_link", "")
        if not link:
            link = exported.get("导出链接", "")

        record = {
            "检索期刊": journal,
            "篇名": title,
            "作者": exported.get("作者") or (match_row or {}).get("list_作者", ""),
            "刊名": exported.get("刊名") or (match_row or {}).get("list_刊名", ""),
            "发表时间": pub_time or (match_row or {}).get("发表时间", ""),
            "链接": link,
            "下载链接": (match_row or {}).get("download_link", ""),
            "摘要": exported.get("摘要", ""),
            "关键词": exported.get("关键词", ""),
        }
        merged.append(record)
    return merged


@dataclass
class ScrapeParams:
    journals: list[str] = field(default_factory=lambda: list(storage.DEFAULT_JOURNALS))
    page_size: int = 50
    max_pages: int = 0
    min_sleep: float = 1.2
    max_sleep: float = 2.2
    start_year: str = "2010"
    end_year: str = "2019"
    fresh: bool = False


@dataclass
class ScrapeStatus:
    running: bool = False
    stopped: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    current_journal: str = ""
    current_page: int = 0
    total_pages: int = 0
    journal_counts: dict[str, int] = field(default_factory=dict)
    records_written_now: int = 0
    last_error: str = ""


class Scraper:
    """抓取任务执行器。一个实例对应一次任务，由 main.py 的 JobManager 持有。"""

    def __init__(self, log_fn: LogFn) -> None:
        self._log = log_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = ScrapeStatus()
        self._params: ScrapeParams | None = None

    # -- 公开 API ----------------------------------------------------------
    def start(self, params: ScrapeParams) -> None:
        if self.status.running:
            raise RuntimeError("scrape already running")
        if not storage.COOKIE_FILE.exists():
            raise BlockedError("Cookie 不存在，请先上传 cnki_cookies.json")
        storage.ensure_dirs()
        if params.fresh:
            storage.reset_progress()
        self._params = params
        self.status = ScrapeStatus(running=True, started_at=time.time())
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.status.stopped = True
        self._log("[stop] 收到停止信号，将在当前页结束后退出")

    def is_running(self) -> bool:
        return self.status.running

    # -- 内部执行 -----------------------------------------------------------
    def _run(self) -> None:
        assert self._params is not None
        p = self._params
        try:
            self._log(
                f"加载 Cookie 并打开 {BASE}/kns8s/AdvSearch 预热会话..."
            )
            session = load_session()
            warm = session.get(f"{BASE}/kns8s/AdvSearch", timeout=30)
            check_blocked(warm, "warmup AdvSearch")
            self._log("预热完成，开始抓取。")

            state = storage.load_state()
            completed: dict[str, list[int]] = state.setdefault("completed_pages", {})
            journal_counts: dict[str, int] = state.setdefault("journal_counts", {})

            started = time.time()
            for j_index, journal in enumerate(p.journals, 1):
                if self._stop_event.is_set():
                    self._log("[stop] 已中断")
                    break
                completed_pages = set(completed.get(journal, []))
                recorded_total = journal_counts.get(journal, 0)
                recorded_pages = (
                    math.ceil(recorded_total / p.page_size) if recorded_total else 0
                )
                if recorded_pages and len(completed_pages) >= recorded_pages:
                    self._log(f"[{j_index}/{len(p.journals)}] {journal} — fully done, skip")
                    continue
                self._log(f"\n[{j_index}/{len(p.journals)}] {journal}")
                self.status.current_journal = journal
                self.status.current_page = 0
                self.status.total_pages = 0

                first_total, first_rows, turnpage = fetch_grid(
                    session, journal, 1, p.page_size, p.start_year, p.end_year
                )
                journal_counts[journal] = first_total
                pages = math.ceil(first_total / p.page_size) if first_total else 0
                if p.max_pages:
                    pages = min(pages, p.max_pages)
                self.status.total_pages = pages
                self._log(
                    f"  total={first_total}, pages={pages}, completed={len(completed_pages)}"
                )

                page_rows_cache: dict[int, list[dict[str, str]]] = {1: first_rows}
                for page in range(1, pages + 1):
                    if self._stop_event.is_set():
                        self._log("[stop] 已中断")
                        break
                    self.status.current_page = page
                    if page in completed_pages:
                        if page != 1:
                            _, _, turnpage = fetch_grid(
                                session, journal, page, p.page_size,
                                p.start_year, p.end_year, turnpage=turnpage,
                                log=self._log,
                            )
                            time.sleep(random.uniform(0.15, 0.35))
                        continue
                    if page in page_rows_cache:
                        rows = page_rows_cache[page]
                    else:
                        _, rows, turnpage = fetch_grid(
                            session, journal, page, p.page_size,
                            p.start_year, p.end_year, turnpage=turnpage,
                            log=self._log,
                        )
                    if first_total > 0 and not rows:
                        raise RuntimeError(
                            f"{journal} page {page}: got no rows; not marking done"
                        )
                    time.sleep(random.uniform(0.2, 0.6))
                    # grid-only 模式：export 端点 /dm8/ 走真实 cnki.net 会 401，
                    # 直接用 grid 行构造 record（与 SearchCollector 一致）
                    records = [
                        {
                            "检索期刊": journal,
                            "篇名": row.get("篇名", ""),
                            "作者": row.get("作者", ""),
                            "刊名": row.get("刊名", ""),
                            "发表时间": row.get("发表时间", ""),
                            "链接": row.get("链接", ""),
                            "下载链接": row.get("下载链接", ""),
                            "摘要": "",
                            "关键词": "",
                        }
                        for row in rows
                    ]
                    storage.append_ndjson(records)
                    self.status.records_written_now += len(records)

                    completed_pages.add(page)
                    completed[journal] = sorted(completed_pages)
                    state["records_written"] = int(
                        state.get("records_written", 0)
                    ) + len(records)
                    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    storage.save_state(state)

                    elapsed = time.time() - started
                    self._log(
                        f"  page {page}/{pages}: rows={len(records)}, "
                        f"written_now={self.status.records_written_now}, "
                        f"elapsed={elapsed/60:.1f}m"
                    )
                    time.sleep(random.uniform(p.min_sleep, p.max_sleep))
                storage.save_state(state)
                if not self._stop_event.is_set():
                    time.sleep(random.uniform(1.5, 3.5))

            # 同步生成 JSON 供前端浏览/统计
            all_records = storage.load_all_records()
            storage.save_json_records(all_records)
            summary = {
                "filters": {
                    "资源": "学术期刊",
                    "出版年度": f"{p.start_year}-{p.end_year}",
                    "来源类别": "CSSCI",
                    "中英文扩展": True,
                },
                "journal_counts": journal_counts,
                "record_count": len(all_records),
                "completed_pages": {k: len(v) for k, v in completed.items()},
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            storage.SUMMARY_PATH.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._log(f"\nDone. records={len(all_records)} -> {storage.JSON_PATH}")
        except BlockedError as exc:
            self.status.last_error = f"BLOCKED: {exc}"
            self._log(f"[BLOCKED] {exc}")
        except Exception as exc:
            self.status.last_error = str(exc)
            self._log(f"[ERROR] {exc!r}")
        finally:
            self.status.running = False
            self.status.finished_at = time.time()
            self.status.current_journal = ""


@dataclass
class SearchCollectParams:
    conditions: list[dict[str, Any]]
    start_year: str | None = None
    end_year: str | None = None
    source_categories: list[str] | None = None
    page_size: int = 50
    max_pages: int = 0
    min_sleep: float = 1.2
    max_sleep: float = 2.2


class SearchCollector:
    """按任意高级检索条件分页抓取并合并到 records，与 Scraper 同样的产物路径。"""

    def __init__(self, log_fn: LogFn) -> None:
        self._log = log_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = ScrapeStatus()
        self._params: SearchCollectParams | None = None

    def start(self, params: SearchCollectParams) -> None:
        if self.status.running:
            raise RuntimeError("search collect already running")
        if not storage.COOKIE_FILE.exists():
            raise BlockedError("Cookie 不存在，请先上传 cnki_cookies.json")
        storage.ensure_dirs()
        self._params = params
        self.status = ScrapeStatus(running=True, started_at=time.time())
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.status.stopped = True
        self._log("[stop] 收到停止信号，将在当前页结束后退出")

    def is_running(self) -> bool:
        return self.status.running

    def _run(self) -> None:
        assert self._params is not None
        p = self._params
        try:
            label = build_aside(p.conditions, p.start_year, p.end_year, p.source_categories) or "高级检索"
            self._log(f"开始按检索条件采集：{label}")
            session = load_session()
            warm = session.get(f"{BASE}/kns8s/AdvSearch", timeout=30)
            check_blocked(warm, "warmup AdvSearch")
            self._log("预热完成，开始检索采集。")

            query = build_query_json(
                p.conditions, p.start_year, p.end_year, p.source_categories
            )
            aside = build_aside(p.conditions, p.start_year, p.end_year, p.source_categories)
            search_from = build_search_from(
                p.conditions, p.start_year, p.end_year, p.source_categories
            )

            first_total, first_rows, turnpage = search_grid(
                session, query, aside, search_from, 1, p.page_size, log=self._log
            )
            pages = math.ceil(first_total / p.page_size) if first_total else 0
            if p.max_pages:
                pages = min(pages, p.max_pages)
            self.status.total_pages = pages
            self.status.journal_counts = {"搜索结果": first_total}
            self._log(f"  total={first_total}, pages={pages}")

            started = time.time()
            page_rows_cache: dict[int, list[dict[str, str]]] = {1: first_rows}
            for page in range(1, pages + 1):
                if self._stop_event.is_set():
                    self._log("[stop] 已中断")
                    break
                self.status.current_page = page
                if page in page_rows_cache:
                    rows = page_rows_cache[page]
                else:
                    _, rows, turnpage = search_grid(
                        session, query, aside, search_from, page, p.page_size,
                        turnpage=turnpage, log=self._log,
                    )
                if first_total > 0 and not rows:
                    raise RuntimeError(f"search page {page}: got no rows; not marking done")
                # grid-only 模式：export 端点 /dm8/ 走真实 cnki.net 会 401，
                # sclib.cn 代理 cookie 不覆盖该路径。直接用 grid 行构造 record，
                # 并把 grid 里的下载链接（bar/download/order）一并存入，供 PDF下载使用。
                records = [
                    {
                        "检索期刊": label,
                        "篇名": row.get("篇名", ""),
                        "作者": row.get("作者", ""),
                        "刊名": row.get("刊名", ""),
                        "发表时间": row.get("发表时间", ""),
                        "链接": row.get("链接", ""),
                        "下载链接": row.get("下载链接", ""),
                        "摘要": "",
                        "关键词": "",
                    }
                    for row in rows
                ]
                storage.append_ndjson(records)
                self.status.records_written_now += len(records)
                elapsed = time.time() - started
                self._log(
                    f"  page {page}/{pages}: rows={len(records)}, "
                    f"written_now={self.status.records_written_now}, "
                    f"elapsed={elapsed/60:.1f}m"
                )
                time.sleep(random.uniform(p.min_sleep, p.max_sleep))

            all_records = storage.load_all_records()
            storage.save_json_records(all_records)
            storage.SUMMARY_PATH.write_text(
                json.dumps({
                    "filters": {
                        "检索条件": label,
                        "出版年度": f"{p.start_year}-{p.end_year}" if p.start_year else "不限",
                        "来源类别": "、".join(p.source_categories or []) or "不限",
                    },
                    "record_count": len(all_records),
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._log(f"\nDone. records={len(all_records)} -> {storage.JSON_PATH}")
        except BlockedError as exc:
            self.status.last_error = f"BLOCKED: {exc}"
            self._log(f"[BLOCKED] {exc}")
        except Exception as exc:
            self.status.last_error = str(exc)
            self._log(f"[ERROR] {exc!r}")
        finally:
            self.status.running = False
            self.status.finished_at = time.time()
