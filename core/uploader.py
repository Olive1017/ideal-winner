from __future__ import annotations

import os
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

# 出现这些字样说明是账号/密码/验证码问题，重试没有意义
AUTH_FAILURE_PATTERN = re.compile(r"密码|账号|帐号|用户名|验证码|锁定|冻结|不存在")

# IAM 点「登录」后会自动弹出的「登录说明」页，不是登录页，要排除掉
GUIDE_URL_PATTERN = re.compile(r"loginInfoIframe", re.IGNORECASE)

# 登录 iframe，用来在一堆标签页里认出真正的登录页
LOGIN_IFRAME_SELECTOR = "#iam_iframe_sdk"
USERNAME_PLACEHOLDER = "用户编号"
PASSWORD_PLACEHOLDER = "密码"
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

# 带会话进来后，最多花多久确认「不需要登录」。会话有效时落地页不会出现
# 「登录」按钮，这段时间会等满，所以设短一点，别让每次成功上传都白等。
LOGIN_STATE_PROBE_MS = 8_000

# 「我的工作台」按钮：账号密码登录后的落地页要先点它；会话恢复的落地页没有它。
# 等这么久还没出现就当它不存在，直接跳到订单中心，不作为硬前置。
WORKBENCH_WAIT_MS = 3_000

# 人工登录（python main.py login）最多等多久，以及等待期间多久播报一次
MANUAL_LOGIN_TIMEOUT_MS = 600_000
LOGIN_HEARTBEAT_SEC = 30.0


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


