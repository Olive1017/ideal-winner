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

# IAM 点「登录」后会自动弹出的「登录说明」页，不是业务页面
GUIDE_URL_PATTERN = re.compile(r"loginInfoIframe", re.IGNORECASE)

WORKBENCH_BUTTON = "我的工作台"
IMPORT_BUTTON_SELECTOR = "button.sdccDropBtn"
IMPORT_MENU_ITEM = "导入订单"
PROJECT_PLACEHOLDER = "请输入关键字选择"
TEMPLATE_PLACEHOLDER = "请选择"

OPTION_SELECTOR = ".el-popper li:visible, .el-autocomplete-suggestion li:visible"

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
PAGE_SCAN_INTERVAL_SEC = 0.5

# 人工登录最多等多久
MANUAL_LOGIN_TIMEOUT_MS = 600_000

# 等待登录期间多久播报一次
LOGIN_HEARTBEAT_SEC = 30.0

# 「我的工作台」按钮等待时间
WORKBENCH_WAIT_MS = 3_000

# 导航菜单点击超时
NAV_MENU_TIMEOUT_MS = 10_000


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _capture_screenshot(
    page,
    screenshot_dir: Optional[Path],
    run_id: str,
) -> Optional[str]:
    """出错时截图，方便定位卡在哪一步。"""
    if screenshot_dir is None or page is None:
        return None

    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        stamp = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        target = screenshot_dir / f"{stamp}_error.png"

        page.screenshot(
            path=str(target),
            full_page=True,
        )

        return str(target)

    except Exception:
        return None


def _launch_browser(
    playwright,
    config,
    logger,
    headless: Optional[bool] = None,
):
    channels = list(config.browser_channels or ["chrome"])

    want_headless = (
        bool(config.headless)
        if headless is None
        else bool(headless)
    )

    last_error: Optional[Exception] = None

    for channel in channels:
        try:
            browser = playwright.chromium.launch(
                channel=channel,
                headless=want_headless,
            )

            logger.info(
                "browser",
                f"已启动浏览器：{channel}",
            )

            return browser

        except PlaywrightError as exc:
            last_error = exc

            logger.warn(
                "browser",
                f"{channel} 启动失败，尝试下一个：{exc}",
            )

    raise UploadError(
        "未找到可用的 Chrome 或 Edge 浏览器，请确认本机已安装其中之一",
        ErrorKind.FATAL,
        str(last_error) if last_error else None,
    )


def _page_url(page) -> str:
    """安全地取页面 URL。"""
    try:
        return page.url or ""
    except PlaywrightError:
        return ""


def _live_pages(context) -> list:
    return [
        p
        for p in context.pages
        if not p.is_closed()
    ]


def _open_page_urls(context) -> list:
    return [
        _page_url(p)
        for p in _live_pages(context)
    ]


def _close_guide_pages(context, logger) -> int:
    """关闭 IAM 自动弹出的「登录说明」页。"""

    closed = 0

    for candidate in list(context.pages):
        if candidate.is_closed():
            continue

        if not GUIDE_URL_PATTERN.search(_page_url(candidate)):
            continue

        try:
            candidate.close()
            closed += 1
        except PlaywrightError:
            continue

    if closed:
        logger.info(
            "login",
            f"已关闭「登录说明」页 {closed} 个",
        )

    return closed


def _is_logged_in(page) -> bool:
    """判断当前页面是否已经进入 SDCC 系统。"""

    try:
        return (
            page
            .get_by_role(
                "button",
                name=WORKBENCH_BUTTON,
            )
            .first
            .is_visible()
        )

    except PlaywrightError:
        return False


def _find_logged_in_page(context):
    """
    在所有标签页里找已经登录成功的页面。
    """

    for candidate in _live_pages(context):

        if GUIDE_URL_PATTERN.search(_page_url(candidate)):
            continue

        if _is_logged_in(candidate):
            return candidate

    return None


def _wait_for_login(
    context,
    timeout_ms: int,
    logger,
):
    """
    等待用户手工登录。

    不填写账号。
    不填写密码。
    不点击登录。
    不读取 iframe。

    只负责等待登录成功。
    """

    deadline = (
        time.monotonic()
        + timeout_ms / 1000
    )

    next_beat = (
        time.monotonic()
        + LOGIN_HEARTBEAT_SEC
    )

    while time.monotonic() < deadline:

        _close_guide_pages(
            context,
            logger,
        )

        found = _find_logged_in_page(context)

        if found is not None:
            return found

        now = time.monotonic()

        if now >= next_beat:

            logger.info(
                "login",
                f"仍在等待手工登录……"
                f"剩余约 {(deadline - now) / 60:.0f} 分钟",
            )

            next_beat = (
                now + LOGIN_HEARTBEAT_SEC
            )

        time.sleep(
            PAGE_SCAN_INTERVAL_SEC
        )

    return None


