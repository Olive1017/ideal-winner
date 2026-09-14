from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Union

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.models import ConvertError, UploadError, UploadResult, UploadStatus
from core.uploader import upload_file
from core.api_uploader import KEYRING_API_KEY, KEYRING_KEY_ID, upload_file_api

from . import credentials
from .config import (
    Config,
    archive_outbox,
    clear_stale_outbox,
    data_dir_configured,
    latest_outbox,
    sdcc_file_name,
    sdcc_orders_dir,
)
from .logger import RunLogger, new_run_id
from .single_instance import upload_lock

JOB_ID_DAILY = "daily_prepare"
JOB_ID_RETRY_PREFIX = "retry_prepare"

PathLike = Union[str, Path]
ProgressCallback = Optional[Callable[[str, str, str], None]]
ResultCallback = Optional[Callable[[UploadResult], None]]

NO_DATA_DIR_MSG = "尚未选择数据文件夹，请先在「运行」页选择"


def _export_and_convert(config: Config, logger, report) -> Path:
    """导出壳牌订单 -> 转换 -> 保存 SDCC 文件 -> 返回 SDCC 文件路径。"""
    from core.converter import convert
    from core.converter import export as export_xlsx
    from core.shell_exporter import export_orders

    report("export", "start", "从壳牌 LMS 导出订单")
    shell_user = config.shell_username
    shell_pwd = credentials.get_password(shell_user) if shell_user else ""
    # 导出直接落 壳牌订单/，无需再复制一份
    shell_file = export_orders(
        config=config,
        logger=logger,
        password=shell_pwd or "",
        progress=report,
    )
    report("export", "success", f"已保存原始壳牌订单：{shell_file.name}")

    report("convert", "start", "转换为 SDCC 导入格式")
    car_file = config.car_file or None
    result = convert(shell_file, car_file)
    for warning in result.warnings:
        report("convert", "warn", warning)

    target = sdcc_orders_dir() / sdcc_file_name(shell_file.name)
    export_xlsx(result.df, target)
    report(
        "convert",
        "success",
        f"转换完成，共 {result.row_count} 行，已保存到 {target.name}",
    )
    return target


def prepare_orders(
    config: Optional[Config] = None,
    run_id: Optional[str] = None,
    attempt: int = 1,
    progress: ProgressCallback = None,
    force_export: bool = False,
) -> UploadResult:
    """准备订单：壳牌导出 -> 保存 -> 转换 -> 保存 SDCC 文件；结束，不自动上传。"""
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

    # 没选数据文件夹就别跑，更不能往程序目录里创建数据文件夹
    if not config.data_dir.strip():
        report("run", "fail", NO_DATA_DIR_MSG)
        return UploadResult(
            status=UploadStatus.FAILED,
            message=NO_DATA_DIR_MSG,
            run_id=logger.run_id,
            retryable=False,
        )

    if not upload_lock.acquire():
        report("lock", "info", "已有任务在执行，本次跳过")
        return UploadResult(
            status=UploadStatus.SKIPPED,
            message="已有任务在执行，本次跳过",
            run_id=logger.run_id,
        )

    target: Optional[Path] = None
    try:
        if force_export:
            report("queue", "info", "强制重新导出：忽略已有 SDCC 文件，从壳牌拉取最新订单")
            target = _export_and_convert(config, logger, report)
        else:
            target = latest_outbox()
            if target is None:
                target = _export_and_convert(config, logger, report)

        if target is None or not target.exists():
            report("queue", "info", "没有可准备的 SDCC 文件，本次跳过")
            return UploadResult(
                status=UploadStatus.SKIPPED,
                message="没有可准备的 SDCC 文件，本次跳过",
                run_id=logger.run_id,
            )

        duration = time.monotonic() - started
        report("run", "success", f"订单准备完成，生成文件：{target.name}，耗时 {duration:.1f}s")
        return UploadResult(
            status=UploadStatus.SUCCESS,
            message=f"订单准备完成：{target.name}",
            run_id=logger.run_id,
            file_path=str(target),
            duration_sec=duration,
            attempt=attempt,
        )

    except ConvertError as exc:
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
    except UploadError as exc:
        logger.fail("run", f"准备失败（{exc.kind.value}）：{exc.message}", detail=exc.detail)
        return UploadResult(
            status=UploadStatus.FAILED,
            message=exc.message,
            run_id=logger.run_id,
            detail=exc.detail,
            duration_sec=time.monotonic() - started,
            attempt=attempt,
            retryable=exc.retryable,
        )
    except Exception as exc:  # noqa: BLE001
        logger.fail("run", f"出现未预期错误：{exc}", exc_info=True)
        return UploadResult(
            status=UploadStatus.FAILED,
            message=f"出现未预期错误：{exc}",
            run_id=logger.run_id,
            duration_sec=time.monotonic() - started,
            attempt=attempt,
            retryable=True,
        )
    finally:
        upload_lock.release()