def _launch_browser(playwright, config, logger, headless: Optional[bool] = None):
    """优先 Chrome，起不来就降级 Edge，都没有才报错。

    不打包 Chromium，是为了把分发给同事的 exe 控制在合理体积。

    ``headless`` 传 None 表示按配置走；人工登录那条路必须看得见窗口，
    会显式传 False 把配置盖掉。
    """
    channels = list(config.browser_channels or ["chrome"])
    want_headless = bool(config.headless) if headless is None else bool(headless)
    last_error: Optional[Exception] = None
    for channel in channels:
        try:
            browser = playwright.chromium.launch(
                channel=channel, headless=want_headless
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


def _page_url(page) -> str:
    """安全地取页面 URL；页面刚关掉时不要因此炸掉调用方。"""
    try:
        return page.url or ""
    except PlaywrightError:
        return ""


def _live_pages(context) -> list:
    return [p for p in context.pages if not p.is_closed()]


def _open_page_urls(context) -> list:
    return [_page_url(p) for p in _live_pages(context)]


def _close_guide_pages(context, logger) -> int:
    """关掉 IAM 自动弹出的「登录说明」页。

    点「登录」之后 IAM 会额外开一个纯指引页，跟登录无关。关掉它是为了
    后面遍历标签页找登录页/工作台时不被它干扰。
    """
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
        logger.info("login", f"已关闭「登录说明」页 {closed} 个")
    return closed


def _is_logged_in(page) -> bool:

    try:
        return page.get_by_role("button", name=WORKBENCH_BUTTON).first.is_visible()
    except PlaywrightError:
        return False


def _find_logged_in_page(context):
    """在所有标签页里找已登录的那个，找不到返回 None。

    登录完成后工作台可能落在任意一个标签页上，所以必须全都看一遍，
    并且把找到的页面交回去当作后续操作的对象。
    """
    for candidate in _live_pages(context):
        if GUIDE_URL_PATTERN.search(_page_url(candidate)):
            continue
        if _is_logged_in(candidate):
            return candidate
    return None


def _find_login_page(context, timeout_ms: int):
    """在所有标签页里找带登录 iframe 的那个。

    登录页可能是点「登录」后新开的弹窗，也可能是原页面直接跳转过去的，
    所以不能只在「新增的页面」里找，也不能假设第一个弹出的就是它。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for candidate in _live_pages(context):
            if GUIDE_URL_PATTERN.search(_page_url(candidate)):
                continue
            try:
                if candidate.locator(LOGIN_IFRAME_SELECTOR).count() > 0:
                    return candidate
            except PlaywrightError:
                continue
        time.sleep(PAGE_SCAN_INTERVAL_SEC)
    return None


def _wait_for_login(context, timeout_ms: int, logger):
    """轮询等待登录完成，返回已登录的标签页；超时返回 None。

    自动登录和人工登录共用这一段：两者的差别只在「谁来填表单」，
    「怎么算登录成功」必须是同一个判定，否则又会出现
    「手动能过、自动过不了」这种没法排查的情况。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    next_beat = time.monotonic() + LOGIN_HEARTBEAT_SEC
    while time.monotonic() < deadline:
        _close_guide_pages(context, logger)
        found = _find_logged_in_page(context)
        if found is not None:
            return found
        now = time.monotonic()
        if now >= next_beat:
            logger.info("login", f"仍在等待登录完成……剩余约 {(deadline - now) / 60:.0f} 分钟")
            next_beat = now + LOGIN_HEARTBEAT_SEC
        time.sleep(PAGE_SCAN_INTERVAL_SEC)
    return None


def _read_login_error(frame) -> str:
    """尽力从登录 iframe 里抓出错误提示文案，用来判断是不是账号密码问题。"""
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
# 会话（storage_state）
# --------------------------------------------------------------------------- #


def _resolve_session_file(session_file: Optional[PathLike]) -> Optional[Path]:
    """None 表示用默认位置。

    这里是**延迟导入**：services.scheduler 在模块顶层导入 core.uploader，
    如果 core.uploader 也在顶层导入 services.config，两个包就互相依赖了。
    放进函数体里，等真正调用时才解析，绕开成环。
    """
    if session_file is not None:
        return Path(session_file)
    try:
        from services.config import session_path

        return session_path()
    except Exception:  # noqa: BLE001 - 拿不到就当作不启用会话复用
        return None


def _new_context(browser, config, session_file: Optional[Path], logger):
    """建 context，能复用会话就带上 storage_state。

    storage_state 里存的是 cookie + localStorage，也就是「我已登录」和
    「这台机器过过验证码」这两件事。带上它，服务器就认得这个浏览器，
    不会再发短信。
    """
    reuse = bool(getattr(config, "reuse_session", True))
    state: Optional[str] = None

    if not reuse:
        logger.info("login", "配置里关掉了会话复用，本次全新登录")
    elif session_file is not None and session_file.exists():
        state = str(session_file)
        logger.info("login", f"载入已保存的会话：{session_file.name}")
    else:
        logger.info("login", "没有已保存的会话，本次需要登录")

    if state is None:
        return browser.new_context(accept_downloads=False)

    try:
        return browser.new_context(accept_downloads=False, storage_state=state)
    except (PlaywrightError, ValueError) as exc:
        # 会话文件损坏不该让整个任务挂掉，退回全新 context 走正常登录
        logger.warn("login", f"会话文件无法载入，已忽略：{exc}")
        return browser.new_context(accept_downloads=False)


def _save_storage_state(context, session_file: Optional[Path], logger) -> bool:
    """把当前登录状态写进 session.json。

    存盘失败**绝不能**影响已经成功的上传，所以这里吞掉所有异常只记警告。
    """
    if session_file is None:
        return False
    try:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(session_file))
    except Exception as exc:  # noqa: BLE001
        logger.warn("login", f"会话保存失败：{exc}")
        return False

    # 文件里是活的登录凭证，等价于密码。Windows 上 chmod 基本无效，改不动就算了。
    try:
        os.chmod(str(session_file), 0o600)
    except OSError:
        pass

    logger.info("login", f"会话已保存：{session_file}")
    return True


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #


def _open_login_page(page, context, config, logger):
    """点首页的「登录」，把真正的登录页找出来并置前。

    自动登录和人工登录共用。找不到就返回 None，由调用方决定是报错还是让人接手。
    """
    # exact=True 很重要：页面上还有「登录说明」「退出登录」这类文案，
    # 默认的子串匹配会把它们一起命中。
    page.get_by_role("button", name="登录", exact=True).first.click()

    # 点「登录」之后，IAM 会额外弹一个「登录说明」页，跟登录页的先后顺序不固定。
    # 所以按「有没有登录 iframe」来认页面，而不是用 expect_popup 拿第一个弹窗。
    login_page = _find_login_page(context, config.timeout_ms)
    _close_guide_pages(context, logger)

    if login_page is None or login_page.is_closed():
        return None

    try:
        login_page.bring_to_front()
    except PlaywrightError:
        pass
    return login_page