def _extract_result_text(dialog_text: str) -> str:
    """从弹窗全文里剔除固定文案，剩下 SDCC 返回的结果。"""

    lines = [
        line.strip()
        for line in (dialog_text or "").splitlines()
    ]

    return " ".join(
        line
        for line in lines
        if line
        and line not in STATIC_DIALOG_TEXT
    )


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #

def _ensure_logged_in(
    page,
    config,
    logger,
    report,
):
    """
    打开 SDCC，等待用户手工登录。

    每次上传都是全新的浏览器 context。
    不使用 session。
    不自动填写账号密码。
    """

    context = page.context

    logger.start(
        "login",
        "打开 SDCC，等待手工登录",
    )

    page.goto(
        config.login_url,
        wait_until="domcontentloaded",
    )

    _close_guide_pages(
        context,
        logger,
    )

    logger.info(
        "login",
        "请在浏览器中手工完成 SDCC 登录（包括手机验证码）",
    )

    report(
        "login",
        "start",
        "请在浏览器中手工完成 SDCC 登录",
    )

    found = _wait_for_login(
        context,
        MANUAL_LOGIN_TIMEOUT_MS,
        logger,
    )

    if found is None:
        raise UploadError(
            f"{MANUAL_LOGIN_TIMEOUT_MS / 60000:.0f} "
            "分钟内没有检测到登录成功",
            ErrorKind.RETRYABLE,
            "当前标签页："
            + (
                " | ".join(
                    _open_page_urls(context)
                )
                or "无"
            ),
        )

    try:
        found.bring_to_front()
    except PlaywrightError:
        pass

    logger.success(
        "login",
        "手工登录完成，继续上传",
    )

    report(
        "login",
        "success",
        "登录成功，继续上传",
    )

    return found


# --------------------------------------------------------------------------- #
# 导航
# --------------------------------------------------------------------------- #

def _find_import_button(page, config, logger):
    button = (
        page
        .locator(IMPORT_BUTTON_SELECTOR)
        .filter(
            has_text=re.compile(r"导入")
        )
        .first
    )

    try:
        button.wait_for(
            state="visible",
            timeout=config.timeout_ms,
        )

    except PlaywrightTimeout as exc:

        raise UploadError(
            "订单管理页上找不到「导入」按钮",
            ErrorKind.RETRYABLE,
            "页面可能还没加载完，"
            "或者当前账号没有导入权限",
        ) from exc

    return button


def _click_popover_item(
    page,
    reference,
    text: str,
    logger,
) -> None:

    popover_id = (
        reference.get_attribute(
            "aria-describedby"
        )
        or ""
    )

    if not popover_id:
        raise UploadError(
            "「导入」按钮上没有 aria-describedby，"
            "无法定位浮层菜单",
            ErrorKind.RETRYABLE,
        )

    item = (
        page
        .locator(f"#{popover_id}")
        .get_by_text(
            text,
            exact=True,
        )
        .first
    )

    try:
        item.wait_for(
            state="visible",
            timeout=DROPDOWN_TIMEOUT_MS,
        )

    except PlaywrightTimeout as exc:
        raise UploadError(
            f"点开「导入」后没有出现「{text}」菜单项",
            ErrorKind.RETRYABLE,
        ) from exc

    item.click()

    logger.info(
        "dialog",
        f"已在浮层 #{popover_id} 里点击「{text}」",
    )


