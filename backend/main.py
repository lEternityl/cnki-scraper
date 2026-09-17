"""FastAPI 入口：CNKI 抓取/检索/统计/PDF 下载的统一后端。

启动：
    cd 知网
    uvicorn backend.main:app --reload --port 8000

API：
    GET    /api/health
    GET    /api/journals
    GET    /api/records?keyword=&journal=&author=&year=&page=1&page_size=20
    GET    /api/records/stats
    GET    /api/cookie
    POST   /api/cookie            (multipart 字段 file = cookie JSON 文本)
    DELETE /api/cookie
    GET    /api/scrape/state       (state.json 内容)
    GET    /api/scrape/summary
    POST   /api/scrape/start       (body: ScrapeStartIn)
    POST   /api/scrape/stop
    GET    /api/scrape/status
    GET    /api/scrape/logs        (text/event-stream)
    POST   /api/pdf/start          (body: PdfStartIn)
    POST   /api/pdf/stop
    GET    /api/pdf/status
    GET    /api/pdf/logs           (text/event-stream)
    GET    /api/pdf/files
    GET    /api/pdf/files/{name}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import storage
from .importer import import_records
from .scraper import (
    ScrapeParams, Scraper, BlockedError,
    SearchCollectParams, SearchCollector,
    build_query_json, build_aside, build_search_from, search_grid,
    load_session, FIELD_META, SOURCE_CATEGORIES,
)
from .pdf_downloader import DownloadParams, PdfDownloader


# --------------------------------------------------------------------------- #
# 日志总线：抓取/下载线程把消息塞进广播队列，SSE 客户端各自消费一份。
# --------------------------------------------------------------------------- #
class LogBroker:
    """多订阅者的线程安全日志广播器，附带最近 500 条历史。"""

    def __init__(self, history: int = 500) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._history: deque[str] = deque(maxlen=history)

    def publish(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._lock:
            self._history.append(line)
            subs = list(self._subs)
        for q in subs:
            q.put(line)

    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue()
        with self._lock:
            # 新订阅者先把历史灌进去，再注册自己
            for line in list(self._history):
                q.put(line)
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def history(self) -> list[str]:
        with self._lock:
            return list(self._history)


scrape_broker = LogBroker()
pdf_broker = LogBroker()
search_broker = LogBroker()


def scrape_log(msg: str) -> None:
    scrape_broker.publish(msg)


def pdf_log(msg: str) -> None:
    pdf_broker.publish(msg)


def search_log(msg: str) -> None:
    search_broker.publish(msg)


# --------------------------------------------------------------------------- #
# 单例任务管理器：同时只允许一个抓取/下载任务运行。
# --------------------------------------------------------------------------- #
scraper = Scraper(log_fn=scrape_log)
pdf_downloader = PdfDownloader(log_fn=pdf_log)
search_collector = SearchCollector(log_fn=search_log)


# --------------------------------------------------------------------------- #
# 检索会话缓存：CNKI 翻页必须带前一页返回的 turnpage 令牌，而 /api/search 是
# 无状态 REST 调用，所以按"查询签名"缓存 session + turnpage + rows，TTL 10 分钟。
# --------------------------------------------------------------------------- #
class SearchSessionCache:
    TTL = 600  # 秒

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}

    def _key(self, body: "SearchIn") -> str:
        conds = sorted(
            [{"f": c.field, "v": c.value, "l": c.logic} for c in body.conditions],
            key=lambda x: (x["f"], x["v"]),
        )
        raw = json.dumps({
            "c": conds,
            "sy": body.start_year, "ey": body.end_year,
            "src": sorted(body.source_categories or []),
            "ps": body.page_size,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get(self, body: "SearchIn") -> dict:
        key = self._key(body)
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry and now - entry["ts"] < self.TTL:
                return entry
            entry = {
                "key": key,
                "session": load_session(),
                "query": build_query_json(
                    [c.model_dump() for c in body.conditions],
                    body.start_year, body.end_year, body.source_categories,
                ),
                "aside": build_aside(
                    [c.model_dump() for c in body.conditions],
                    body.start_year, body.end_year, body.source_categories,
                ),
                "search_from": build_search_from(
                    [c.model_dump() for c in body.conditions],
                    body.start_year, body.end_year, body.source_categories,
                ),
                "turnpage_to_call": {1: ""},  # page N → 请求该页时传入的 turnpage
                "rows_cache": {},             # page N → rows
                "total": 0,
                "ts": now,
            }
            self._entries[key] = entry
            # 清理过期
            stale = [k for k, v in self._entries.items() if now - v["ts"] > self.TTL]
            for k in stale:
                self._entries.pop(k, None)
            return entry

    def invalidate_all(self) -> None:
        with self._lock:
            self._entries.clear()


search_cache = SearchSessionCache()


# --------------------------------------------------------------------------- #
# Pydantic 输入模型
# --------------------------------------------------------------------------- #
class ScrapeStartIn(BaseModel):
    journals: Optional[List[str]] = None
    page_size: int = 50
    max_pages: int = 0
    min_sleep: float = 1.2
    max_sleep: float = 2.2
    start_year: str = "2010"
    end_year: str = "2019"
    fresh: bool = False


class PdfStartIn(BaseModel):
    keyword: Optional[str] = None
    journal: Optional[str] = None
    author: Optional[str] = None
    year: Optional[str] = None
    indices: Optional[List[int]] = None  # 优先用索引筛选（按当前 records 顺序）
    overwrite: bool = False
    min_sleep: float = 0.6
    max_sleep: float = 1.5


class SearchCondition(BaseModel):
    field: str = "SU"
    value: str
    logic: int = 0  # 0=AND, 1=OR, 2=NOT


class SearchIn(BaseModel):
    conditions: List[SearchCondition]
    start_year: Optional[str] = None
    end_year: Optional[str] = None
    source_categories: Optional[List[str]] = None
    page: int = 1
    page_size: int = 20
    sort_field: str = "FFD"
    sort_type: str = "DESC"


class SearchCollectIn(BaseModel):
    conditions: List[SearchCondition]
    start_year: Optional[str] = None
    end_year: Optional[str] = None
    source_categories: Optional[List[str]] = None
    page_size: int = 50
    max_pages: int = 0
    min_sleep: float = 1.2
    max_sleep: float = 2.2


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="CNKI 抓取与检索系统", version="1.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> HTMLResponse:
    index_html = STATIC_DIR / "index.html"
    if not index_html.exists():
        return HTMLResponse("<h1>static/index.html 未生成</h1>", status_code=500)
    return HTMLResponse(index_html.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/journals")
async def api_journals() -> Dict[str, Any]:
    records = storage.load_all_records()
    return {
        "defaults": storage.DEFAULT_JOURNALS,
        "in_records": storage.list_journals(records),
        "total_records": len(records),
    }


@app.get("/api/records")
async def api_records(
    keyword: Optional[str] = None,
    journal: Optional[str] = None,
    author: Optional[str] = None,
    year: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    records = storage.load_all_records()
    page_records, total = storage.search_records(
        records,
        keyword=keyword,
        journal=journal,
        author=author,
        year=year,
        page=page,
        page_size=page_size,
    )
    return {
        "items": page_records,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/records/stats")
async def api_records_stats() -> Dict[str, Any]:
    records = storage.load_all_records()
    return storage.stats_aggregate(records)


@app.post("/api/records/import")
async def api_records_import(file: UploadFile = File(...)) -> Dict[str, Any]:
    """导入知网官网导出的题录文件（EndNote / Refworks / 自定义格式）。"""
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文件为空或无法解析")
    result = import_records(text, source="官网导入")
    if result["total_in_file"] == 0:
        raise HTTPException(
            status_code=400,
            detail="未识别到任何题录，请确认导出格式为 EndNote / Refworks / 自定义",
        )
    return {"ok": True, **result}


# ===================== 高级检索（CNKI 实时） =============================
@app.get("/api/search/fields")
async def api_search_fields() -> Dict[str, Any]:
    """返回检索字段与来源类别元数据，供前端渲染表单。"""
    return {
        "fields": [{"code": c, "title": t, "placeholder": p} for c, t, p in FIELD_META],
        "source_categories": [{"code": c, "title": t} for c, t in SOURCE_CATEGORIES],
        "logic": [{"code": 0, "title": "AND"}, {"code": 1, "title": "OR"}, {"code": 2, "title": "NOT"}],
    }


@app.post("/api/search")
async def api_search(body: SearchIn) -> Dict[str, Any]:
    """单页实时检索 CNKI（不写盘）。返回 grid 行。

    CNKI 翻页必须带前一页返回的 turnpage 令牌，所以按查询签名缓存
    session + turnpage 链；请求第 N 页时若前面页未抓过，会先顺序抓
    1..N-1 把 turnpage 链补齐，再抓第 N 页。
    """
    if not storage.COOKIE_FILE.exists():
        raise HTTPException(status_code=400, detail="Cookie 不存在，请先上传")
    conditions = [c.model_dump() for c in body.conditions]
    if not any((c.get("value") or "").strip() for c in conditions):
        raise HTTPException(status_code=400, detail="请至少填写一个检索条件")
    # CNKI grid 对 pageSize 有约束（5/10 等会『参数校验失败』），只允许 20/50
    if body.page_size not in (20, 50):
        body.page_size = 20
    try:
        entry = search_cache.get(body)
        session = entry["session"]
        query = entry["query"]
        aside = entry["aside"]
        search_from = entry["search_from"]
        turnpage_to_call: Dict[int, str] = entry["turnpage_to_call"]
        rows_cache: Dict[int, list] = entry["rows_cache"]

        target = body.page
        if target not in rows_cache:
            # 从已缓存的最高页向后顺序补齐到 target，沿途缓存 turnpage 链
            start = max((p for p in rows_cache if p < target), default=0)
            cur = start
            while cur < target:
                nxt = cur + 1
                tp = turnpage_to_call.get(nxt, "")
                total, rows, next_tp = search_grid(
                    session, query, aside, search_from, nxt, body.page_size,
                    turnpage=tp, log=search_log,
                )
                rows_cache[nxt] = rows
                turnpage_to_call[nxt + 1] = next_tp
                if nxt == 1:
                    entry["total"] = total
                cur = nxt
        rows = rows_cache[target]
    except BlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{exc!r}")
    total = entry["total"]
    pages = (total + body.page_size - 1) // body.page_size if total else 0
    return {
        "total": total,
        "pages": pages,
        "page": body.page,
        "page_size": body.page_size,
        "items": rows,
        "aside": aside,
    }


@app.post("/api/search/collect")
async def api_search_collect(body: SearchCollectIn) -> Dict[str, Any]:
    """启动后台任务：按检索条件分页抓取全部结果并合并到 records。"""
    if search_collector.is_running():
        raise HTTPException(status_code=409, detail="检索采集任务正在运行")
    conditions = [c.model_dump() for c in body.conditions]
    if not any((c.get("value") or "").strip() for c in conditions):
        raise HTTPException(status_code=400, detail="请至少填写一个检索条件")
    try:
        params = SearchCollectParams(
            conditions=conditions,
            start_year=body.start_year,
            end_year=body.end_year,
            source_categories=body.source_categories,
            page_size=body.page_size,
            max_pages=body.max_pages,
            min_sleep=body.min_sleep,
            max_sleep=body.max_sleep,
        )
        search_collector.start(params)
    except BlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "params": body.model_dump()}


@app.post("/api/search/collect/stop")
async def api_search_collect_stop() -> Dict[str, Any]:
    search_collector.stop()
    return {"ok": True}


@app.get("/api/search/collect/status")
async def api_search_collect_status() -> Dict[str, Any]:
    s = search_collector.status
    return {
        "running": s.running,
        "stopped": s.stopped,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "current_page": s.current_page,
        "total_pages": s.total_pages,
        "records_written_now": s.records_written_now,
        "last_error": s.last_error,
    }


@app.get("/api/search/collect/logs")
async def api_search_collect_logs() -> StreamingResponse:
    q = search_broker.subscribe()

    async def event_stream():
        try:
            while True:
                if not search_collector.is_running() and q.empty():
                    yield f"data: {json.dumps('[done] 采集结束', ensure_ascii=False)}\n\n"
                    break
                try:
                    msg = await asyncio.to_thread(q.get, timeout=15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            search_broker.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/cookie")
async def api_cookie_get() -> Dict[str, Any]:
    return storage.cookie_status()


@app.post("/api/cookie")
async def api_cookie_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    text = (await file.read()).decode("utf-8", errors="replace")
    try:
        count = storage.save_cookie_upload(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    search_cache.invalidate_all()  # 旧 session 的 cookie 已失效
    return {"ok": True, "count": count, **storage.cookie_status()}


@app.delete("/api/cookie")
async def api_cookie_delete() -> Dict[str, Any]:
    if storage.COOKIE_FILE.exists():
        storage.COOKIE_FILE.unlink()
    search_cache.invalidate_all()
    return storage.cookie_status()


@app.get("/api/scrape/state")
async def api_scrape_state() -> Dict[str, Any]:
    return storage.load_state()


@app.get("/api/scrape/summary")
async def api_scrape_summary() -> Dict[str, Any]:
    return storage.load_summary()


@app.get("/api/scrape/status")
async def api_scrape_status() -> Dict[str, Any]:
    s = scraper.status
    return {
        "running": s.running,
        "stopped": s.stopped,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "current_journal": s.current_journal,
        "current_page": s.current_page,
        "total_pages": s.total_pages,
        "records_written_now": s.records_written_now,
        "journal_counts": s.journal_counts,
        "last_error": s.last_error,
    }


@app.post("/api/scrape/start")
async def api_scrape_start(body: ScrapeStartIn) -> Dict[str, Any]:
    if scraper.is_running():
        raise HTTPException(status_code=409, detail="抓取任务正在运行")
    try:
        params = ScrapeParams(
            journals=body.journals or list(storage.DEFAULT_JOURNALS),
            page_size=body.page_size,
            max_pages=body.max_pages,
            min_sleep=body.min_sleep,
            max_sleep=body.max_sleep,
            start_year=body.start_year,
            end_year=body.end_year,
            fresh=body.fresh,
        )
        scraper.start(params)
    except BlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "params": body.model_dump()}


@app.post("/api/scrape/stop")
async def api_scrape_stop() -> Dict[str, Any]:
    scraper.stop()
    return {"ok": True}


@app.get("/api/scrape/logs")
async def api_scrape_logs() -> StreamingResponse:
    q = scrape_broker.subscribe()

    async def event_stream():
        try:
            while True:
                # 任务结束后排空剩余消息即退出
                if not scraper.is_running() and q.empty():
                    yield f"data: {json.dumps('[done] 任务结束', ensure_ascii=False)}\n\n"
                    break
                try:
                    msg = await asyncio.to_thread(q.get, timeout=15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except Exception:
                    # 队列空且 15s 内无新消息：发心跳保持连接
                    yield ": ping\n\n"
        finally:
            scrape_broker.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/scrape/logs/history")
async def api_scrape_logs_history() -> Dict[str, Any]:
    return {"lines": scrape_broker.history()}


@app.post("/api/pdf/start")
async def api_pdf_start(body: PdfStartIn) -> Dict[str, Any]:
    if pdf_downloader.is_running():
        raise HTTPException(status_code=409, detail="PDF 下载任务正在运行")
    records = storage.load_all_records()
    if body.indices:
        # 索引必须在范围内
        selected = [records[i] for i in body.indices if 0 <= i < len(records)]
    else:
        selected, _ = storage.search_records(
            records,
            keyword=body.keyword,
            journal=body.journal,
            author=body.author,
            year=body.year,
            page=1,
            page_size=len(records),
        )
    if not selected:
        raise HTTPException(status_code=400, detail="没有匹配的记录可供下载")
    try:
        params = DownloadParams(
            records=selected,
            overwrite=body.overwrite,
            min_sleep=body.min_sleep,
            max_sleep=body.max_sleep,
        )
        pdf_downloader.start(params)
    except BlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "selected": len(selected)}


@app.post("/api/pdf/stop")
async def api_pdf_stop() -> Dict[str, Any]:
    pdf_downloader.stop()
    return {"ok": True}


@app.get("/api/pdf/status")
async def api_pdf_status() -> Dict[str, Any]:
    s = pdf_downloader.status
    return {
        "running": s.running,
        "stopped": s.stopped,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "total": s.total,
        "done": s.done,
        "skipped": s.skipped,
        "failed": s.failed,
        "current_title": s.current_title,
        "failures": s.failures[-20:],
        "last_error": s.last_error,
    }


@app.get("/api/pdf/logs")
async def api_pdf_logs() -> StreamingResponse:
    q = pdf_broker.subscribe()

    async def event_stream():
        try:
            while True:
                if not pdf_downloader.is_running() and q.empty():
                    yield f"data: {json.dumps('[done] 下载结束', ensure_ascii=False)}\n\n"
                    break
                try:
                    msg = await asyncio.to_thread(q.get, timeout=15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            pdf_broker.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/pdf/files")
async def api_pdf_files() -> Dict[str, Any]:
    return {"items": storage.list_pdfs()}


@app.get("/api/pdf/files/{name}")
async def api_pdf_file(name: str) -> FileResponse:
    # 安全：禁止路径穿越
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    target = storage.PDF_DIR / name
    if not target.exists() or target.suffix.lower() not in (".pdf", ".caj"):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(target), filename=name)


def run() -> None:
    """uvicorn 启动入口（python -m backend.main 也行）。"""
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
