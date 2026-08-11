"""SDCC TMS 订单导入 RPA（Playwright）。

全流程无人值守：用保存的账号密码自动登录，然后
    我的工作台 → 订单中心 → 订单管理 → 导入 → 导入订单
    → 选项目 → 选模板 → 上传文件 → 读取上传结果

所有异常都归一成 ``UploadError`` 并带上 ``ErrorKind``，由调度器决定是否重试。

注意：如果 SDCC 对该账号启用了短信验证码，自动登录会失败并归为 FATAL（重试没用），
需要人到 IAM 侧关掉验证码要求，或改成先人工登录再复用会话。
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

# 出现这些字样说明是账号/密码/验证码问题，重试没有意义
AUTH_FAILURE_PATTERN = re.compile(r"密码|账号|帐号|用户名|验证码|锁定|冻结|不存在")

# IAM 点「登录」后会自动弹出的「登录说明」页，不是登录页，要排除掉
GUIDE_URL_PATTERN = re.compile(r"loginInfoIframe", re.IGNORECASE)

# 登录 iframe，用来在一堆标签页里认出真正的登录页
LOGIN_IFRAME_SELECTOR = "#iam_iframe_sdk"

# 登录表单的 placeholder。只取稳定的片段做模糊匹配——
# IAM 实际写的是「用户编号/手机号码/邮箱」，写全了反而容易因为改文案而失配。
USERNAME_PLACEHOLDER = "用户编号"
PASSWORD_PLACEHOLDER = "密码"

# 判定「已登录」只认一个信号：「我的工作台」按钮可见。详见 _is_logged_in 的说明。
WORKBENCH_BUTTON = "我的工作台"

# 订单管理页的「导入」按钮。SDCC 给它挂了 sdccDropBtn 这个自定义 class，
# 比按文案找稳；它同时是 el-popover 的触发元素，点完会弹出一个浮层菜单。
IMPORT_BUTTON_SELECTOR = "button.sdccDropBtn"
IMPORT_MENU_ITEM = "导入订单"

# 「1.上传准备」那一行有两个控件，而且是两种不同的下拉：
# - 项目：远程搜索，可输入筛选，placeholder 是「请输入关键字选择」
# - 模板：标准 el-select，input 带 readonly，只能点开选，placeholder 是「请选择」
# 所以不能用 nth(0)/nth(1) 按位置取，必须各认各的 placeholder。
# 两个串互不包含（「请输入关键字选择」里没有连着的「请选择」），不会误匹配。
PROJECT_PLACEHOLDER = "请输入关键字选择"
TEMPLATE_PLACEHOLDER = "请选择"

# 下拉浮层里的选项。Element UI 把浮层挂在 body 上并统一带 el-popper class，
# 所以一条并集选择器就能同时覆盖 el-select 和 el-autocomplete 两种下拉。
# 必须带 :visible：没展开的浮层也还在 DOM 里，详见 _find_dropdown_option。
# 不用裸 li——那会把左侧菜单也算进去，点中就跳页了。
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
# 登录
# --------------------------------------------------------------------------- #


def _auto_login(page, context, config, password: str, logger):
    """用保存的账号密码自动登录，返回已登录的那个标签页。"""
    if not config.username or not password:
        raise UploadError("自动登录需要账号和密码，请先到「设置」里录入", ErrorKind.FATAL)

    logger.start("login", f"自动登录：{config.username}")
    # exact=True 很重要：页面上还有「登录说明」「退出登录」这类文案，
    # 默认的子串匹配会把它们一起命中。
    page.get_by_role("button", name="登录", exact=True).first.click()

    # 点「登录」之后，IAM 会额外弹一个「登录说明」页，跟登录页的先后顺序不固定。
    # 所以按「有没有登录 iframe」来认页面，而不是用 expect_popup 拿第一个弹窗。
    login_page = _find_login_page(context, config.timeout_ms)
    _close_guide_pages(context, logger)

    if login_page is None or login_page.is_closed():
        raise UploadError(
            "点击登录后没找到登录页（IAM 登录 iframe 未出现）",
            ErrorKind.RETRYABLE,
            "当前标签页：" + (" | ".join(_open_page_urls(context)) or "无"),
        )

    try:
        login_page.bring_to_front()
    except PlaywrightError:
        pass

    # 定位器不跨 iframe 边界，必须先 frame_locator 进去再找输入框。
    frame = login_page.frame_locator(LOGIN_IFRAME_SELECTOR)
    frame.get_by_placeholder(USERNAME_PLACEHOLDER).first.fill(config.username)
    frame.get_by_placeholder(PASSWORD_PLACEHOLDER).first.fill(password)
    frame.get_by_role("button", name="登录").first.click()

    deadline = time.monotonic() + config.timeout_ms / 1000
    while time.monotonic() < deadline:
        _close_guide_pages(context, logger)
        found = _find_logged_in_page(context)
        if found is not None:
            logger.success("login", f"账号 {config.username} 登录成功")
            return found
        time.sleep(PAGE_SCAN_INTERVAL_SEC)

    detail = ""
    if not login_page.is_closed():
        detail = _read_login_error(frame)
    detail = detail or "登录后未跳转回主页面（可能要求短信验证码）"
    kind = ErrorKind.FATAL if AUTH_FAILURE_PATTERN.search(detail) else ErrorKind.RETRYABLE
    raise UploadError(f"登录失败：{detail}", kind)


def _ensure_logged_in(page, config, password: str, logger, report):
    """保证进入登录态，返回后续操作应该使用的标签页。

    返回值很重要：登录完成后工作台不一定还在我们最初打开的那个标签页上。
    """
    context = page.context

    logger.start("login", "打开 SDCC")
    page.goto(config.login_url, wait_until="domcontentloaded")
    _close_guide_pages(context, logger)

    # 浏览器带着有效会话进来的情况，直接跳过登录
    already = _find_logged_in_page(context)
    if already is not None:
        logger.success("login", "当前已是登录态，跳过登录")
        report("login", "success", "已处于登录态")
        return already

    return _auto_login(page, context, config, password, logger)


# --------------------------------------------------------------------------- #
# 各步骤
# --------------------------------------------------------------------------- #


def _find_import_button(page, config, logger):
    """找订单管理页上的「导入」按钮，并等到它可见。

    用 SDCC 自己挂的 sdccDropBtn class 定位，不怕改文案；再按「导入」二字过滤，
    是因为同一行上可能还有别的 sdccDropBtn 按钮（比如导出）。

    这里刻意不等「订单管理」标签页出现——SDCC 的标签栏是自定义组件，没有标准
    tab 角色，等它只会白白卡满超时。直接等下一步真正要点的东西。
    """
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
    """点击 el-popover 浮层里的菜单项。

    el-popover 的内容不渲染在按钮内部，而是挂到 body 底下的独立节点，
    通过按钮的 aria-describedby 关联。按这个 id 精确定位，
    避免点到页面别处同名的元素。
    """
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
    page.get_by_role("button", name=WORKBENCH_BUTTON).first.click()
    page.get_by_role("menuitem", name=re.compile("订单中心")).locator("div").first.click()
    page.get_by_text("订单管理", exact=True).first.click()

    import_button = _find_import_button(page, config, logger)
    logger.success("navigate", "已进入订单管理")

    logger.start("dialog", "打开导入订单弹窗")
    import_button.click()
    _click_popover_item(page, import_button, IMPORT_MENU_ITEM, logger)

    # 不按标题文案找弹窗。「导入 - 订单」这几个字里连字符两边到底有没有空格，
    # 猜错了就是又一次「明明开了却报超时」。改成按内容特征认：
    # 里面有「请输入关键字选择」输入框的那个 dialog 就是它，
    # 而这恰好就是下一步要填的东西。
    dialog = page.get_by_role("dialog").filter(
        has=page.get_by_placeholder(PROJECT_PLACEHOLDER)
    ).first
    dialog.get_by_placeholder(PROJECT_PLACEHOLDER).first.wait_for(
        state="visible", timeout=config.timeout_ms
    )
    logger.success("dialog", "导入弹窗已打开")
    return dialog


def _find_dropdown_option(page, keyword: str, field_name: str):
    """在当前展开的下拉浮层里找选项。

    两个关键点：

    1. 从 page 找而不是从 dialog 找——Element UI 把浮层挂在 body 上，不在弹窗里。
    2. OPTION_SELECTOR 里的 :visible 不能去掉。没展开的浮层也还在 DOM 里，
       而模板选项「中海壳牌深圳-中海壳牌导入模版」包含项目名「中海壳牌深圳」，
       has_text 是子串匹配，不先排除隐藏元素的话 .first 会拿到那个隐藏的 li，
       然后死等一个永远不会可见的东西。
    """
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
    """展开下拉 → （能输入就）输入关键词 → 点击列表项。

    按 placeholder 而不是按 nth(0)/nth(1) 取控件：项目和模板是两种不同的下拉，
    placeholder 各不相同，按位置取不但会取不到，顺序一变还会静默填错字段。

    也不用「填完直接回车」，因为下拉是异步加载的，回车经常选不中。
    """
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
) -> str:
    """把一个 Excel 上传到 SDCC，返回上传结果文案。

    Args:
        file_path: 待上传的 Excel。
        config: services.config.Config 实例。
        password: 从 keyring 取出的密码，自动登录必填。
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
