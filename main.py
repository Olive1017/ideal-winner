"""程序入口。

只做三件事：单实例检查、日志初始化、拉起界面。
"""

from __future__ import annotations

import sys

from services.logger import setup
from services.single_instance import ensure_single_instance


def main() -> int:
    # 两个实例同时到点会同时开浏览器，同一个 SDCC 账号会互相踢下线
    if not ensure_single_instance():
        print("程序已在运行，看一下系统托盘")
        return 1

    # 延迟导入：缺 PySide6 时给一句人话，而不是一屏 traceback
    try:
        from ui.main_window import run_app
    except ImportError as exc:
        print(f"界面依赖没装全：{exc}")
        print("请执行：pip install -r requirements.txt")
        return 1

    # 打包后没有控制台，往 stdout 写会直接报错，所以固定关掉
    setup(console=False)

    # 双击 exe 时没有参数；开机自启只传 --minimized
    return run_app(start_minimized="--minimized" in sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
