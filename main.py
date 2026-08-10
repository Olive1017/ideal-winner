"""程序入口。

不带参数直接双击 / 运行 → 启动图形界面。

命令行模式主要用于排查问题和无人值守执行：

    python main.py                                        # 启动界面
    python main.py ui --minimized                         # 启动后直接进托盘（开机自启用的就是这个）
    python main.py convert 订单.xlsx -c 车型.xlsx        # 转换并放进待上传队列
    python main.py upload                                 # 立即上传队列里最新的文件
    python main.py config --username 13100000000          # 修改配置
    python main.py password                               # 交互式录入密码（存入凭据管理器）
"""

from __future__ import annotations

import argparse
import getpass
import sys

from core.converter import convert, export
from core.models import ConvertError, UploadStatus
from services import credentials
from services.config import UPLOAD_FILENAME, Config, pending_dir
from services.logger import setup
from services.scheduler import run_upload_once
from services.single_instance import ensure_single_instance


def cmd_ui(args: argparse.Namespace) -> int:
    # 两个实例同时到点会同时开浏览器，同一个 SDCC 账号会互相踢下线
    if not ensure_single_instance():
        print("程序已在运行，看一下系统托盘")
        return 1

    # 延迟导入：命令行模式下不应该因为没装 PySide6 就跑不了
    try:
        from ui.main_window import run_app
    except ImportError as exc:
        print(f"界面依赖没装全：{exc}")
        print("请执行：pip install -r requirements.txt")
        return 1

    return run_app(start_minimized=args.minimized)


def cmd_convert(args: argparse.Namespace) -> int:
    try:
        result = convert(args.shell_file, args.car_file)
    except ConvertError as exc:
        print(f"转换失败：{exc}")
        return 1

    target = pending_dir() / UPLOAD_FILENAME
    export(result.df, target)

    print(result.summary())
    for warning in result.warnings:
        print(f"[警告] {warning}")
    print(f"已写入待上传队列：{target}")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    def progress(step: str, status: str, message: str) -> None:
        print(f"  [{step}/{status}] {message}")

    result = run_upload_once(file_path=args.file, progress=progress)
    print(f"\n{result.status.value}: {result.message}")
    if result.detail:
        print(f"详情：{result.detail}")
    return 0 if result.status is not UploadStatus.FAILED else 1


def cmd_config(args: argparse.Namespace) -> int:
    config = Config.load()
    changed = False
    for key in ("username", "project", "template", "login_url", "schedule_time"):
        value = getattr(args, key, None)
        if value is not None:
            setattr(config, key, value)
            changed = True
    if args.headless is not None:
        config.headless = args.headless
        changed = True
    if args.auto is not None:
        config.auto_upload_enabled = args.auto
        changed = True
    if changed:
        config.save()
        print(f"已保存：{Config.path()}")

    print(f"账号：{config.username or '(未设置)'}")
    print(f"项目：{config.project}")
    print(f"模板：{config.template}")
    print(f"每日上传时间：{config.schedule_time}（自动上传：{'开' if config.auto_upload_enabled else '关'}）")
    print(f"密码：{'已保存' if credentials.has_password(config.username) else '未保存'}")
    return 0


def cmd_password(args: argparse.Namespace) -> int:
    config = Config.load()
    username = args.username or config.username
    if not username:
        print("请先设置账号：python main.py config --username 你的账号")
        return 1
    if not credentials.available():
        print("未安装 keyring，无法安全保存密码：pip install keyring")
        return 1

    password = getpass.getpass(f"请输入 {username} 的 SDCC 密码（输入不回显）：")
    if not password:
        print("密码为空，已取消")
        return 1

    if credentials.set_password(username, password):
        print("密码已存入 Windows 凭据管理器")
        return 0
    print("密码保存失败")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shell-convert", description="壳牌订单转换与 SDCC 自动上传")
    sub = parser.add_subparsers(dest="command")

    p_ui = sub.add_parser("ui", help="启动图形界面（默认）")
    p_ui.add_argument("--minimized", action="store_true", help="启动后直接进托盘，不弹窗口")
    p_ui.set_defaults(func=cmd_ui)

    p_convert = sub.add_parser("convert", help="转换并放入待上传队列")
    p_convert.add_argument("shell_file", help="壳牌订单 Excel")
    p_convert.add_argument("-c", "--car-file", default=None, help="车型映射 Excel")
    p_convert.set_defaults(func=cmd_convert)

    p_upload = sub.add_parser("upload", help="立即执行一次上传")
    p_upload.add_argument("-f", "--file", default=None, help="指定文件，默认取队列里最新的")
    p_upload.set_defaults(func=cmd_upload)

    p_config = sub.add_parser("config", help="查看或修改配置")
    p_config.add_argument("--username")
    p_config.add_argument("--project")
    p_config.add_argument("--template")
    p_config.add_argument("--login-url", dest="login_url")
    p_config.add_argument("--schedule-time", dest="schedule_time", help="格式 HH:MM")
    p_config.add_argument("--headless", dest="headless", action="store_true", default=None)
    p_config.add_argument("--no-headless", dest="headless", action="store_false")
    p_config.add_argument("--auto", dest="auto", action="store_true", default=None, help="开启自动上传")
    p_config.add_argument("--no-auto", dest="auto", action="store_false")
    p_config.set_defaults(func=cmd_config)

    p_password = sub.add_parser("password", help="录入 SDCC 密码")
    p_password.add_argument("-u", "--username", default=None)
    p_password.set_defaults(func=cmd_password)

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 双击 exe 时没有参数；开机自启只传 --minimized。两种都补上 ui 子命令
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "ui")

    args = build_parser().parse_args(argv)

    # 界面模式下没有控制台，写 stdout 反而会在打包后报错
    setup(console=args.command != "ui")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