def _auto_login(page, context, config, password: str, logger):
    """用保存的账号密码自动登录，返回已登录的那个标签页。"""
    if not config.username or not password:
        raise UploadError(
            "没有可复用的会话，也没有账号密码。"
            "请先到「设置」里录入账号密码，或跑一次 python main.py login",
            ErrorKind.FATAL,
        )

    logger.start("login", f"自动登录：{config.username}")
    login_page = _open_login_page(page, context, config, logger)
    if login_page is None:
        raise UploadError(
            "点击登录后没找到登录页（IAM 登录 iframe 未出现）",
            ErrorKind.RETRYABLE,
            "当前标签页：" + (" | ".join(_open_page_urls(context)) or "无"),
        )

    # 定位器不跨 iframe 边界，必须先 frame_locator 进去再找输入框。
    frame = login_page.frame_locator(LOGIN_IFRAME_SELECTOR)
    frame.get_by_placeholder(USERNAME_PLACEHOLDER).first.fill(config.username)
    frame.get_by_placeholder(PASSWORD_PLACEHOLDER).first.fill(password)
    frame.get_by_role("button", name="登录").first.click()

    found = _wait_for_login(context, config.timeout_ms, logger)
    if found is not None:
        logger.success("login", f"账号 {config.username} 登录成功")
        return found

    detail = ""
    if not login_page.is_closed():
        detail = _read_login_error(frame)
    detail = detail or "登录后没有跳转回主页面，多半是被要求短信验证码"

    if AUTH_FAILURE_PATTERN.search(detail):
        # 验证码 / 密码错 / 账号锁定，重试多少次都一样，必须人来处理。
        raise UploadError(
            f"自动登录失败：{detail}。"
            "如果是验证码，说明这台机器的设备信任已过期，"
            "跑一次 python main.py login 人工登录并保存会话即可",
            ErrorKind.FATAL,
        )
    raise UploadError(f"自动登录失败：{detail}", ErrorKind.RETRYABLE)


def _need_login(page, timeout_ms: int, logger):
    
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _close_guide_pages(page.context, logger)
        try:
            if page.get_by_role("button", name="登录", exact=True).first.is_visible():
                return True
        except PlaywrightError:
            pass
        time.sleep(PAGE_SCAN_INTERVAL_SEC)
    return False


def _ensure_logged_in(page, config, password: str, logger, report):
    """保证进入登录态，返回后续操作应该使用的标签页。

    返回值很重要：登录完成后工作台不一定还在我们最初打开的那个标签页上。
    """
    context = page.context

    logger.start("login", "打开 SDCC")
    page.goto(config.login_url, wait_until="domcontentloaded")
    _close_guide_pages(context, logger)

    # 第一级：判据是落地页有没有「登录」按钮。会话有效时（本例）直接就是
    # 登录后的样子、没有「登录」按钮；会话无效时 .../manage/ 会给一个「登录」按钮。
    # 不再依赖「我的工作台」——那是账号密码登录后才有、会话恢复的落地页上根本没有。
    if not _need_login(page, LOGIN_STATE_PROBE_MS, logger):
        logger.success("login", "会话有效，跳过登录")
        report("login", "success", "会话有效，跳过登录")
        return page

    # 第二级：会话过期了，但浏览器里的设备信任标记通常命更长，
    # 这时候账号密码往往能直接进，服务器不会再发短信。
    logger.info("login", "会话无效或不存在，改用账号密码登录")
    report("login", "start", "会话已失效，正在用账号密码登录")
    return _auto_login(page, context, config, password, logger)


def _prefill_credentials(page, context, config, password: str, logger) -> None:
    """人工登录时，把能自动填的先填上，验证码留给人。

    这里**故意不点提交**：验证码填在哪一步、要不要先点一次「确定」才出现，
    各家 IAM 不一样。猜错了就是白白报一个错误提示，还不如让人自己点。

    任何一步失败都只记警告不抛异常——大不了整个表单你自己输，
    这一步只是省事，不是流程前提。
    """
    try:
        login_page = _open_login_page(page, context, config, logger)
    except PlaywrightError as exc:
        logger.warn("login", f"没点到首页的「登录」按钮，请手动点开登录页：{exc}")
        return

    if login_page is None:
        logger.warn("login", "没自动找到登录页，请在浏览器里手动操作")
        return

    if not config.username or not password:
        logger.info("login", "没有保存账号或密码，请手动输入完整表单")
        return

    try:
        frame = login_page.frame_locator(LOGIN_IFRAME_SELECTOR)
        frame.get_by_placeholder(USERNAME_PLACEHOLDER).first.fill(config.username)
        frame.get_by_placeholder(PASSWORD_PLACEHOLDER).first.fill(password)
        logger.info("login", f"已自动填入账号 {config.username} 和密码")
    except PlaywrightError as exc:
        logger.warn("login", f"自动填账号密码失败，请手动输入：{exc}")


