from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Union

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.models import ErrorKind, UploadError, UploadResult, UploadStatus
from core.uploader import upload_file

from . import credentials
from .config import Config, archive_pending, latest_pending, log_dir, session_path
from .logger import RunLogger, new_run_id
from .single_instance import upload_lock

JOB_ID_DAILY = "daily_upload"
JOB_ID_RETRY_PREFIX = "retry_upload"

PathLike = Union[str, Path]
ProgressCallback = Optional[Callable[[str, str, str], None]]
ResultCallback = Optional[Callable[[UploadResult], None]]


def run_upload_once(
    config: Optional[Config] = None,
    run_id: Optional[str] = None,
    attempt: int = 1,
    file_path: Optional[PathLike] = None,
    progress: ProgressCallback = None,
    interactive: bool = False,
) -> UploadResult:
   
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

    target = Path(file_path) if file_path else latest_pending()
    if target is None or not target.exists():
        report("queue", "info", "待上传目录为空，本次跳过")
        return UploadResult(
            status=UploadStatus.SKIPPED,
            message="待上传目录为空，本次跳过",
            run_id=logger.run_id,
        )

    # 同一个 SDCC 账号不能开两个会话，拿不到锁就直接跳过
    if not upload_lock.acquire():
        report("lock", "info", "已有上传任务在执行，本次跳过")
        return UploadResult(
            status=UploadStatus.SKIPPED,
            message="已有上传任务在执行，本次跳过",
            run_id=logger.run_id,
        )

    try:
        password = credentials.get_password(config.username) if config.username else ""

        # 有可复用的会话时，账号密码**不是必需品**：session.json 本身就是登录凭证。
        # 只有两者都没有，才真的无路可走。
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
        logger.fail("run", f"上传失败（{exc.kind.value}）：{exc.message}", detail=exc.detail)
        if progress is not None:
            try:
                progress("run", "fail", exc.message)
            except Exception:  # noqa: BLE001
                pass

        archived: Optional[Path] = None
        if not exc.retryable:
            # 重试无意义，归档走人，避免明天又拿同一个坏文件重跑
            archived = archive_pending(target, success=False)

        return UploadResult(
            status=UploadStatus.FAILED,
            message=exc.message,
            run_id=logger.run_id,
            file_path=str(target),
            archived_path=str(archived) if archived else None,
            detail=exc.detail,
            screenshot=exc.detail if (exc.detail or "").endswith(".png") else None,
            duration_sec=duration,
            attempt=attempt,
            retryable=exc.retryable,
        )

    except Exception as exc:  # noqa: BLE001 - 意外异常不能把调度器打死
        duration = time.monotonic() - started
        logger.fail("run", f"上传出现未预期错误：{exc}", exc_info=True)
        # 归为可重试：可能只是一次偶发崩溃，且有最大次数封顶
        return UploadResult(
            status=UploadStatus.FAILED,
            message=f"上传出现未预期错误：{exc}",
            run_id=logger.run_id,
            file_path=str(target),
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

    def run_now(self, file_path: Optional[PathLike] = None) -> UploadResult:
        """手动触发，同步执行（UI 侧放到子线程里调）。

        人在屏幕前，允许转人工登录（弹有头浏览器输验证码）。
        """
        result = run_upload_once(
            self.config,
            attempt=1,
            file_path=file_path,
            progress=self.progress,
            interactive=True,
        )
        self._notify(result)
        return result

    def _run_scheduled(self) -> None:
        self._clear_retries()
        self._execute(attempt=1)

    def _execute(self, attempt: int) -> None:
        # 定时任务不传 interactive：没人值守时缺会话直接报错，不弹窗干等
        result = run_upload_once(self.config, attempt=attempt, progress=self.progress)

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