def _open_import_dialog(
    page,
    config,
    logger,
):
    logger.start(
        "navigate",
        "进入订单管理",
    )

    workbench = (
        page
        .get_by_role(
            "button",
            name=WORKBENCH_BUTTON,
        )
        .first
    )

    try:

        workbench.wait_for(
            state="visible",
            timeout=WORKBENCH_WAIT_MS,
        )

        workbench.click()

        logger.info(
            "navigate",
            "已点开「我的工作台」",
        )

    except PlaywrightError:

        logger.info(
            "navigate",
            "落地页没有「我的工作台」，直接找订单中心",
        )

    page.locator(
        ".el-submenu__title",
        has_text="订单中心",
    ).first.click(
        timeout=NAV_MENU_TIMEOUT_MS
    )

    page.get_by_role(
        "menuitem",
        name="订单管理",
        exact=True,
    ).first.click(
        timeout=NAV_MENU_TIMEOUT_MS
    )

    import_button = _find_import_button(
        page,
        config,
        logger,
    )

    logger.success(
        "navigate",
        "已进入订单管理",
    )

    logger.start(
        "dialog",
        "打开导入订单弹窗",
    )

    import_button.click()

    _click_popover_item(
        page,
        import_button,
        IMPORT_MENU_ITEM,
        logger,
    )

    dialog = (
        page
        .get_by_role("dialog")
        .filter(
            has=page.get_by_placeholder(
                PROJECT_PLACEHOLDER
            )
        )
        .first
    )

    dialog.get_by_placeholder(
        PROJECT_PLACEHOLDER
    ).first.wait_for(
        state="visible",
        timeout=config.timeout_ms,
    )

    logger.success(
        "dialog",
        "导入弹窗已打开",
    )

    return dialog


# --------------------------------------------------------------------------- #
# 下拉选择
# --------------------------------------------------------------------------- #

def _find_dropdown_option(
    page,
    keyword: str,
    field_name: str,
):

    options = page.locator(
        OPTION_SELECTOR
    )

    hit = (
        options
        .filter(has_text=keyword)
        .first
    )

    try:

        hit.wait_for(
            state="visible",
            timeout=DROPDOWN_TIMEOUT_MS,
        )

        return hit

    except PlaywrightTimeout as exc:

        try:
            seen = [
                text.strip()
                for text in options.all_inner_texts()
                if text.strip()
            ]
        except PlaywrightError:
            seen = []

        raise UploadError(
            f"下拉列表里找不到"
            f"{field_name}「{keyword}」；"
            f"当前可见选项："
            f"{seen if seen else '无（浮层没展开）'}",
            ErrorKind.FATAL,
        ) from exc


def _select_option(
    page,
    dialog,
    placeholder: str,
    keyword: str,
    field_name: str,
    config,
    logger,
) -> None:

    logger.start(
        "select",
        f"选择{field_name}：{keyword}",
    )

    box = (
        dialog
        .get_by_placeholder(placeholder)
        .first
    )

    try:

        box.wait_for(
            state="visible",
            timeout=config.timeout_ms,
        )

    except PlaywrightTimeout as exc:

        raise UploadError(
            f"导入弹窗里找不到"
            f"{field_name}下拉框"
            f"（placeholder「{placeholder}」）",
            ErrorKind.FATAL,
        ) from exc

    box.click()

    # el-select 不支持筛选时 input 是 readonly。
    if box.get_attribute("readonly") is None:

        box.fill("")

        try:

            box.press_sequentially(
                keyword,
                delay=60,
            )

        except (
            AttributeError,
            PlaywrightError,
        ):

            box.type(
                keyword,
                delay=60,
            )

    _find_dropdown_option(
        page,
        keyword,
        field_name,
    ).click()

    logger.success(
        "select",
        f"{field_name}已选择：{keyword}",
    )


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def _upload_and_wait(
    page,
    dialog,
    file_path: Path,
    config,
    logger,
) -> str:

    logger.start(
        "upload",
        f"上传文件：{file_path.name}",
    )

    with page.expect_file_chooser(
        timeout=config.timeout_ms
    ) as chooser_info:

        dialog.get_by_role(
            "button",
            name="点击上传",
        ).first.click()

    chooser_info.value.set_files(
        str(file_path)
    )

    deadline = (
        time.monotonic()
        + config.result_timeout_ms / 1000
    )

    result_text = ""

    while time.monotonic() < deadline:

        try:
            result_text = _extract_result_text(
                dialog.inner_text()
            )
        except PlaywrightError:
            result_text = ""

        if result_text and (
            SUCCESS_PATTERN.search(result_text)
            or FAILURE_PATTERN.search(result_text)
        ):
            break

        page.wait_for_timeout(
            POLL_INTERVAL_MS
        )

    if not result_text:

        raise UploadError(
            f"{config.result_timeout_ms / 1000:.0f} 秒内没有拿到上传结果，"
            "请到 SDCC「导入结果」页人工确认后再决定是否重传",
            ErrorKind.RETRYABLE,
        )

    has_success = bool(
        SUCCESS_PATTERN.search(result_text)
    )

    has_failure = bool(
        FAILURE_PATTERN.search(result_text)
    )

    if has_failure and not has_success:

        raise UploadError(
            f"SDCC 返回上传失败：{result_text}",
            ErrorKind.FATAL,
            result_text,
        )

    if has_failure and has_success:

        logger.warn(
            "upload",
            f"上传部分失败，需人工核对：{result_text}",
        )

    else:

        logger.success(
            "upload",
            f"上传结果：{result_text}",
        )

    return result_text


