"""从壳牌 LMS 系统自动导出装运单（订单源数据）。

流程：登录壳牌 LMS → 运输管理 → 装运单 → 计划日期设成明天 →
勾选运输区域 → 点「导出」下载 xlsx。

导出的 xlsx 就是要和车型文件合并、再转成 SDCC 模板上传的那份源数据，
所以这里只负责「拿到源文件」，转换和上传各自是下一步。

壳牌 LMS 是和 SDCC 完全独立的另一个账号，且不发短信验证码，
所以登录比 SDCC 简单：账号密码直登即可，不做会话复用。

设计上和 core.uploader 对齐：
- logger 用 services.logger.RunLogger（.start/.info/.success/.warn/.fail）
- progress 回调签名 report(step, status, msg)，供 UI 实时显示
- 出错抛 core.models.UploadError，带 ErrorKind 供调度器判断是否重试
  （沿用 UploadError 而不新造异常，是为了让 scheduler 的重试逻辑不用改）

独立测试：python -m core.shell_exporter（有头浏览器跑一遍，盯着页面调选择器）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Union

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .models import ErrorKind, UploadError
# _launch_browser 已经把「Chrome/Edge 挨个试、都没有就报错」处理好了，直接复用
from .uploader import _launch_browser

PathLike = Union[str, Path]
ProgressCallback = Optional[Callable[[str, str, str], None]]

# --------------------------------------------------------------------------- #
# 选择器
# --------------------------------------------------------------------------- #

# 登录页
USERNAME_PLACEHOLDER = "请输入您的账号"
PASSWORD_PLACEHOLDER = "请输入您的密码"
# 登录按钮是 <div class="login-button">，不是真的 <button>（type="button" 写在 div 上无效），
# 所以不能用 get_by_role("button")，按 class 定位
LOGIN_BUTTON_SELECTOR = "div.login-button"

# 计划日期的两个输入框（起、止），都设成明天。
# 注意：两个框 id 不同构（一个 QPlanDate、一个 DatePicker1_Comp2），不能用后缀统一匹配；
# 而且 QPlanDate 后缀还会误中同组的 div/span。所以直接拿两个 input 的确切 id。
# id 里的 "2_" 像是页签/行的序号；若换页面后前缀变了，改用 ".ui-dp input.txt" 按结构定位。
PLAN_DATE_INPUT_IDS = (
    "dateInput_2_QPlanDate",
    "dateInput_2_DatePicker1_Comp2",
)
# 日期格式：input 的 maxlength=10，正好是 YYYY-MM-DD；实测若不认改这里（比如 %Y/%m/%d）
DATE_FORMAT = "%Y-%m-%d"

# 导出文件名前缀
EXPORT_PREFIX = "shell"

# 各步超时
NAV_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 120_000
# 运输区域是个 ui-dict 下拉控件：点箭头展开面板 → 面板里有搜索框和区域表 →
# 勾中目标行的 <span class="dt-chk"> → 点 <span class="sure">确定</span>。
# 用 id 后缀匹配，避开会变的 "2_" 前缀。
REGION_DROPBTN_SELECTOR = "[id$='QCityGroup_dropbtn']"
REGION_DROPVIEW_SELECTOR = "[id$='QCityGroup_dropview']"


def _noop(step: str, status: str, msg: str) -> None:
    pass


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _tomorrow(fmt: str = DATE_FORMAT) -> str:
    return (datetime.now() + timedelta(days=1)).strftime(fmt)


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #


def _login(page, config, password: str, logger, report) -> None:
    login_url = config.shell_login_url
    username = config.shell_username
    if not username or not password:
        raise UploadError(
            "壳牌 LMS 没有账号或密码，请先到「设置」里录入壳牌账号密码",
            ErrorKind.FATAL,
        )

    logger.start("login", f"打开壳牌 LMS：{login_url}")
    report("login", "start", "正在登录壳牌 LMS")
    page.goto(login_url, wait_until="domcontentloaded")

    try:
        page.get_by_placeholder(USERNAME_PLACEHOLDER).first.fill(username)
        page.get_by_placeholder(PASSWORD_PLACEHOLDER).first.fill(password)
        page.locator(LOGIN_BUTTON_SELECTOR).first.click()
    except PlaywrightError as exc:
        raise UploadError("登录页元素没找到，页面可能改版", ErrorKind.RETRYABLE, str(exc))

    # 登录成功的判据：能看到「运输管理」入口。壳牌不发短信，正常账号密码就能进；
    # 进不去多半是账号密码错，直接当 FATAL，重试也是白撞。
    try:
        page.get_by_text("运输管理").first.wait_for(timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise UploadError(
            "登录后没进到主页面，请确认壳牌账号密码是否正确",
            ErrorKind.FATAL,
        )
    logger.success("login", f"壳牌 LMS 登录成功：{username}")


# --------------------------------------------------------------------------- #
# 导航
# --------------------------------------------------------------------------- #


def _open_shipment_list(page, logger, report):
    """运输管理 → #Booking0 → 装运单，返回装运单列表所在的 page。

    若装运单是新开标签页打开的，实测时改成 expect_popup 拿新页。目前按同页处理。
    """
    report("navigate", "start", "进入装运单")
    logger.start("navigate", "运输管理 → 装运单")

    page.get_by_text("运输管理").first.click()
    # #Booking0 是运输管理下的二级入口
    page.locator("#Booking0").first.click()
    # 「装运单」是个链接，点击后进列表页
    page.locator("a").filter(has_text="装运单").first.click()
    page.wait_for_load_state("domcontentloaded")

    logger.success("navigate", "已进入装运单列表")
    return page


def _set_plan_dates(page, logger) -> None:
    """把计划日期的起止两个框都设成明天。

    这两个框是 <input type=text class=txt>（非 readonly），fill 能直接写；
    再用 JS 派发 input/change 兜底，保证 datepicker 控件读到新值。
    列表页的日期控件是异步渲染的，所以先等第一个框出现再操作
    （之前就是没等、点太早，导致一个都没找到）。
    """
    tomorrow = _tomorrow()
    filled = 0
    for i, input_id in enumerate(PLAN_DATE_INPUT_IDS):
        box = page.locator(f"#{input_id}").first
        try:
            # 第一个框等久点，等列表页把日期控件渲染出来；后面的不用久等
            box.wait_for(
                state="visible", timeout=NAV_TIMEOUT_MS if i == 0 else 5_000
            )
        except PlaywrightTimeout:
            logger.warn("navigate", f"没等到日期框 #{input_id}，跳过")
            continue
        try:
            box.fill(tomorrow)
        except PlaywrightError:
            pass
        # JS 兜底：赋值 + 触发 input/change，保证控件能收到新值
        try:
            box.evaluate(
                "(el, v) => { el.value = v;"
                " el.dispatchEvent(new Event('input', {bubbles:true}));"
                " el.dispatchEvent(new Event('change', {bubbles:true})); }",
                tomorrow,
            )
        except PlaywrightError:
            pass
        filled += 1

    if filled == 0:
        raise UploadError(
            "没找到任何计划日期输入框（页面可能改版，或 id 前缀变了）",
            ErrorKind.RETRYABLE,
        )
    logger.info("navigate", f"计划日期已设为明天：{tomorrow}（共填 {filled} 个框）")


def _select_region(page, region: str, logger) -> None:
    """在运输区域下拉控件里勾选目标区域。

    控件是 ui-dict：一个 readonly 输入框 + 箭头，点箭头展开面板。面板里：
    - .findinput 关键字搜索框，用它过滤，省得翻页
    - 每个区域是一行 <tr>，行内 <span class="dt-chk"> 是勾选态：
      chkstate0=未选、chkstate1=已选。点它是切换，所以已选就别再点（会取消）。
    - <span class="sure">确定</span> 确认
    """
    logger.start("navigate", f"选择运输区域：{region}")

    # 1) 点箭头展开下拉面板
    page.locator(REGION_DROPBTN_SELECTOR).first.click()
    dropview = page.locator(REGION_DROPVIEW_SELECTOR).first
    try:
        dropview.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise UploadError("点了区域下拉但面板没弹出", ErrorKind.RETRYABLE)

    # 2) 用搜索框过滤到目标区域（比翻页稳；没搜索框也不影响，直接在表里找）
    try:
        search = dropview.locator(".findinput").first
        search.fill(region)
        search.press("Enter")
        page.wait_for_timeout(800)  # 等表格按关键字刷新
    except PlaywrightError:
        pass

    # 3) 找到目标区域那一行（tr 里含中文区域名），按需勾选
    row = dropview.locator("tr").filter(has_text=region).first
    try:
        row.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout:
        raise UploadError(f"区域列表里没找到「{region}」", ErrorKind.FATAL)

    chk = row.locator(".dt-chk").first
    # class 里带 chkstate1 表示已选；已选就不要再点，点了会取消
    already_selected = False
    try:
        already_selected = "chkstate1" in (chk.get_attribute("class") or "")
    except PlaywrightError:
        pass
    if not already_selected:
        try:
            chk.click()
        except PlaywrightError:
            # 兜底：点该行的锚点/代码文字
            row.locator("a.dt-anchor, .b-txt").first.click()

    # 4) 点「确定」
    dropview.locator(".sure").first.click()
    logger.success("navigate", f"已选择区域：{region}")


def _click_query(page, logger) -> None:
    """点「查询」刷新列表。导出前必须先查询，否则导的是空/旧数据。

    查询按钮是 <div id="3_Query" class="ui-btn Query"><button>...</button></div>，
    里面是真 <button>；用 class 定位（id 的 "3_" 前缀会变）。
    """
    logger.start("navigate", "点击查询")
    page.locator(".ui-btn.Query button").first.click()
    # 等列表按条件刷新出来再导出
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(500)
    logger.success("navigate", "查询完成")


def _trigger_export(page, target: Path, logger, report) -> Path:
    """点「导出」并等浏览器下载，存到 target。"""
    report("export", "start", "正在导出并下载")
    logger.start("export", "点击导出")
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl:
            # 导出是 <li key="Export"><a class="tbr-btn"><span>导出</span></a></li>，
            # 不是真 <button>；拿语义标记 key="Export" 定位最稳
            page.locator("li[key='Export'] a.tbr-btn").first.click()
        download = dl.value
    except PlaywrightTimeout:
        raise UploadError(
            "点了导出但没等到文件下载，可能是没有符合条件的订单或页面改版",
            ErrorKind.RETRYABLE,
        )
    except PlaywrightError as exc:
        raise UploadError("导出按钮点击失败", ErrorKind.RETRYABLE, str(exc))

    download.save_as(str(target))
    logger.success("export", f"已下载：{target}")
    report("export", "success", f"导出完成：{target.name}")
    return target


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def export_orders(
    config=None,
    logger=None,
    password: str = "",
    *,
    progress: ProgressCallback = None,
    headless: Optional[bool] = None,
    run_id: Optional[str] = None,
) -> Path:
    """从壳牌 LMS 导出装运单，返回下载到本地的 xlsx 路径。

    password 为空时会尝试从 keyring 里按壳牌用户名取（和 SDCC 共用一个服务名、
    但用户名不同，互不影响）；取不到就报错。
    这个函数只做「导出拿文件」，转换/上传由各自的模块接手。
    """
    # 延迟导入，避免和 services 之间的循环依赖
    from services.config import Config, shell_orders_dir
    from services.logger import RunLogger

    config = config or Config.load()
    logger = logger or RunLogger(run_id=run_id)
    report = progress or _noop

    username = config.shell_username
    if not password:
        try:
            from services import credentials

            password = credentials.get_password(username) or ""
        except Exception:  # noqa: BLE001 - 取不到就当空，交给 _login 报错
            password = ""

    # 导出直接落到 壳牌订单/，用户可直接查看，无需再复制
    target = shell_orders_dir() / f"{EXPORT_PREFIX}_{datetime.now():%Y%m%d}.xlsx"

    report("browser", "start", "启动浏览器")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, config, logger, headless=headless)
        # 导出要接收下载，必须开 accept_downloads
        context = browser.new_context(accept_downloads=True)
        context.set_default_timeout(config.timeout_ms)
        page = context.new_page()
        try:
            _login(page, config, password, logger, report)
            list_page = _open_shipment_list(page, logger, report)
            _set_plan_dates(list_page, logger)
            _select_region(list_page, config.export_region, logger)
            _click_query(list_page, logger)
            result = _trigger_export(list_page, target, logger, report)
            return result
        except UploadError:
            raise
        except PlaywrightTimeout as exc:
            raise UploadError("壳牌 LMS 导出超时", ErrorKind.RETRYABLE, str(exc))
        except PlaywrightError as exc:
            raise UploadError("壳牌 LMS 导出过程出错", ErrorKind.RETRYABLE, str(exc))
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass
            try:
                browser.close()
            except PlaywrightError:
                pass


if __name__ == "__main__":
    # 独立测试：python -m core.shell_exporter
    # 有头浏览器跑一遍，把过程打印到控制台，方便盯着页面调选择器。
    import getpass
    import sys

    from services.config import Config
    from services.logger import RunLogger

    cfg = Config.load()
    if not cfg.shell_username:
        cfg.shell_username = input("壳牌 LMS 账号：").strip()

    pwd = ""
    try:
        from services import credentials

        pwd = credentials.get_password(cfg.shell_username) or ""
    except Exception:  # noqa: BLE001
        pwd = ""
    if not pwd:
        pwd = getpass.getpass("壳牌 LMS 密码：")

    def _print(step: str, status: str, msg: str) -> None:
        print(f"[{step}/{status}] {msg}")

    try:
        path = export_orders(
            config=cfg,
            logger=RunLogger(console=True),
            password=pwd,
            progress=_print,
            headless=False,
        )
        print(f"\n导出成功：{path}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n导出失败：{exc}")
        sys.exit(1)