def save_session(
    config,
    logger,
    password: str = "",
    session_file: Optional[PathLike] = None,
    timeout_ms: int = MANUAL_LOGIN_TIMEOUT_MS,
) -> Path:
    """人工登录一次，把登录状态存成 storage_state 供以后复用。

    有账号密码就自动填上，但**不替你提交**——SDCC 要短信验证码，
    只能你自己输。程序在旁边轮询，一看到「我的工作台」就存盘退出。

    这条路是给 ``python main.py login`` 用的，人在跟前，所以强制有头浏览器。

    Args:
        config: services.config.Config 实例。
        logger: services.logger.RunLogger 实例。
        password: keyring 里的密码，没有就留空，全靠手输。
        session_file: 会话保存位置，默认 services.config.session_path()。
        timeout_ms: 最多等多久，默认 10 分钟。

    Returns:
        保存好的 session.json 路径。

    Raises:
        UploadError: 浏览器起不来、超时没等到登录成功、或者存盘失败。
    """
    target = _resolve_session_file(session_file)
    if target is None:
        raise UploadError("无法确定会话文件的保存位置", ErrorKind.FATAL)

    with sync_playwright() as playwright:
        # 人在跟前输验证码，必须看得见窗口，这里不看 config.headless
        browser = _launch_browser(playwright, config, logger, headless=False)
        # 故意用全新 context：既然是来重新登录的，就该从干净状态开始，
        # 免得一个半死不活的旧会话把流程带偏。
        context = browser.new_context(accept_downloads=False)
        context.set_default_timeout(config.timeout_ms)
        page = context.new_page()

        try:
            logger.start("login", "打开 SDCC，等待人工登录")
            page.goto(config.login_url, wait_until="domcontentloaded")
            _close_guide_pages(context, logger)

            _prefill_credentials(page, context, config, password, logger)
            logger.info("login", "请在浏览器里完成登录（含短信验证码），程序会自动检测")

            found = _wait_for_login(context, timeout_ms, logger)
            if found is None:
                raise UploadError(
                    f"{timeout_ms / 60000:.0f} 分钟内没有检测到登录成功，已放弃",
                    ErrorKind.FATAL,
                )

            if not _save_storage_state(context, target, logger):
                raise UploadError("登录成功了，但会话没能写入磁盘", ErrorKind.FATAL)

            logger.success("login", f"会话已保存：{target}")
            return target
        finally:
            try:
                context.close()
            finally:
                browser.close()


# --------------------------------------------------------------------------- #
# 各步骤
# --------------------------------------------------------------------------- #


def _find_import_button(page, config, logger):
    button = page.locator(IMPORT_BUTTON_SELECTOR).filter(has_text=re.compile(r"导入")).first
    try:
        button.wait_for(state="visible", timeout=config.timeout_ms)
    except PlaywrightTimeout as exc:
        raise UploadError(
            "订单管理页上找不到「导入」按钮",
            ErrorKind.RETRYABLE,
            "页面可能还没加载完，或者当前账号没有导入权限",
        ) from exc
    return button