def _close_dialog(
    dialog,
    logger,
) -> None:

    try:

        dialog.get_by_role(
            "button",
            name="返回",
        ).first.click(
            timeout=5_000
        )

    except PlaywrightError:

        logger.info(
            "dialog",
            "关闭弹窗失败，忽略",
        )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def upload_file(
    file_path: PathLike,
    config,
    logger=None,
    screenshot_dir: Optional[PathLike] = None,
    progress: ProgressCallback = None,
) -> str:
    """上传一个文件到 SDCC，返回结果文案。

    登录方式：每次打开全新的浏览器，由用户手工完成登录。
    不使用账号密码自动登录，不读取/保存 session。
    """

    path = Path(file_path)

    if not path.exists():

        raise UploadError(
            f"待上传文件不存在：{path}",
            ErrorKind.FATAL,
        )

    shot_dir = (
        Path(screenshot_dir)
        if screenshot_dir
        else None
    )

    run_id = getattr(
        logger,
        "run_id",
        "",
    )

    def report(
        step: str,
        status: str,
        message: str = "",
    ) -> None:

        if progress is None:
            return

        try:

            progress(
                step,
                status,
                message,
            )

        except Exception:
            pass

    with sync_playwright() as playwright:

        # 必须有界面，因为用户需要手工登录。
        browser = _launch_browser(
            playwright,
            config,
            logger,
            headless=False,
        )

        # 每次都是全新的 context。
        # 不传 storage_state。
        context = browser.new_context(
            accept_downloads=False,
        )

        context.set_default_timeout(
            config.timeout_ms
        )

        page = context.new_page()

        try:

            # ----------------------------------------------------------
            # 1. 手工登录
            # ----------------------------------------------------------

            report(
                "login",
                "start",
                "正在打开 SDCC，请手工登录",
            )

            page = _ensure_logged_in(
                page,
                config,
                logger,
                report,
            )

            # ----------------------------------------------------------
            # 2. 进入订单管理 / 打开导入弹窗
            # ----------------------------------------------------------

            report(
                "navigate",
                "start",
                "正在进入订单管理",
            )

            dialog = _open_import_dialog(
                page,
                config,
                logger,
            )

            report(
                "navigate",
                "success",
                "导入弹窗已打开",
            )

            # ----------------------------------------------------------
            # 3. 选择项目和模板
            # ----------------------------------------------------------

            report(
                "select",
                "start",
                "正在选择项目和模板",
            )

            _select_option(
                page,
                dialog,
                PROJECT_PLACEHOLDER,
                config.project,
                "项目",
                config,
                logger,
            )

            _select_option(
                page,
                dialog,
                TEMPLATE_PLACEHOLDER,
                config.template,
                "模板",
                config,
                logger,
            )

            report(
                "select",
                "success",
                "项目和模板已选择",
            )

            # ----------------------------------------------------------
            # 4. 上传
            # ----------------------------------------------------------

            report(
                "upload",
                "start",
                f"正在上传 {path.name}",
            )

            result_text = _upload_and_wait(
                page,
                dialog,
                path,
                config,
                logger,
            )

            report(
                "upload",
                "success",
                result_text,
            )

            # ----------------------------------------------------------
            # 5. 关闭弹窗
            # ----------------------------------------------------------

            _close_dialog(
                dialog,
                logger,
            )

            # 不保存 session。
            return result_text

        except UploadError as exc:

            if not exc.detail:

                exc.detail = _capture_screenshot(
                    page,
                    shot_dir,
                    run_id,
                )

            report(
                "upload",
                "fail",
                exc.message,
            )

            raise

        except PlaywrightTimeout as exc:

            shot = _capture_screenshot(
                page,
                shot_dir,
                run_id,
            )

            report(
                "upload",
                "fail",
                "页面操作超时",
            )

            raise UploadError(
                f"页面操作超时：{exc}",
                ErrorKind.RETRYABLE,
                shot,
            ) from exc

        except PlaywrightError as exc:

            shot = _capture_screenshot(
                page,
                shot_dir,
                run_id,
            )

            report(
                "upload",
                "fail",
                "浏览器操作失败",
            )

            raise UploadError(
                f"浏览器操作失败：{exc}",
                ErrorKind.RETRYABLE,
                shot,
            ) from exc

        finally:

            try:
                context.close()
            except PlaywrightError:
                pass

            finally:
                browser.close()