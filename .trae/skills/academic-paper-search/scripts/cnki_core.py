#!/usr/bin/env python3
"""知网（CNKI sclib.cn 代理镜像）核心客户端 —— skill 自包含版。

代码来源：zhiwang 项目 backend/scraper.py 与 backend/pdf_downloader.py 的公共子集
（kns8s 一框式检索签名/QueryJson/grid 解析/翻页惯例/反爬检测/直连下载/CAJ→PDF
解密转换），本文件不依赖 zhiwang 项目，可独立运行。

依赖：requests、beautifulsoup4；CAJ→PDF 转换另需 PyPDF2（缺失时保留 .caj 原文件）。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LogFn = Callable[[str], None]

BASE = "https://kns--cnki--net.share.sclib.cn"

# kns8s 新版检索（一框式 TOPRANK 口径）所需的请求签名盐，
# 来自 kns8s 页面 common.min.js 的 createSign 函数。
_SIGN_SALT = "t8b52yrsoyx66f35tk0p4nubrmrcglv5"

# 高级检索字段元数据：CNKI 字段代码 → (中文标题, 占位提示)
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

# 总库（CROSSDB）包含的子库代码，与官网总库检索页 crossids 一致。
# 归属经实测确认（智慧应急检索各单库返回的来源类型）。
CROSSDB_CODES = [
    "YSTT4HG0",  # 学术期刊
    "LSTPFY1C",  # 学位论文
    "JUP3MUPD",  # 会议
    "MPMFIG1A",  # 报纸
    "EMRPGLPA",  # 图书
    "NN3FJMUV",  # 特色期刊
    "BLZOG7CK",  # 科技成果
    "WQ0UVIAA",  # 年鉴
    "PWFIRAGL",  # 标准
    "NLBO1Z6R",  # 专利
]

# 逻辑：0=AND, 1=OR, 2=NOT
LOGIC_AND = 0


class BlockedError(RuntimeError):
    """CNKI 触发安全验证时抛出，可据此提示重新录入 Cookie。"""


# --------------------------------------------------------------------------- #
# 动态签名（kns8s grid 请求必需）
# --------------------------------------------------------------------------- #
def _js_sin_str(x: float) -> str:
    """复刻 JS Math.sin(x).toString() 的最短小数表示。"""
    s = repr(math.sin(x))
    return re.sub(r"e([+-])0+(\d)", r"e\1\2", s)


def sign_headers(client_id: str = "") -> dict[str, str]:
    """生成 kns8s grid 请求的动态签名头（timestamp/nonce/signature/appID/ClientID）。"""
    ts = int(time.time() * 1000)
    nonce = _js_sin_str(ts)[6:]
    signature = hashlib.md5(
        f"{ts}{nonce}{_SIGN_SALT}{client_id}".encode("utf-8")
    ).hexdigest()
    return {
        "timestamp": str(ts),
        "nonce": nonce,
        "signature": signature,
        "appID": "LoginWap",
        "ClientID": client_id,
    }


# --------------------------------------------------------------------------- #
# 文本工具
# --------------------------------------------------------------------------- #
def compact_text(value: str | None) -> str:
    if not value:
        return ""
    import html
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    return value.strip()


def normalize_people(value: str) -> str:
    parts = [compact_text(p) for p in re.split(r"[;；]", value) if compact_text(p)]
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# QueryJson 构造（新版一框式口径，总库，与官网检索结果一致）
# --------------------------------------------------------------------------- #
def build_query_json(
    conditions: list[dict[str, Any]],
    start_year: str | None = None,
    end_year: str | None = None,
    source_categories: list[str] | None = None,
    sub_dbs: list[str] | None = None,
) -> dict[str, Any]:
    """通用高级检索 QueryJson 构造器。

    conditions: [{"field": "SU", "value": "数字经济", "logic": 0}]
      logic: 0=AND, 1=OR, 2=NOT（默认 AND）
    start_year/end_year: 出版年度范围；为空则不限。
    source_categories: ["CSI"] 等；为空则不限来源类别。
    sub_dbs: 子库代码多选筛选（CROSSDB_CODES 的子集）；为空/全选 = 总库全部子库。

    与官网总库页一致：Resource=CROSSDB + 全部子库 KuaKuCode，
    主题等条件用 Operator=TOPRANK + SearchType=2 的一框式匹配；
    时间范围（YE）与来源类别仍通过 ControlGroup 生效。
    """
    field_title = {code: title for code, title, _ in FIELD_META}
    src_title = {code: title for code, title in SOURCE_CATEGORIES}

    items: list[dict[str, Any]] = []
    for idx, cond in enumerate(conditions):
        field = cond.get("field", "SU")
        value = (cond.get("value") or "").strip()
        if not value:
            continue
        logic = int(cond.get("logic", LOGIC_AND)) if idx > 0 else 0
        title = field_title.get(field, field)
        items.append({
            "Field": field,
            "Value": value,
            "Operator": "TOPRANK",
            "Logic": logic,
            "Vector": "",
            "Title": title,
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
        cat_items = []
        for i, code in enumerate(source_categories):
            cat_items.append({
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
            "Items": cat_items,
            "ChildItems": [],
        })

    qgroup: list[dict[str, Any]] = [{
        "Key": "Subject",
        "Title": "",
        "Logic": 0,
        "Items": items,
        "ChildItems": [],
    }]
    if control_children:
        qgroup.append({
            "Key": "ControlGroup",
            "Title": "",
            "Logic": 0,
            "Items": [],
            "ChildItems": control_children,
        })

    kua_ku = [c for c in (sub_dbs or []) if c in CROSSDB_CODES] or CROSSDB_CODES
    return {
        "Platform": "",
        "Resource": "CROSSDB",
        "Classid": "WD0FTY92",
        "Products": "",
        "QNode": {"QGroup": qgroup},
        "ExScope": 1,
        "SimpTrad": "0",
        "SearchType": 2,
        "Rlang": "CHINESE",
        "KuaKuCode": ",".join(kua_ku),
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
    search_from = "资源范围：总库;  中英文扩展;  "
    if start_year and end_year:
        search_from += f"时间范围：出版年度：{start_year} 到 {end_year},更新时间：不限;  "
    if source_categories:
        src_title = {code: title for code, title in SOURCE_CATEGORIES}
        cats = "、".join(src_title.get(c, c) for c in source_categories)
        search_from += f"来源类别：{cats}; "
    return search_from


# --------------------------------------------------------------------------- #
# 反爬检测与带重试请求
# --------------------------------------------------------------------------- #
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
    # "login--" 覆盖 bar/kns 等域名的登录跳转（如 login--cnki--net.share.sclib.cn）
    login_redirect = (
        "login.share.sclib.cn" in final_url
        or "sclib.cn/page/" in final_url
        or "login--" in final_url
        or "login.share.sclib.cn" in x_redirect
        or "sclib.cn/page/" in x_redirect
        or "login--" in x_redirect
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
                "请重新登录 sclib.cn 并按 references/cookie-guide.md 导出新的 Cookie"
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


def post_with_retries(
    session: requests.Session,
    url: str,
    data: dict[str, Any],
    context: str,
    log: LogFn,
    timeout: int = 45,
    retries: int = 3,
    signed: bool = False,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            headers = None
            if signed:
                # 新版一框式口径需要动态签名头；Ecp_ClientId 取自 Cookie（可能为空，实测同样有效）
                headers = sign_headers(
                    session.cookies.get("Ecp_ClientId", "") or ""
                )
            response = session.post(url, data=data, headers=headers, timeout=timeout)
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


# --------------------------------------------------------------------------- #
# grid 检索与行解析
# --------------------------------------------------------------------------- #
def search_grid(
    session: requests.Session,
    query: dict[str, Any],
    aside: str,
    search_from: str,
    page: int,
    page_size: int,
    turnpage: str = "",
    sort_field: str = "",
    sort_type: str = "DESC",
    log: LogFn = lambda _msg: None,
) -> tuple[int, list[dict[str, str]], str]:
    """通用 grid 检索。返回 (总条数, 本页行, 下页 turnpage)。

    注意：pageSize 只接受 20/50，非法值（如 5、10）服务端静默返回 0 条。
    翻页惯例：boolSearch=false 且不传 CurPage，turnpage 用上一页返回的令牌。
    """
    bool_search = page == 1
    data = {
        "boolSearch": "true" if bool_search else "false",
        "QueryJson": json.dumps(query, ensure_ascii=False, separators=(",", ":")),
        "pageNum": str(page),
        "pageSize": str(page_size),
        "dstyle": "listmode",
        "boolSortSearch": "false",
        "sentenceSearch": "false",
        "productStr": "",
        "aside": aside,
        "searchFrom": search_from,
        "CurPage": str(page),
        "turnpage": turnpage,
    }
    # 排序："" = 相关度（官网默认，无 sortField）；PT=发表时间 CF=被引 DFR=下载 ZH=综合
    if sort_field:
        data["sortField"] = sort_field
        data["sortType"] = sort_type or "DESC"
        data["boolSortSearch"] = "true"
    if not bool_search:
        data.pop("CurPage", None)
    response = post_with_retries(
        session, f"{BASE}/kns8s/brief/grid", data, f"search grid page {page}", log,
        signed=True,
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
        # 数据库（来源类型中文，如"期刊"/"报纸"）；被引/下载计数（官网列表列）
        data_el = tr.select_one("td.data span")
        quote_el = tr.select_one("td.quote a.quoteCnt")
        dlc_el = tr.select_one("td.download a.downloadCnt")
        rows.append({
            "cid": checkbox.get("value", ""),
            "篇名": compact_text(title_el.get_text(" ", strip=True)),
            "链接": title_el.get("href", ""),
            "下载链接": download_link,
            "数据库": compact_text(data_el.get_text(" ", strip=True)) if data_el else "",
            "被引": compact_text(quote_el.get_text(strip=True)) if quote_el else "",
            "下载": compact_text(dlc_el.get_text(strip=True)) if dlc_el else "",
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


# --------------------------------------------------------------------------- #
# PDF/CAJ 下载与 CAJ→PDF 转换
# --------------------------------------------------------------------------- #
def safe_filename(title: str, pub_time: str) -> str:
    """把篇名+发表时间转成安全的文件名（去非法字符、限长），不含扩展名。

    扩展名由下载响应的 Content-Type/Disposition 决定（.caj 或 .pdf）。
    """
    base = f"{title}_{pub_time}".strip()
    base = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", base)
    base = re.sub(r"\s+", " ", base)
    if len(base) > 120:
        base = base[:120]
    return base


def _ext_from_response(resp: requests.Response) -> str:
    """根据响应头推断文件扩展名：优先 Content-Disposition 的 filename，其次 Content-Type。"""
    disp = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:utf-8\'\')?"?([^";]+)"?$', disp) or re.search(r'filename="?([^";]+)"?', disp)
    if m:
        name = m.group(1)
        low = name.lower()
        if low.endswith(".caj"):
            return ".caj"
        if low.endswith(".pdf"):
            return ".pdf"
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "caj" in ctype:
        return ".caj"
    if "pdf" in ctype:
        return ".pdf"
    return ".caj"  # sclib.cn 代理下多为 CAJ


def download_direct(
    session: requests.Session,
    dl_link: str,
    base_name: str,
    title: str,
    idx: int,
    total: int,
    out_dir: Path,
    detail_link: str = "",
    log: LogFn = lambda _msg: None,
) -> Path:
    """直接 GET grid 行里的下载链接（bar/download/order），流式存盘到 out_dir。

    sclib.cn 代理会重定向到 docdown/fulltext/download，返回 CAJ/PDF。
    绕过详情页，不会触发 verify 验证码。若返回 CAJ 且提供了详情页链接，
    会先尝试详情页的『PDF下载』按钮拿原生 PDF；拿不到再落盘 CAJ 并
    自动转 PDF（转换成功删除 CAJ；失败保留 CAJ）。返回最终文件路径。
    """
    log(f"[{idx}/{total}] 下载: {title[:40]}")
    # 兜底：grid 行的 href 可能是相对路径，补全为绝对地址
    if dl_link.startswith("/bar/"):
        dl_link = "https://bar--cnki--net.share.sclib.cn" + dl_link
    elif dl_link.startswith("/") and not dl_link.startswith("//"):
        dl_link = urljoin(BASE, dl_link)
    resp = session.get(dl_link, timeout=90, stream=True, allow_redirects=True)
    check_blocked(resp, f"download {title}")
    ext = _ext_from_response(resp)
    target = out_dir / (base_name + ext)
    # 嗅探首个内容块：sclib 代理跳登录页/验证页时可能仍带 CAJ 的
    # Content-Disposition 头，必须以实际内容为准拦截，避免把 HTML 存成 .caj
    chunks = resp.iter_content(chunk_size=65536)
    first = next(chunks, b"")
    head = first[:400].decode("utf-8", errors="ignore").lower()
    if ("安全验证" in head or "login" in head or "verify" in head
            or "<!doctype html" in head or "<html" in head):
        raise BlockedError(
            f"download {title}: 下载链接返回登录/验证页"
            "（下载通道要求登录：Cookie 已失效或该文献需账号权限）"
        )
    # 返回 CAJ 时优先尝试详情页的『PDF下载』按钮（能拿原生 PDF 就不存 CAJ）
    if ext == ".caj" and detail_link:
        pdf = _try_detail_pdf(session, detail_link, base_name, title, out_dir, log)
        if pdf:
            return pdf
    tmp = target.with_suffix(target.suffix + ".part")
    with tmp.open("wb") as fh:
        fh.write(first)
        for chunk in chunks:
            if chunk:
                fh.write(chunk)
    tmp.replace(target)
    size = target.stat().st_size
    log(f"  [ok] saved {target.name} ({size//1024} KB)")
    # CAJ → PDF 自动转换
    if target.suffix.lower() == ".caj":
        pdf = _caj_to_pdf(target, log)
        if pdf and pdf.exists():
            log(
                f"  [ok] 转换 PDF: {pdf.name} "
                f"({pdf.stat().st_size // 1024} KB)"
            )
            target.unlink()  # 转换成功后删除 CAJ 原文件
            return pdf
        # 转换失败时保留 CAJ 文件，用户仍可手动处理
    return target


def _try_detail_pdf(
    session: requests.Session,
    detail_link: str,
    base_name: str,
    title: str,
    out_dir: Path,
    log: LogFn = lambda _msg: None,
) -> Path | None:
    """访问文献详情页找『PDF下载』按钮并下载原生 PDF。

    详情页必须走 sclib 代理域（原始 kns.cnki.net 域会触发验证码）。
    成功返回 PDF 路径；无按钮/被拦截/格式不对时返回 None（调用方回退）。
    """
    url = detail_link
    if url.startswith("https://kns.cnki.net"):
        url = url.replace(
            "https://kns.cnki.net", "https://kns--cnki--net.share.sclib.cn"
        )
    elif url.startswith("/"):
        url = urljoin(BASE, url)
    if not url.startswith("http"):
        return None
    try:
        resp = session.get(url, timeout=45)
        check_blocked(resp, f"detail {title}")
        soup = BeautifulSoup(resp.text, "html.parser")
        pdf_url = ""
        for a in soup.select("a"):
            if a.get_text(strip=True) == "PDF下载" and a.get("href"):
                pdf_url = urljoin(url, a["href"])
                break
        if not pdf_url:
            return None
        dl = session.get(pdf_url, timeout=90, stream=True, allow_redirects=True)
        check_blocked(dl, f"pdf {title}")
        if _ext_from_response(dl) != ".pdf":
            return None
        chunks = dl.iter_content(chunk_size=65536)
        first = next(chunks, b"")
        head = first[:400].decode("utf-8", errors="ignore").lower()
        if ("安全验证" in head or "login" in head or "verify" in head
                or "<!doctype html" in head or "<html" in head):
            return None
        target = out_dir / (base_name + ".pdf")
        tmp = target.with_suffix(".pdf.part")
        with tmp.open("wb") as fh:
            fh.write(first)
            for chunk in chunks:
                if chunk:
                    fh.write(chunk)
        tmp.replace(target)
        log(
            f"  [ok] saved {target.name} "
            f"({target.stat().st_size // 1024} KB, 详情页 PDF)"
        )
        return target
    except Exception as exc:
        log(f"  [warn] 详情页 PDF 获取失败，回退默认下载: {exc}")
        return None


def _caj_to_pdf(caj_path: Path, log: LogFn = lambda _msg: None) -> Path | None:
    """把 CNKI 的 KDH/CAJ 文件转成标准 PDF。

    KDH 格式本质是 254 字节头 + XOR 加密（密码 FZHMEI）的 PDF。
    解密后用 PyPDF2 修复 xref 表写出合法 PDF。
    无 mutool 依赖，纯 Python 实现。
    """
    try:
        import io
        from PyPDF2 import PdfReader, PdfWriter
    except ImportError:
        log("  [skip] PyPDF2 未安装，跳过 CAJ→PDF 转换")
        return None

    KDH_PASSPHRASE = b"FZHMEI"
    try:
        with open(caj_path, "rb") as f:
            raw = f.read()
        # KDH 格式：跳过 254 字节头 + XOR 解密
        if raw[:4] == b"KDH ":
            body = raw[254:]
            decrypted = bytes(
                b ^ KDH_PASSPHRASE[i % len(KDH_PASSPHRASE)]
                for i, b in enumerate(body)
            )
            eof = decrypted.rfind(b"%%EOF")
            if eof < 0:
                log("  [skip] CAJ 内未找到 %%EOF 标记，无法转换")
                return None
            pdf_data = decrypted[: eof + 5]
        elif raw[:4] == b"%PDF":
            pdf_data = raw  # 已经是 PDF
        else:
            log(f"  [skip] 未知 CAJ 格式（头: {raw[:8]}），跳过转换")
            return None

        reader = PdfReader(io.BytesIO(pdf_data))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        pdf_path = caj_path.with_suffix(".pdf")
        with open(pdf_path, "wb") as f:
            writer.write(f)
        return pdf_path
    except Exception as exc:
        log(f"  [warn] CAJ→PDF 转换失败: {exc}")
        return None
