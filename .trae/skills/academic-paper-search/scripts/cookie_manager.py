#!/usr/bin/env python3
"""学术论文搜索 skill — Cookie 加密管理。

用法:
  cookie_manager.py init --file /tmp/cookie.txt   # 录入并加密保存+验证
  cookie_manager.py check                          # 检测有效性
  cookie_manager.py clear                          # 清除存储

存储: ~/.academic-paper-search/cookie.enc (Fernet 加密) + key.bin (0600)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cnki_core as scraper  # noqa: E402  自包含核心模块（同目录）

STORE_DIR = Path.home() / ".academic-paper-search"
ENC_FILE = STORE_DIR / "cookie.enc"
KEY_FILE = STORE_DIR / "key.bin"

BASE = scraper.BASE


def _load_crypto():
    from cryptography.fernet import Fernet

    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        os.chmod(KEY_FILE, 0o600)
    else:
        os.chmod(KEY_FILE, 0o600)
    return Fernet(KEY_FILE.read_bytes())


def parse_cookie_text(text: str) -> dict[str, str]:
    """支持两种格式：'name=value; name2=v2' 串；scrapy JSON 数组。"""
    text = text.strip()
    if text.startswith("["):
        arr = json.loads(text)
        return {c["name"]: c["value"] for c in arr if c.get("name")}
    pairs: dict[str, str] = {}
    for part in re.split(r"[;\n]", text):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                pairs[k] = v
    return pairs


def save_cookie(pairs: dict[str, str]) -> None:
    f = _load_crypto()
    ENC_FILE.write_bytes(f.encrypt(json.dumps(pairs).encode("utf-8")))
    os.chmod(ENC_FILE, 0o600)


def load_cookie() -> dict[str, str] | None:
    if not ENC_FILE.exists():
        return None
    f = _load_crypto()
    return json.loads(f.decrypt(ENC_FILE.read_bytes()).decode("utf-8"))


def make_session(pairs: dict[str, str]):
    import requests

    s = requests.Session()
    s.trust_env = False  # 绕过系统代理（如本机 Clash），直连 sclib 代理镜像
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json,text/plain,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": BASE,
        "Referer": f"{BASE}/kns8s/AdvSearch",
        "X-Requested-With": "XMLHttpRequest",
    })
    # 与 backend.scraper.load_session 一致：cookie 绑定 sclib 域（实测必需）
    for k, v in pairs.items():
        s.cookies.set(k, v, domain=".share.sclib.cn", path="/")
    return s


def check_pairs(pairs: dict[str, str]) -> tuple[bool, str]:
    """发一次最小检索验证 Cookie 是否有效（以实测为准）。"""
    if not pairs:
        return False, "Cookie 为空"
    session = make_session(pairs)
    try:
        query = scraper.build_query_json(
            [{"field": "SU", "value": "应急管理"}], "2020", "2026", None, None
        )
        total, _rows, _tp = scraper.search_grid(
            session, query, "(主题：应急管理)", "资源范围：总库", 1, 20,
            log=lambda _m: None,
        )
        if total > 0:
            return True, f"有效（测试检索返回 {total} 条）"
        return False, "接口可通但返回 0 条，Cookie 权限异常"
    except Exception as exc:  # BlockedError / elib 挑战 / 网络错误
        msg = str(exc)
        if "验证" in msg or "blocked" in msg.lower() or "elib" in msg:
            return False, "服务端要求安全验证——Cookie 已失效或被风控，需重新导出"
        return False, f"请求失败：{msg[:120]}"


def cmd_init(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    pairs = parse_cookie_text(text)
    if len(pairs) < 3:
        print("ERROR 解析到的 Cookie 字段过少，请检查格式（见 references/cookie-guide.md）")
        return 2
    ok, reason = check_pairs(pairs)
    if not ok and "请求失败" in reason:
        print(f"ERROR 网络异常无法验证：{reason}")
        return 3
    save_cookie(pairs)
    masked = ", ".join(sorted(pairs)[:5])
    print(f"SAVED {len(pairs)} 个字段（含 {masked}...）已加密存储")
    print("VALID " + reason if ok else "INVALID " + reason)
    return 0 if ok else 1


def cmd_check(_: argparse.Namespace) -> int:
    pairs = load_cookie()
    if not pairs:
        print("MISSING 尚未配置 Cookie，请先按 references/cookie-guide.md 导出并录入")
        return 1
    ok, reason = check_pairs(pairs)
    print(("VALID " if ok else "INVALID ") + reason)
    return 0 if ok else 1


def cmd_clear(_: argparse.Namespace) -> int:
    for f in (ENC_FILE, KEY_FILE):
        f.unlink(missing_ok=True)
    print("CLEARED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="知网 Cookie 加密管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("--file", required=True, help="Cookie 文本文件")
    sub.add_parser("check")
    sub.add_parser("clear")
    args = ap.parse_args()
    return {"init": cmd_init, "check": cmd_check, "clear": cmd_clear}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
