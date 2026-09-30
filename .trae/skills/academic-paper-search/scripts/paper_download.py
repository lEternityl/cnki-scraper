#!/usr/bin/env python3
"""学术论文搜索 skill — PDF 下载（含 CAJ 自动转标准 PDF）。

用法:
  paper_download.py --input /tmp/result.json --sel 1,3-5 [--out ~/Downloads/papers]
  paper_download.py --input /tmp/result.json --all

input 为 paper_search.py --save 生成的 JSON；sel 为结果序号（支持区间）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cookie_manager import load_cookie, make_session  # noqa: E402
from cnki_core import BlockedError, download_direct, safe_filename  # noqa: E402


def parse_sel(spec: str, n: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return [i for i in out if 1 <= i <= n]


def main() -> int:
    ap = argparse.ArgumentParser(description="按检索结果下载 PDF")
    ap.add_argument("--input", required=True, help="paper_search.py --save 的 JSON")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sel", help="序号选择，如 1,3-5")
    g.add_argument("--all", action="store_true", help="下载当前结果页全部")
    ap.add_argument("--out", default=str(Path.home() / "Downloads" / "papers"),
                    help="保存目录（默认 ~/Downloads/papers）")
    ap.add_argument("--sleep", type=float, default=0.8, help="下载间隔秒数")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = data.get("items", [])
    idx = list(range(1, len(rows) + 1)) if args.all else parse_sel(args.sel, len(rows))
    if not idx:
        print("ERROR 序号超出范围")
        return 2
    targets = [rows[i - 1] for i in idx]

    pairs = load_cookie()
    if not pairs:
        print("MISSING 尚未配置 Cookie，请先执行 cookie_manager.py init")
        return 1
    session = make_session(pairs)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    ok = fail = skip = 0
    print(f"开始下载 {len(targets)} 篇 → {out_dir}")
    for n, r in enumerate(targets, 1):
        title = r.get("篇名", "")
        dl = r.get("下载链接") or ""
        if not dl:
            print(f"[{n}/{len(targets)}] SKIP 无下载链接：{title[:36]}")
            skip += 1
            continue
        base = safe_filename(title, r.get("发表时间", ""))
        try:
            if any((out_dir / (base + ext)).exists() for ext in (".pdf", ".caj")):
                print(f"[{n}/{len(targets)}] EXIST {title[:36]}")
                skip += 1
                continue
            # 直连下载 + CAJ→PDF 解密（cnki_core 自包含实现）；
            # 直连返回 CAJ 时自动尝试详情页『PDF下载』按钮拿原生 PDF
            download_direct(session, dl, base, title, n, len(targets), out_dir,
                            detail_link=r.get("链接", ""))
            f = out_dir / (base + ".pdf")
            if f.exists():
                print(f"[{n}/{len(targets)}] OK {f.name[:52]} ({f.stat().st_size//1024}KB)")
                ok += 1
            else:
                caj = out_dir / (base + ".caj")
                if caj.exists():
                    print(f"[{n}/{len(targets)}] OK(caj) {caj.name[:48]}（知网老文献仅提供 CAJ 格式）")
                    ok += 1
                else:
                    print(f"[{n}/{len(targets)}] FAIL 未生成文件：{title[:36]}")
                    fail += 1
        except BlockedError as exc:
            print(f"INVALID_COOKIE 下载被拦截（{str(exc)[:60]}），请重新导出 Cookie 后重试剩余 {len(targets)-n+1} 篇")
            return 4
        except Exception as exc:
            print(f"[{n}/{len(targets)}] FAIL {title[:36]} — {str(exc)[:90]}")
            fail += 1
        if n < len(targets):
            time.sleep(args.sleep)
    print(f"\n完成：成功 {ok}，已存在/跳过 {skip}，失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
