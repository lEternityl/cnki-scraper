"""文献 PDF 批量下载器。

策略：
1. 使用与抓取器同一份 Cookie 建立 session；
2. 访问记录中的 "链接"（CNKI 详情页），从 HTML 解析 PDF 下载按钮；
3. 兼容多种选择器与 articleDownload / pdf 字样链接，找到就流式下载到 PDF_DIR；
4. 文件名以"篇名+发表时间"安全化为文件名，已存在则默认跳过（可覆盖）；
5. 失败计入失败列表，最后一次性返回。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import storage
from .scraper import (
    BlockedError, check_blocked, load_session, BASE, resolve_download_link,
)

LogFn = Callable[[str], None]


# 详情页上常见的 PDF 下载按钮选择器（按优先级）
_PDF_SELECTORS = [
    "a.btn-dlpdf",
    "a.btn-pdf",
    "a.pdfDown",
    "a[href*='articleDownload']",
    "a[href*='downloadArticle']",
    "a[href*='.pdf']",
    "a[class*='pdf']",
]


@dataclass
class DownloadParams:
    records: list[dict[str, str]]
    overwrite: bool = False
    min_sleep: float = 0.6
    max_sleep: float = 1.5
    timeout: int = 60


@dataclass
class DownloadStatus:
    running: bool = False
    stopped: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    current_title: str = ""
    last_error: str = ""
    failures: list[dict[str, str]] = field(default_factory=list)


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


def find_pdf_url(detail_url: str, soup: BeautifulSoup) -> str:
    """从详情页 HTML 中尽力找出 PDF 下载 URL（绝对地址）。"""
    for sel in _PDF_SELECTORS:
        node = soup.select_one(sel)
        if not node:
            continue
        href = node.get("href") or node.get("data-url") or ""
        if href:
            return urljoin(detail_url, href)
    # 兜底：找任何 onclick 含 pdf/articleDownload 的按钮
    for node in soup.select("a[onclick]"):
        onclick = node.get("onclick", "")
        m = re.search(r"(https?://[^'\"\s]+(?:articleDownload|\.pdf)[^'\"\s]*)", onclick)
        if m:
            return m.group(1)
    return ""


class PdfDownloader:
    def __init__(self, log_fn: LogFn) -> None:
        self._log = log_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = DownloadStatus()

    def start(self, params: DownloadParams) -> None:
        if self.status.running:
            raise RuntimeError("download already running")
        if not storage.COOKIE_FILE.exists():
            raise BlockedError("Cookie 不存在，请先上传 cnki_cookies.json")
        storage.ensure_dirs()
        self.status = DownloadStatus(
            running=True, started_at=time.time(), total=len(params.records)
        )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(params,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.status.stopped = True
        self._log("[stop] 收到停止信号，将在当前 PDF 结束后退出")

    def is_running(self) -> bool:
        return self.status.running

    # -- 内部 -------------------------------------------------------------
    def _run(self, params: DownloadParams) -> None:
        try:
            self._log("加载 Cookie 并建立会话...")
            session = load_session()
            # 预热会话：部分记录需回查 grid 获取下载链接，grid 检索要求先访问 AdvSearch
            try:
                warm = session.get(f"{BASE}/kns8s/AdvSearch", timeout=30)
                check_blocked(warm, "warmup AdvSearch")
            except Exception as exc:
                self._log(f"[warn] 预热失败（不影响已有下载链接的记录）: {exc}")
            self._log(f"开始批量下载，共 {len(params.records)} 篇。")
            for idx, rec in enumerate(params.records, 1):
                if self._stop_event.is_set():
                    self._log("[stop] 已中断")
                    break
                title = rec.get("篇名") or f"record-{idx}"
                pub_time = rec.get("发表时间", "")
                self.status.current_title = title
                self.status.done = idx - 1
                base_name = safe_filename(title, pub_time)  # 不含扩展名
                # 已存在则跳过（检查 .caj 和 .pdf 两种）
                existing = [storage.PDF_DIR / (base_name + ext) for ext in (".caj", ".pdf")]
                if any(p.exists() for p in existing) and not params.overwrite:
                    self.status.skipped += 1
                    ex = next(p.name for p in existing if p.exists())
                    self._log(f"[{idx}/{len(params.records)}] skip 已存在: {ex}")
                    continue
                # 优先用 grid 里的下载链接（bar/download/order），不访问详情页
                dl_link = rec.get("下载链接") or ""
                detail_link = rec.get("链接") or ""
                try:
                    if not dl_link and title:
                        # 旧记录没有下载链接，按篇名回查 grid 即时补全（绕过详情页验证码）
                        self._log(f"[{idx}/{len(params.records)}] 回查下载链接: {title[:30]}")
                        dl_link = resolve_download_link(session, title, log=self._log)
                    if dl_link:
                        self._download_direct(
                            session, dl_link, base_name, title, idx, total=len(params.records)
                        )
                    elif detail_link:
                        # 回退：访问详情页找 PDF（很可能触发验证码）
                        target = storage.PDF_DIR / (base_name + ".pdf")
                        self._download_via_detail(
                            session, detail_link, target, title, idx, total=len(params.records)
                        )
                    else:
                        raise RuntimeError("无下载链接且无详情页链接")
                    self.status.done = idx
                except BlockedError as exc:
                    self.status.failed += 1
                    self.status.failures.append({"title": title, "reason": f"BLOCKED: {exc}"})
                    self._log(f"[{idx}/{len(params.records)}] BLOCKED: {exc}")
                except Exception as exc:
                    self.status.failed += 1
                    self.status.failures.append({"title": title, "reason": str(exc)})
                    self._log(f"[{idx}/{len(params.records)}] 失败: {exc!r}")
                time.sleep(random.uniform(params.min_sleep, params.max_sleep))
            self._log(
                f"\nDone. done={self.status.done}, skipped={self.status.skipped}, "
                f"failed={self.status.failed}"
            )
        except Exception as exc:
            self.status.last_error = str(exc)
            self._log(f"[ERROR] {exc!r}")
        finally:
            self.status.running = False
            self.status.finished_at = time.time()
            self.status.current_title = ""

    def _download_direct(
        self,
        session: requests.Session,
        dl_link: str,
        base_name: str,
        title: str,
        idx: int,
        total: int,
    ) -> None:
        """直接 GET grid 行里的下载链接（bar/download/order），流式存盘。

        sclib.cn 代理会重定向到 docdown/fulltext/download，返回 CAJ/PDF。
        绕过详情页，不会触发 verify 验证码。
        """
        self._log(f"[{idx}/{total}] 下载: {title[:40]}")
        # 兜底：grid 行的 href 可能是相对路径，补全为绝对地址
        if dl_link.startswith("/bar/"):
            dl_link = "https://bar--cnki--net.share.sclib.cn" + dl_link
        elif dl_link.startswith("/") and not dl_link.startswith("//"):
            dl_link = urljoin(BASE, dl_link)
        resp = session.get(dl_link, timeout=90, stream=True, allow_redirects=True)
        check_blocked(resp, f"download {title}")
        ext = _ext_from_response(resp)
        target = storage.PDF_DIR / (base_name + ext)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        # 非预期二进制时检查是否跳到验证页
        if ("caj" not in ctype and "pdf" not in ctype
                and "octet-stream" not in ctype
                and not resp.headers.get("Content-Disposition", "")):
            head = resp.raw.read(400, decode_content=True) if resp.raw else b""
            try:
                ht = head.decode("utf-8", errors="ignore").lower()
            except Exception:
                ht = ""
            if "安全验证" in ht or "verify" in ht or "login" in ht:
                raise BlockedError(f"download {title}: redirected to verification/login")
        tmp = target.with_suffix(target.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        tmp.replace(target)
        size = target.stat().st_size
        self._log(f"  [ok] saved {target.name} ({size//1024} KB)")
        # CAJ → PDF 自动转换
        if target.suffix.lower() == ".caj":
            pdf = self._caj_to_pdf(target)
            if pdf and pdf.exists():
                self._log(
                    f"  [ok] 转换 PDF: {pdf.name} "
                    f"({pdf.stat().st_size // 1024} KB)"
                )
                target.unlink()  # 转换成功后删除 CAJ 原文件
            # 转换失败时保留 CAJ 文件，用户仍可手动处理

    def _caj_to_pdf(self, caj_path: Path) -> Path | None:
        """把 CNKI 的 KDH/CAJ 文件转成标准 PDF。

        KDH 格式本质是 254 字节头 + XOR 加密（密码 FZHMEI）的 PDF。
        解密后用 PyPDF2 修复 xref 表写出合法 PDF。
        无 mutool 依赖，纯 Python 实现。
        """
        try:
            import io
            from PyPDF2 import PdfReader, PdfWriter
        except ImportError:
            self._log("  [skip] PyPDF2 未安装，跳过 CAJ→PDF 转换")
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
                    self._log("  [skip] CAJ 内未找到 %%EOF 标记，无法转换")
                    return None
                pdf_data = decrypted[: eof + 5]
            elif raw[:4] == b"%PDF":
                pdf_data = raw  # 已经是 PDF
            else:
                self._log(f"  [skip] 未知 CAJ 格式（头: {raw[:8]}），跳过转换")
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
            self._log(f"  [warn] CAJ→PDF 转换失败: {exc}")
            return None

    def _download_via_detail(
        self,
        session: requests.Session,
        link: str,
        target: Path,
        title: str,
        idx: int,
        total: int,
    ) -> None:
        """回退路径：访问详情页找 PDF 按钮。sclib.cn 代理下详情页常触发验证码。"""
        detail_url = urljoin(BASE, link)
        self._log(f"[{idx}/{total}] 抓详情页(回退): {title[:40]}")
        resp = session.get(detail_url, timeout=45)
        check_blocked(resp, f"detail {title}")
        soup = BeautifulSoup(resp.text, "html.parser")
        pdf_url = find_pdf_url(detail_url, soup)
        if not pdf_url:
            raise RuntimeError("详情页未找到 PDF 下载链接（可能触发验证码）")
        self._log(f"  → 下载: {urlparse(pdf_url).path}")
        dl = session.get(pdf_url, timeout=60, stream=True, allow_redirects=True)
        check_blocked(dl, f"pdf {title}")
        ctype = (dl.headers.get("Content-Type") or "").lower()
        if "pdf" not in ctype and not dl.headers.get("Content-Disposition", ""):
            head_bytes = dl.raw.read(400, decode_content=True) if dl.raw else b""
            try:
                head_text = head_bytes.decode("utf-8", errors="ignore").lower()
            except Exception:
                head_text = ""
            if "安全验证" in head_text or "verify" in head_text:
                raise BlockedError(f"pdf {title}: redirected to verification")
        tmp = target.with_suffix(".pdf.part")
        with tmp.open("wb") as fh:
            for chunk in dl.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        tmp.replace(target)
        size = target.stat().st_size
        self._log(f"  [ok] saved {target.name} ({size//1024} KB)")