def upload_order(
    config: Optional[Config] = None,
    file_path: Optional[PathLike] = None,
    progress: ProgressCallback = None,
    force_export: bool = False,
) -> UploadResult:
    """手动上传：指定文件 / 队列最新文件直接传；强制重导或队列为空时先「导出→转换」。

    上传段调用 uploader.py，交由用户手工登录。
    """
    config = config or Config.load()
    logger = RunLogger(new_run_id())
    started = time.monotonic()

    def report(step: str, status: str, message: str = "") -> None:
        logger.log(step, status, message)
        if progress is not None:
            try:
                progress(step, status, message)
            except Exception:  # noqa: BLE001
                pass

    if not config.data_dir.strip():
        report("run", "fail", NO_DATA_DIR_MSG)
        return UploadResult(
            status=UploadStatus.FAILED,
            message=NO_DATA_DIR_MSG,
            run_id=logger.run_id,
            retryable=False,
        )

    # 与定时任务互斥：两个浏览器同时登同一个 SDCC 账号会互踢。
    # FileLock 同线程可重入，下面调 prepare_orders 不会死锁。
    if not upload_lock.acquire():
        report("lock", "info", "已有任务在执行，本次跳过")
        return UploadResult(
            status=UploadStatus.SKIPPED,
            message="已有任务在执行，本次跳过",
            run_id=logger.run_id,
        )

    try:
        target: Optional[Path] = Path(file_path) if file_path is not None else None
        # 强制重新导出不看队列；队列为空时「立即准备订单」也要真的去导出，
        # 否则按钮名不副实——点了什么都没有发生
        if target is None and (force_export or latest_outbox() is None):
            prepared = prepare_orders(
                config=config, progress=progress, force_export=force_export
            )
            if prepared.status is not UploadStatus.SUCCESS or not prepared.file_path:
                return prepared
            target = Path(prepared.file_path)
        if target is None:
            target = latest_outbox()
        if target is None:
            return UploadResult(
                status=UploadStatus.SKIPPED,
                message="没有可上传的 SDCC 文件",
                run_id=logger.run_id,
                duration_sec=time.monotonic() - started,
            )
        if not target.exists():
            return UploadResult(
                status=UploadStatus.SKIPPED,
                message=f"文件不存在：{target}",
                run_id=logger.run_id,
                duration_sec=time.monotonic() - started,
            )

        try:
            report("run", "start", f"开始上传：{target.name}")
            if config.transfer_mode == "api":
                message = upload_file_api(
                    target,
                    config,
                    key_id=credentials.get_password(KEYRING_KEY_ID) or "",
                    api_key=credentials.get_password(KEYRING_API_KEY) or "",
                    logger=logger,
                    progress=progress,
                )
            else:
                message = upload_file(
                    file_path=target,
                    config=config,
                    logger=logger,
                    progress=progress,
                )
            archived = archive_outbox(target, success=True)
            duration = time.monotonic() - started
            report("run", "success", f"上传完成，耗时 {duration:.1f}s")
            return UploadResult(
                status=UploadStatus.SUCCESS,
                message=message or "上传成功",
                run_id=logger.run_id,
                file_path=str(target),
                archived_path=str(archived),
                duration_sec=duration,
            )
        except UploadError as exc:
            archived = archive_outbox(target, success=False) if target.exists() and not exc.retryable else None
            return UploadResult(
                status=UploadStatus.FAILED,
                message=exc.message,
                run_id=logger.run_id,
                file_path=str(target),
                archived_path=str(archived) if archived else None,
                detail=exc.detail,
                screenshot=exc.detail if (exc.detail or "").endswith(".png") else None,
                duration_sec=time.monotonic() - started,
                retryable=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001
            return UploadResult(
                status=UploadStatus.FAILED,
                message=f"出现未预期错误：{exc}",
                run_id=logger.run_id,
                file_path=str(target),
                duration_sec=time.monotonic() - started,
                retryable=True,
            )
    finally:
        upload_lock.release()


class UploadScheduler:
    """包一层 APScheduler，对外只暴露「开/关自动准备」和「立即跑一次」。"""

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

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self.apply_config(self.config)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def apply_config(self, config: Config) -> None:
        """配置变了就重建任务；关闭自动准备时一并取消掉排队中的重试。"""
        self.config = config
        self._remove_job(JOB_ID_DAILY)
        self._clear_retries()

        if not config.auto_prepare_enabled:
            return

        hour, minute = config.schedule_hour_minute
        self._scheduler.add_job(
            self._run_scheduled,
            CronTrigger(hour=hour, minute=minute),
            id=JOB_ID_DAILY,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )

    @property
    def next_run_time(self) -> Optional[datetime]:
        job = self._scheduler.get_job(JOB_ID_DAILY)
        return getattr(job, "next_run_time", None) if job else None

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def run_now(
        self,
        file_path: Optional[PathLike] = None,
        force_export: bool = False,
    ) -> UploadResult:
        """手动触发：只做“准备/上传”中的上传一段，走用户人工登录。"""
        result = upload_order(
            config=self.config,
            file_path=file_path,
            progress=self.progress,
            force_export=force_export,
        )
        self._notify(result)
        return result

    def _run_scheduled(self) -> None:
        self._clear_retries()
        # 未选数据文件夹时直接失败通知，不创建任何目录
        if not data_dir_configured():
            self._notify(
                UploadResult(
                    status=UploadStatus.FAILED,
                    message="尚未选择数据文件夹，自动准备已跳过；请在「运行」页选择",
                    run_id=new_run_id(),
                    retryable=False,
                )
            )
            return
        removed = clear_stale_outbox()
        if removed and self.progress is not None:
            names = "、".join(p.name for p in removed)
            try:
                self.progress("queue", "info", f"已清理 {len(removed)} 个隔天残留文件：{names}")
            except Exception:  # noqa: BLE001
                pass
        self._execute(attempt=1)

    def _execute(self, attempt: int) -> None:
        if self.config.transfer_mode == "api":
            # API 模式：定时任务全自动，导出 → 转换 → 上传一次跑完
            result = upload_order(self.config, progress=self.progress)
        else:
            result = prepare_orders(self.config, attempt=attempt, progress=self.progress)

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
        except Exception:  # noqa: BLE001
            pass

    def _clear_retries(self) -> None:
        for job in list(self._scheduler.get_jobs()):
            if str(job.id).startswith(JOB_ID_RETRY_PREFIX):
                self._remove_job(job.id)