def _click_popover_item(page, reference, text: str, logger) -> None:
    popover_id = reference.get_attribute("aria-describedby") or ""
    if not popover_id:
        raise UploadError(
            "「导入」按钮上没有 aria-describedby，无法定位浮层菜单",
            ErrorKind.RETRYABLE,
        )

    item = page.locator(f"#{popover_id}").get_by_text(text, exact=True).first
    try:
        item.wait_for(state="visible", timeout=DROPDOWN_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        raise UploadError(f"点开「导入」后没有出现「{text}」菜单项", ErrorKind.RETRYABLE) from exc
    item.click()
    logger.info("dialog", f"已在浮层 #{popover_id} 里点击「{text}」")


def _open_import_dialog(page, config, logger):
    logger.start("navigate", "进入订单管理")

    workbench = page.get_by_role("button", name=WORKBENCH_BUTTON).first
    try:
        workbench.wait_for(state="visible", timeout=WORKBENCH_WAIT_MS)
        workbench.click()
        logger.info("navigate", "已点开「我的工作台」")
    except PlaywrightError:
        logger.info("navigate", "落地页没有「我的工作台」，直接找订单中心")

    page.locator(".el-submenu__title", has_text="订单中心").first.click()
    page.get_by_role("menuitem", name="订单管理", exact=True).first.click()
    

    import_button = _find_import_button(page, config, logger)
    logger.success("navigate", "已进入订单管理")

    logger.start("dialog", "打开导入订单弹窗")
    import_button.click()
    _click_popover_item(page, import_button, IMPORT_MENU_ITEM, logger)

  
    dialog = page.get_by_role("dialog").filter(
        has=page.get_by_placeholder(PROJECT_PLACEHOLDER)
    ).first
    dialog.get_by_placeholder(PROJECT_PLACEHOLDER).first.wait_for(
        state="visible", timeout=config.timeout_ms
    )
    logger.success("dialog", "导入弹窗已打开")
    return dialog


def _find_dropdown_option(page, keyword: str, field_name: str):
    options = page.locator(OPTION_SELECTOR)
    hit = options.filter(has_text=keyword).first
    try:
        hit.wait_for(state="visible", timeout=DROPDOWN_TIMEOUT_MS)
        return hit
    except PlaywrightTimeout as exc:
        try:
            seen = [t.strip() for t in options.all_inner_texts() if t.strip()]
        except PlaywrightError:
            seen = []
        raise UploadError(
            f"下拉列表里找不到{field_name}「{keyword}」；"
            f"当前可见选项：{seen if seen else '无（浮层没展开）'}",
            ErrorKind.FATAL,
        ) from exc


def _select_option(
    page, dialog, placeholder: str, keyword: str, field_name: str, config, logger
) -> None:

    logger.start("select", f"选择{field_name}：{keyword}")

    box = dialog.get_by_placeholder(placeholder).first
    try:
        box.wait_for(state="visible", timeout=config.timeout_ms)
    except PlaywrightTimeout as exc:
        raise UploadError(
            f"导入弹窗里找不到{field_name}下拉框（placeholder「{placeholder}」）",
            ErrorKind.FATAL,
        ) from exc

    box.click()  # 先展开浮层

    # el-select 不支持筛选时 input 是 readonly，fill() 会直接报错。
    # readonly 是布尔属性，值可能是空字符串，所以要跟 None 比，不能直接判真假。
    if box.get_attribute("readonly") is None:
        # 必须用真实按键，不能用 fill()。fill() 是直接赋 value + 只发一个 input
        # 事件，项目那个远程搜索下拉收不到，就永远不发查询请求，浮层一直是空的。
        # 这个坑实测踩过：字打进去了，但页面上一个可见浮层都没有。
        box.fill("")
        try:
            box.press_sequentially(keyword, delay=60)
        except (AttributeError, PlaywrightError):
            box.type(keyword, delay=60)  # 老版 Playwright 没有 press_sequentially

    _find_dropdown_option(page, keyword, field_name).click()
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
    password: str = "",
    logger=None,
    screenshot_dir: Optional[PathLike] = None,
    progress: ProgressCallback = None,
    session_file: Optional[PathLike] = None,
) -> str:
   
    path = Path(file_path)
    if not path.exists():
        raise UploadError(f"待上传文件不存在：{path}", ErrorKind.FATAL)

    shot_dir = Path(screenshot_dir) if screenshot_dir else None
    run_id = getattr(logger, "run_id", "")
    state_file = _resolve_session_file(session_file)

    def report(step: str, status: str, message: str = "") -> None:
        if progress is None:
            return
        try:
            progress(step, status, message)
        except Exception:  # noqa: BLE001 - UI 回调不能影响主流程
            pass

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, config, logger)
        context = _new_context(browser, config, state_file, logger)
        context.set_default_timeout(config.timeout_ms)
        page = context.new_page()

        try:
            report("login", "start", "正在打开 SDCC")
            # 注意要接住返回值：登录后工作台可能在另一个标签页上
            page = _ensure_logged_in(page, config, password, logger, report)

            report("navigate", "start", "正在进入订单管理")
            dialog = _open_import_dialog(page, config, logger)
            report("navigate", "success", "导入弹窗已打开")

            report("select", "start", "正在选择项目和模板")
            _select_option(
                page, dialog, PROJECT_PLACEHOLDER, config.project, "项目", config, logger
            )
            _select_option(
                page, dialog, TEMPLATE_PLACEHOLDER, config.template, "模板", config, logger
            )
            report("select", "success", "项目和模板已选择")

            report("upload", "start", f"正在上传 {path.name}")
            result_text = _upload_and_wait(page, dialog, path, config, logger)
            report("upload", "success", result_text)

            _close_dialog(dialog, logger)

            # 走到这里说明这套会话是好的，顺手重存一遍。
            # 如果 SDCC 是滚动续期，这一步能让会话一直不过期；
            # 就算不是，重存也不会更糟。
            if getattr(config, "reuse_session", True):
                _save_storage_state(context, state_file, logger)

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
