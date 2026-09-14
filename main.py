#!/usr/bin/env python3
"""X Account Manager launcher.

The browser-cookie cleaner deliberately lives in its own module and never opens a
browser.  Keeping this tiny launcher separate makes it possible to add official
OAuth login later without mixing the two credential types.
"""

from __future__ import annotations

import asyncio
import sys

from cookie_cleaner import run_cookie_cleaner


def _banner() -> None:
    print("\n╭────────────────────────────────────────────╮")
    print("│              X 账号本地管理器              │")
    print("│       纯请求运行 · 不启动浏览器 · 本地处理 │")
    print("╰────────────────────────────────────────────╯")


async def app() -> int:
    _banner()
    await run_cookie_cleaner()
    print('已退出。')
    return 0


def main() -> int:
    try:
        return asyncio.run(app())
    except KeyboardInterrupt:
        print("\n已安全停止；未开始的操作不会执行。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
