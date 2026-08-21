from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Union

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.models import ConvertError, ErrorKind, UploadError, UploadResult, UploadStatus
from core.uploader import upload_file

from . import credentials
from .config import (
    Config,
    UPLOAD_FILENAME,
    archive_pending,
    clear_stale_pending,
    latest_pending,
    log_dir,
    pending_dir,
    session_path,
)
from .logger import RunLogger, new_run_id
from .single_instance import upload_lock

JOB_ID_DAILY = "daily_upload"
JOB_ID_RETRY_PREFIX = "retry_upload"

PathLike = Union[str, Path]
ProgressCallback = Optional[Callable[[str, str, str], None]]
ResultCallback = Optional[Callable[[UploadResult], None]]


def _export_and_convert(config: Config, logger, report) -> Path:
    """导出壳牌订单 -> 用车型表转换 -> 写入待上传队列，返回队列里的文件路径。

    这一步把原来需要人工在「转换」页做的事自动化了：
    - 从壳牌 LMS 导出明天的装运单源数据
    - 套用 config.car_file 里的车型映射转成 SDCC 导入格式
    - 落到 pending/ 目录，交给后面的上传步骤

    重活（playwright、pandas）都在这里延迟导入，避免拉高模块加载成本、绕开循环依赖。
    """
    from core.converter import convert
    from core.converter import export as export_xlsx
    from core.shell_exporter import export_orders

    # 1) 从壳牌 LMS 导出源数据
    report("export", "start", "从壳牌 LMS 导出订单")
    shell_user = getattr(config, "shell_username", "") or ""
    shell_pwd = credentials.get_password(shell_user) if shell_user else ""
    shell_file = export_orders(
        config=config,
        logger=logger,
        password=shell_pwd or "",
        progress=report,
    )

    # 2) 转换（+ 车型表）。车型表是可选的；缺了只是把车型填「未知」并给告警
    report("convert", "start", "转换为 SDCC 导入格式")
    car_file = getattr(config, "car_file", "") or None
    result = convert(shell_file, car_file)
    for warning in result.warnings:
        report("convert", "warn", warning)

    # 3) 写入待上传队列（同名覆盖，队列里始终只留最新一份）
    target = pending_dir() / UPLOAD_FILENAME
    export_xlsx(result.df, target)
    report("convert", "success", f"转换完成，共 {result.row_count} 行，已放入待上传队列")
    return target


def run_pipeline_once(
    config: Optional[Config] = None,
    run_id: Optional[str] = None,
    attempt: int = 1,
    file_path: Optional[PathLike] = None,
    progress: ProgressCallback = None,
    interactive: bool = False,
    force_export: bool = False,
) -> UploadResult:
    """跑一次完整流水线：导出 -> 转换 -> 入队 -> 上传。

    取文件的优先级：
    1. 传了 file_path 就用它（手动指定某个文件上传）
    2. force_export=True 时无视队列，直接从壳牌重新导出最新订单
    3. 待上传队列里已有文件就用它（手动在「转换」页备好的，或上次重试留下的）
    4. 都没有才现跑「导出 + 转换」拿今天的
    """
    config = config or Config.load()
    logger = RunLogger(run_id or new_run_id())
    started = time.monotonic()

    def report(step: str, status: str, message: str = "") -> None:
        logger.log(step, status, message)
        if progress is not None:
            try:
                progress(step, status, message)
            except Exception:  # noqa: BLE001
                pass

    # 整条流水线串行：同一个 SDCC 账号不能开两个会话，拿不到锁就直接跳过
    if not upload_lock.acquire():
        report("lock", "info", "已有任务在执行，本次跳过")
        return UploadResult(
            status=UploadStatus.SKIPPED,
            message="已有任务在执行，本次跳过",
            run_id=logger.run_id,
        )

    target: Optional[Path] = None
    try:
        if file_path is not None:
            target = Path(file_path)
        else:
            if force_export:
                # 强制重新导出：忽略待上传队列里已有的文件，直接从壳牌拉最新的
                report("queue", "info", "强制重新导出：忽略待上传队列，从壳牌拉取最新订单")
                target = None
            else:
                target = latest_pending()
            if target is None:
                # 队列空（或强制重新导出），现跑导出 + 转换拿今天的文件
                try:
                    target = _export_and_convert(config, logger, report)
                except ConvertError as exc:
                    # 数据/格式问题，重试也是白搭，直接判失败不重试
                    logger.fail("convert", f"转换失败：{exc}")
                    report("convert", "fail", str(exc))
                    return UploadResult(
                        status=UploadStatus.FAILED,
                        message=f"转换失败：{exc}",
                        run_id=logger.run_id,
                        duration_sec=time.monotonic() - started,
                        attempt=attempt,
                        retryable=False,
                    )

        if target is None or not target.exists():
            report("queue", "info", "没有可上传的文件，本次跳过")
            return UploadResult(
                status=UploadStatus.SKIPPED,
                message="没有可上传的文件，本次跳过",
                run_id=logger.run_id,
            )

        password = credentials.get_password(config.username) if config.username else ""

        # 有可复用的会话时，账号密码不是必需品：session.json 本身就是登录凭证。
        # 交互模式（人在屏幕前）不提前拦：没凭据可以到上传流程里转人工登录。
        has_session = bool(getattr(config, "reuse_session", True)) and session_path().exists()
        if not interactive and not has_session and not (config.username and password):
            raise UploadError(
                "既没有可复用的会话，也没有保存账号密码。"
                "请先到「设置」里填写，或跑一次 python main.py login",
                ErrorKind.FATAL,
            )

        report("run", "start", f"开始上传：{target.name}（第 {attempt} 次尝试）")
        message = upload_file(
            file_path=target,
            config=config,
            password=password or "",
            logger=logger,
            screenshot_dir=log_dir(),
            progress=progress,
            session_file=session_path(),
            interactive=interactive,
        )

        archived = archive_pending(target, success=True)
        duration = time.monotonic() - started
        report("run", "success", f"上传完成，耗时 {duration:.1f}s")
        return UploadResult(
            status=UploadStatus.SUCCESS,
            message=message or "上传成功",
            run_id=logger.run_id,
            file_path=str(target),
            archived_path=str(archived),
            duration_sec=duration,
            attempt=attempt,
        )

    except UploadError as exc:
        duration = time.monotonic() - started
        logger.fail("run", f"失败（{exc.kind.value}）：{exc.message}", detail=exc.detail)
        if progress is not None:
            try:
                progress("run", "fail", exc.message)
            except Exception:  # noqa: BLE001
                pass

        archived: Optional[Path] = None
        if target is not None and target.exists() and not exc.retryable:
            # 重试无意义，归档走人，避免明天又拿同一个坏文件重跑
            archived = archive_pending(target, success=False)

        return UploadResult(
            status=UploadStatus.FAILED,
            message=exc.message,
            run_id=logger.run_id,
            file_path=str(target) if target else None,
            archived_path=str(archived) if archived else None,
            detail=exc.detail,
            screenshot=exc.detail if (exc.detail or "").endswith(".png") else None,
            duration_sec=duration,
            attempt=attempt,
            retryable=exc.retryable,
        )

    except Exception as exc:  # noqa: BLE001 - 意外异常不能把调度器打死
        duration = time.monotonic() - started
        logger.fail("run", f"出现未预期错误：{exc}", exc_info=True)
        # 归为可重试：可能只是一次偶发崩溃，且有最大次数封顶
        return UploadResult(
            status=UploadStatus.FAILED,
            message=f"出现未预期错误：{exc}",
            run_id=logger.run_id,
            file_path=str(target) if target else None,
            duration_sec=duration,
            attempt=attempt,
            retryable=True,
        )

    finally:
        upload_lock.release()


