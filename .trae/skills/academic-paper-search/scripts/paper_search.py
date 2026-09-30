#!/usr/bin/env python3
"""学术论文搜索 skill — 知网总库检索。

用法:
  paper_search.py --kw "大模型 应急管理" [--field SU] [--start-year 2020] [--end-year 2026]
                  [--source-cats CSI,PT] [--dbs 期刊,报纸] [--sort 被引]
                  [--page 1] [--page-size 20] [--save /tmp/result.json]

输出: 终端表格预览 + 可选 JSON（含 下载链接 字段的完整行）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cookie_manager import load_cookie, make_session  # noqa: E402
import cnki_core as scraper  # noqa: E402  自包含核心模块（同目录）

SORT_MAP = {"相关度": "", "时间": "PT", "被引": "CF", "下载": "DFR", "综合": "ZH"}
DB_ALIAS = {n: c for c, n in {
    "YSTT4HG0": "期刊", "LSTPFY1C": "学位论文", "JUP3MUPD": "会议",
    "MPMFIG1A": "报纸", "EMRPGLPA": "图书", "NN3FJMUV": "特色期刊",
    "BLZOG7CK": "科技成果", "WQ0UVIAA": "年鉴", "PWFIRAGL": "标准",
    "NLBO1Z6R": "专利",
}.items()}
DB_ALIAS.update({"学术期刊": "YSTT4HG0", "学位": "LSTPFY1C"})


def main() -> int:
    ap = argparse.ArgumentParser(description="知网总库文献检索")
    ap.add_argument("--kw", required=True, help="检索词")
    ap.add_argument("--field", default="SU",
                    help="SU 主题/TI 篇名/AB 摘要/KY 关键词/AU 作者（默认 SU）")
    ap.add_argument("--start-year")
    ap.add_argument("--end-year")
    ap.add_argument("--source-cats", help="来源类别代码，逗号分隔：NCPCJ/PT/CSI/CSD/EI")
    ap.add_argument("--dbs", help="数据库多选（中文别名），默认总库全部")
    ap.add_argument("--sort", default="相关度",
                    choices=list(SORT_MAP), help="排序方式")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--page-size", type=int, default=20)
    ap.add_argument("--save", help="结果保存为 JSON（供 paper_download.py 使用）")
    args = ap.parse_args()

    pairs = load_cookie()
    if not pairs:
        print("MISSING 尚未配置 Cookie，请先执行 cookie_manager.py init（见 references/cookie-guide.md）")
        return 1
    session = make_session(pairs)
    # CNKI grid 对 pageSize 有约束（5/10 等非法值会静默返回 0 条），只允许 20/50
    if args.page_size not in (20, 50):
        args.page_size = 20

    sub_dbs = None
    if args.dbs:
        sub_dbs = [DB_ALIAS.get(x.strip(), x.strip()) for x in args.dbs.split(",")]
        sub_dbs = [c for c in sub_dbs if c in scraper.CROSSDB_CODES]
        if not sub_dbs:
            print("ERROR --dbs 无法识别，可用：期刊/学位论文/会议/报纸/图书/特色期刊/科技成果/年鉴/标准/专利")
            return 2
    src_cats = [x.strip() for x in args.source_cats.split(",")] if args.source_cats else None

    query = scraper.build_query_json(
        [{"field": args.field, "value": args.kw, "logic": 0}],
        args.start_year, args.end_year, src_cats, sub_dbs,
    )
    try:
        total, rows, _tp = scraper.search_grid(
            session, query, f"(检索：{args.kw})", "资源范围：总库",
            args.page, args.page_size,
            sort_field=SORT_MAP[args.sort], sort_type="DESC",
            log=lambda _m: None,
        )
    except Exception as exc:
        msg = str(exc)
        if "验证" in msg or "blocked" in msg.lower() or "elib" in msg:
            print("INVALID_COOKIE 检索被服务端拦截，Cookie 已失效或被风控，请重新导出（references/cookie-guide.md）")
        else:
            print(f"ERROR 网络或接口异常（已自动重试 3 次）：{msg[:200]}")
        return 3

    # 终端预览
    print(f"共 {total} 条 | 第 {args.page} 页 | 排序 {args.sort} | 数据库 {args.dbs or '总库'}")
    print("-" * 118)
    print(f"{'#':<3} {'题名':<44} {'来源':<20} {'时间':<11} {'数据库':<7} {'被引':>4} {'下载':>6}")
    for i, r in enumerate(rows, 1):
        print(f"{i:<3} {r['篇名'][:42]:<44} {(r.get('刊名') or '')[:18]:<20} "
              f"{(r.get('发表时间') or '')[:10]:<11} {(r.get('数据库') or ''):<7} "
              f"{r.get('被引') or '-':>4} {r.get('下载') or '-':>6}")
    if not rows:
        print("（无结果——建议放宽年份/来源/数据库筛选，或更换检索词）")

    if args.save:
        payload = {"total": total, "page": args.page, "page_size": args.page_size,
                   "items": rows}
        Path(args.save).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nSAVED {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
