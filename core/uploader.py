"""SDCC TMS 订单导入 RPA（Playwright）。

流程与人工操作一致：
    登录 → 我的工作台 → 订单中心 → 订单管理 → 导入 → 导入订单
    → 选项目 → 选模板 → 上传文件 → 读取上传结果

所有异常都归一成 ``UploadError`` 并带上 ``ErrorKind``，由调度器决定是否重试。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .models import ErrorKind, UploadError

PathLike = Union[str, Path]
ProgressCallback = Optional[Callable[[str, str, str], None]]

# 上传结果文案判定
SUCCESS_PATTERN = re.compile(r"成功|完成")
FAILURE_PATTERN = re.compile(r"失败|错误|异常|不存在|不合法|校验不通过|无效")

# 出现这些字样说明是账号/密码问题，重试没有意义
AUTH_FAILURE_PATTERN = re.compile(r"密码|账号|帐号|用户名|验证码|锁定|冻结|不存在")

# 弹窗里的固定文案，读取结果时要排除掉
STATIC_DIALOG_TEXT = {
    "导入 - 订单",
    "1.上传准备",
    "2.录入数据",
    "3.上传文件",
    "4.上传结果",
    "上传准备",
    "录入数据",
    "上传文件",
    "上传结果",
    "点击上传",
    "点击下载导入模板",
    "请将你的信息填入模板后保存，点击下方按钮上传",
    "返回",
}

DROPDOWN_TIMEOUT_MS = 8_000
POLL_INTERVAL_MS = 1_000


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _capture_screenshot(page, screenshot_dir: Optional[Path], run_id: str) -> Optional[str]:
    """出错时截图，方便事后定位卡在哪一步。"""
    if screenshot_dir is None or page is None:
        return None
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        target = screenshot_dir / f"{stamp}_error.png"
        page.screenshot(path=str(target), full_page=True)
        return str(target)
    except Exception:  # noqa: BLE001 - 截图失败不能影响主错误上报
        return None


def _launch_browser(playwright, config, logger):
    """优先 Chrome，起不来就降级 Edge，都没有才报错。

    不打包 Chromium，是为了把分发给同事的 exe 控制在合理体积。
    """
    channels = list(config.browser_channels or ["chrome"])
    last_error: Optional[Exception] = None
    for channel in channels:
        try:
            browser = playwright.chromium.launch(
                channel=channel, headless=bool(config.headless)
            )
            logger.info("browser", f"已启动浏览器：{channel}")
            return browser
        except PlaywrightError as exc:
            last_error = exc
            logger.warn("browser", f"{channel} 启动失败，尝试下一个：{exc}")
    raise UploadError(
        "未找到可用的 Chrome 或 Edge 浏览器，请确认本机已安装其中之一",
        ErrorKind.FATAL,
        str(last_error) if last_error else None,
    )


def _read_login_error(frame) -> str:
    """尽力从登录 iframe 里抓出错误提示文案。"""
    for selector in (".el-message--error", ".el-form-item__error", ".error-tip", "[class*='error']"):
        try:
            locator = frame.locator(selector).first
            if locator.count() == 0:
                continue
            text = (locator.inner_text() or "").strip()
            if text:
                return text
        except PlaywrightError:
            continue
    return ""


def _extract_result_text(dialog_text: str) -> str:
    """从弹窗全文里剔除固定文案，剩下的就是 SDCC 返回的结果。

    比起猜 DOM 结构，这种做法对页面改版更耐受。
    """
    lines = [line.strip() for line in (dialog_text or "").splitlines()]
    return " ".join(line for line in lines if line and line not in STATIC_DIALOG_TEXT)


# --------------------------------------------------------------------------- #
# 各步骤
# --------------------------------------------------------------------------- #


def _login(page, config, password: str, logger) -> None:
    logger.start("login", "打开 SDCC 登录页")
    page.goto(config.login_url, wait_until="domcontentloaded")

    with page.expect_popup(timeout=config.timeout_ms) as popup_info:
        page.get_by_role("button", name="登录").first.click()
    login_page = popup_info.value
    login_page.wait_for_load_state("domcontentloaded")

    frame = login_page.frame_locator("#iam_iframe_sdk")
    frame.get_by_placeholder("用户编号/手机号/邮箱").fill(config.username)
    frame.get_by_placeholder("密码").fill(password)
    frame.get_by_role("button", name="确定").click()

    # 登录成功后弹窗会自动关闭；失败则停在原地并给出提示
    try:
        login_page.wait_for_event("close", timeout=config.timeout_ms)
    except PlaywrightTimeout as exc:
        detail = _read_login_error(frame) or "登录后未跳转回主页面"
        kind = ErrorKind.FATAL if AUTH_FAILURE_PATTERN.search(detail) else ErrorKind.RETRYABLE
        raise UploadError(f"登录失败：{detail}", kind) from exc

    page.wait_for_load_state("networkidle")
    logger.success("login", f"账号 {config.username} 登录成功")


def _open_import_dialog(page, config, logger):
    logger.start("navigate", "进入订单管理")
    page.get_by_role("button", name="我的工作台").first.click()
    page.get_by_role("menuitem", name=re.compile("订单中心")).locator("div").first.click()
    page.get_by_text("订单管理", exact=True).first.click()
    page.get_by_role("tab", name="订单管理").first.wait_for(
        state="visible", timeout=config.timeout_ms
    )
    logger.success("navigate", "已进入订单管理")

    logger.start("dialog", "打开导入订单弹窗")
    page.get_by_role("button", name=re.compile(r"^导入")).first.click()
    page.get_by_text("导入订单", exact=True).first.click()

    dialog = page.get_by_role("dialog").filter(has_text="导入 - 订单").first
    dialog.wait_for(state="visible", timeout=config.timeout_ms)
    logger.success("dialog", "导入弹窗已打开")
    return dialog


def _select_option(page, dialog, index: int, keyword: str, field_name: str, logger) -> None:
    """输入关键词 → 等下拉出现 → 点击列表项。

    不用「填完直接回车」，因为下拉是异步加载的，回车经常选不中。
    """
    logger.start("select", f"选择{field_name}：{keyword}")
    box = dialog.get_by_placeholder("请输入关键字选择").nth(index)
    box.click()
    box.fill(keyword)

    option = page.get_by_role("option").filter(has_text=keyword).first
    try:
        option.wait_for(state="visible", timeout=DROPDOWN_TIMEOUT_MS)
    except PlaywrightTimeout:
        option = page.locator("li").filter(has_text=keyword).first
        try:
            option.wait_for(state="visible", timeout=DROPDOWN_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            # 选项压根不存在，多半是配置里的名称写错了，重试无意义
            raise UploadError(
                f"下拉列表里找不到{field_name}「{keyword}」，请到设置里核对名称",
                ErrorKind.FATAL,
            ) from exc

    option.click()
    logger.success("select", f"{field_name}已选择：{keyword}")


def _upload_and_wait(page, dialog, file_path: Path, config, logger) -> str:
    logger.start("upload", f"上传文件：{file_path.name}")

    with page.expect_file_chooser(timeout=config.timeout_ms) as chooser_info:
        dialog.get_by_role("button", name="点击上传").first.click()
    chooser_info.value.set_files(str(file_path))

    deadline = time.monotonic() + config.result_timeout_ms / 1000
    result_text = ""
    while time.monotonic() < deadline:
        try:
            result_text = _extract_result_text(dialog.inner_text())
        except PlaywrightError:
            result_text = ""
        if result_text and (
            SUCCESS_PATTERN.search(result_text) or FAILURE_PATTERN.search(result_text)
        ):
            break
        page.wait_for_timeout(POLL_INTERVAL_MS)

    if not result_text:
        raise UploadError(
            f"{config.result_timeout_ms / 1000:.0f} 秒内没有拿到上传结果，"
            "请到 SDCC「导入结果」页人工确认后再决定是否重传",
            ErrorKind.RETRYABLE,
        )

    has_success = bool(SUCCESS_PATTERN.search(result_text))
    has_failure = bool(FAILURE_PATTERN.search(result_text))

    if has_failure and not has_success:
        raise UploadError(f"SDCC 返回上传失败：{result_text}", ErrorKind.FATAL, result_text)

    if has_failure and has_success:
        # 例如「成功 8 条，失败 2 条」——不能整批重传，只能提示人工处理
        logger.warn("upload", f"上传部分失败，需人工核对：{result_text}")
    else:
        logger.success("upload", f"上传结果：{result_text}")

    return result_text


def _close_dialog(dialog, logger) -> None:
    try:
        dialog.get_by_role("button", name="返回").first.click(timeout=5_000)
    except PlaywrightError:
        logger.info("dialog", "关闭弹窗失败，忽略")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def upload_file(
    file_path: PathLike,
    config,
    password: str,
    logger,
    screenshot_dir: Optional[PathLike] = None,
    progress: ProgressCallback = None,
) -> str:
    """把一个 Excel 上传到 SDCC，返回上传结果文案。

    Args:
        file_path: 待上传的 Excel。
        config: services.config.Config 实例。
        password: 从 keyring 取出的密码，绝不落盘。
        logger: services.logger.RunLogger 实例。
        screenshot_dir: 出错时截图存放目录。
        progress: 可选回调 ``(step, status, message)``，UI 用来实时显示进度。

    Raises:
        UploadError: 任何失败都归一成它，带 ErrorKind 供调度器判断重试。
    """
    path = Path(file_path)
    if not path.exists():
        raise UploadError(f"待上传文件不存在：{path}", ErrorKind.FATAL)

    shot_dir = Path(screenshot_dir) if screenshot_dir else None
    run_id = getattr(logger, "run_id", "")

    def report(step: str, status: str, message: str = "") -> None:
        if progress is None:
            return
        try:
            progress(step, status, message)
        except Exception:  # noqa: BLE001 - UI 回调不能影响主流程
            pass

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, config, logger)
        context = browser.new_context(accept_downloads=False)
        context.set_default_timeout(config.timeout_ms)
        page = context.new_page()

        try:
            report("login", "start", "正在登录 SDCC")
            _login(page, config, password, logger)
            report("login", "success", "登录成功")

            report("navigate", "start", "正在进入订单管理")
            dialog = _open_import_dialog(page, config, logger)
            report("navigate", "success", "导入弹窗已打开")

            report("select", "start", "正在选择项目和模板")
            _select_option(page, dialog, 0, config.project, "项目", logger)
            _select_option(page, dialog, 1, config.template, "模板", logger)
            report("select", "success", "项目和模板已选择")

            report("upload", "start", f"正在上传 {path.name}")
            result_text = _upload_and_wait(page, dialog, path, config, logger)
            report("upload", "success", result_text)

            _close_dialog(dialog, logger)
            return result_text

        except UploadError as exc:
            if not exc.detail:
                exc.detail = _capture_screenshot(page, shot_dir, run_id)
            report("upload", "fail", exc.message)
            raise
        except PlaywrightTimeout as exc:
            shot = _capture_screenshot(page, shot_dir, run_id)
            report("upload", "fail", "页面操作超时")
            raise UploadError(f"页面操作超时：{exc}", ErrorKind.RETRYABLE, shot) from exc
        except PlaywrightError as exc:
            shot = _capture_screenshot(page, shot_dir, run_id)
            report("upload", "fail", "浏览器操作失败")
            raise UploadError(f"浏览器操作失败：{exc}", ErrorKind.RETRYABLE, shot) from exc
        finally:
            try:
                context.close()
            finally:
                browser.close()