class UploadScheduler:
    """包一层 APScheduler，对外只暴露「开/关自动上传」和「立即跑一次」。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        on_result: ResultCallback = None,
        progress: ProgressCallback = None,
    ) -> None:
        self.config = config or Config.load()
        self.on_result = on_result
        self.progress = progress
        self._scheduler = BackgroundScheduler()  # 默认跟随本机时区

    # ---- 生命周期 ----

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self.apply_config(self.config)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ---- 配置 ----

    def apply_config(self, config: Config) -> None:
        """配置变了就重建任务；关闭自动上传时一并取消掉排队中的重试。"""
        self.config = config
        self._remove_job(JOB_ID_DAILY)
        self._clear_retries()

        if not config.auto_upload_enabled:
            return

        hour, minute = config.schedule_hour_minute
        self._scheduler.add_job(
            self._run_scheduled,
            CronTrigger(hour=hour, minute=minute),
            id=JOB_ID_DAILY,
            replace_existing=True,
            coalesce=True,  # 电脑休眠错过多次触发，只补跑一次
            max_instances=1,
            misfire_grace_time=3600,  # 错过 1 小时以内仍然补跑
        )

    @property
    def next_run_time(self) -> Optional[datetime]:
        job = self._scheduler.get_job(JOB_ID_DAILY)
        return getattr(job, "next_run_time", None) if job else None

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # ---- 执行 ----

    def run_now(
        self,
        file_path: Optional[PathLike] = None,
        force_export: bool = False,
    ) -> UploadResult:
        """手动触发，同步执行（UI 侧放到子线程里调）。

        人在屏幕前，允许转人工登录（弹有头浏览器输验证码）。
        force_export=True 时忽略队列，强制从壳牌重新导出最新订单。
        """
        result = run_pipeline_once(
            self.config,
            attempt=1,
            file_path=file_path,
            progress=self.progress,
            interactive=True,
            force_export=force_export,
        )
        self._notify(result)
        return result

    def _run_scheduled(self) -> None:
        self._clear_retries()
        # 每天开跑前清掉隔天的残留文件，避免误传昨天没传成功的旧数据
        removed = clear_stale_pending()
        if removed and self.progress is not None:
            names = "、".join(p.name for p in removed)
            try:
                self.progress(
                    "queue", "info", f"已清理 {len(removed)} 个隔天残留文件：{names}"
                )
            except Exception:  # noqa: BLE001
                pass
        self._execute(attempt=1)

    def _execute(self, attempt: int) -> None:
        # 定时任务不传 interactive：没人值守时缺会话直接报错，不弹窗干等
        result = run_pipeline_once(self.config, attempt=attempt, progress=self.progress)

        if result.status is UploadStatus.FAILED and result.retryable:
            self._schedule_retry(attempt)

        self._notify(result)

    def _schedule_retry(self, attempt: int) -> None:
        delays = list(self.config.retry_delays_minutes or [])
        if attempt > len(delays):
            return
        delay = delays[attempt - 1]
        run_at = datetime.now() + timedelta(minutes=delay)
        self._scheduler.add_job(
            self._execute,
            DateTrigger(run_date=run_at),
            kwargs={"attempt": attempt + 1},
            id=f"{JOB_ID_RETRY_PREFIX}_{attempt}",
            replace_existing=True,
            misfire_grace_time=1800,
        )

    # ---- 内部 ----

    def _notify(self, result: UploadResult) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(result)
        except Exception:  # noqa: BLE001
            pass

    def _remove_job(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001 - 任务不存在是正常情况
            pass

    def _clear_retries(self) -> None:
        for job in list(self._scheduler.get_jobs()):
            if str(job.id).startswith(JOB_ID_RETRY_PREFIX):
                self._remove_job(job.id)
