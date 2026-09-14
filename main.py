
from __future__ import annotations

import sys

from services.logger import setup
from services.single_instance import ensure_single_instance
from ui.main_window import run_app

def main() -> int:
    start_minimized = "--start-minimized" in sys.argv

    # 两个实例同时到点会同时开浏览器，同一个 SDCC 账号会互相踢下线
    if not ensure_single_instance():
        print("程序已在运行，看一下系统托盘")
        return 1

    # 打包后没有控制台，往 stdout 写会直接报错，所以固定关掉
    setup(console=False)

    return run_app(start_minimized=start_minimized)


if __name__ == "__main__":
    sys.exit(main())
