"""SDCC API 直传：读队列 Excel，逐单 POST 到 SDCC 入单接口。

与 uploader.py（浏览器 RPA）并列的上传通道，由 scheduler.upload_order
按 config.transfer_mode 分发。

鉴权：header 带 keyId + apiKey，body 的 orderInfo 里带 dataSourceFrom，
网关直接鉴权，没有登录接口。keyId/apiKey 存 keyring，条目名见下方常量。

错误策略：网络/5xx 中止整批并抛 RETRYABLE（调度器重试整个文件，
同一 customerPoId 重推 = 改单，天然幂等）；业务校验失败逐单收集、继续跑，
部分成功算成功（与 RPA 策略一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import pandas as pd
import requests

from .models import ErrorKind, UploadError

PathLike = Union[str, Path]
ProgressCallback = Optional[Callable[[str, str, str], None]]

API_PATH = "/sdcc/v1/outerapi/com/comCustomerOrderSet"
REQUEST_TIMEOUT_SEC = 30

# keyring 条目名：设置页保存凭证时必须用这两个名字
KEYRING_KEY_ID = "sdcc_api_key_id"
KEYRING_API_KEY = "sdcc_api_key"

# 车型在 extendData 里的扩展字段（SDCC 方确认）
EXT_FIELD_CAR_RECEIVABLE = "tb_extend_field.extend_field08"  # 应收车型
EXT_FIELD_CAR_PAYABLE = "tb_extend_field.extend_field07"  # 应付车型


# --------------------------------------------------------------------------- #
# 单元格清洗
# --------------------------------------------------------------------------- #


def _text(value) -> str:
    """单元格 → 字符串，NaN/None 归一成空串。"""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _num(value) -> float:
    text = _text(value)
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _dt(value) -> str:
    """转换产物是 yyyy-MM-dd，接口要 yyyy-MM-dd HH:mm:ss，补零点后缀。"""
    text = _text(value)
    if not text:
        return ""
    return text if len(text) > 10 else f"{text} 00:00:00"


# --------------------------------------------------------------------------- #
# 报文组装
# --------------------------------------------------------------------------- #


def _build_payload(row: Dict, config) -> Dict:
    """一行 Excel → 一单报文（orderInfo + detailList）。"""
    order_info = {
        "dataSourceFrom": config.data_source_from,
        "customerPoId": _text(row.get("客户订单号")),
        "customerSaleNo": _text(row.get("客户销售单号")),
        "customerOrignialWaybillNo": _text(row.get("运单号")),
        "orderType": _text(row.get("订单类型")),
        "orderDate": _dt(row.get("客户订单时间")),
        "plannedDeliveryTime": _dt(row.get("计划发货时间")),
        "plannedArrivalTime": _dt(row.get("计划到达时间")),
        "sourceLocId": _text(row.get("始发地编号")),
        "sourceLocName": _text(row.get("始发地名称")),
        "destLocId": _text(row.get("目的地编号")),
        "destLocName": _text(row.get("目的地名称")),
        "destLocAddr": _text(row.get("目的地地址")),
        "itemCode": config.api_item_code,
        "itemDesc": config.project,
        "extendData": {
            EXT_FIELD_CAR_RECEIVABLE: _text(row.get("应收车型")),
            EXT_FIELD_CAR_PAYABLE: _text(row.get("应付车型")),
        },
    }
    detail = {
        "materialCode": _text(row.get("物料编码")),
        "materialDesc": _text(row.get("物料描述")),
        "materialName": _text(row.get("物料描述")),
        "quantity": _num(row.get("物料数量")),
        "volume": _num(row.get("物料体积")),
        "volumeUnit": "M3",
        "weight": _num(row.get("物料重量")),
        "weightUnit": "KG",
        "packingUnit": "件",
    }
    return {"orderInfo": order_info, "detailList": [detail]}


# --------------------------------------------------------------------------- #
# 发单
# --------------------------------------------------------------------------- #


def _post_order(payload: Dict, api_key: str, base_url: str) -> str:
    """发一单，成功返回 SDCC 单号；失败抛 UploadError。"""
    url = base_url.rstrip("/") + API_PATH
    headers = {"apiKey": api_key}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise UploadError(f"网络异常：{exc}", kind=ErrorKind.RETRYABLE) from exc

    if resp.status_code in (401, 403):
        raise UploadError(
            f"鉴权失败（HTTP {resp.status_code}），请检查 apiKey",
            kind=ErrorKind.FATAL,
            detail=resp.text[:500],
        )
    if resp.status_code >= 500:
        raise UploadError(
            f"SDCC 服务端异常（HTTP {resp.status_code}）",
            kind=ErrorKind.RETRYABLE,
            detail=resp.text[:500],
        )
    if resp.status_code != 200:
        raise UploadError(
            f"请求被拒绝（HTTP {resp.status_code}）",
            kind=ErrorKind.FATAL,
            detail=resp.text[:500],
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise UploadError(
            "响应不是合法 JSON",
            kind=ErrorKind.RETRYABLE,
            detail=resp.text[:500],
        ) from exc

    if body.get("success") is True or body.get("code") == 0:
        return str(body.get("data") or "")
    raise UploadError(
        f"业务校验失败：{body.get('message')}",
        kind=ErrorKind.FATAL,
        detail=resp.text[:500],
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def upload_file_api(
    file_path: PathLike,
    config,
    key_id: str,
    api_key: str,
    logger=None,
    progress: ProgressCallback = None,
) -> str:
    """API 通道上传一份队列 Excel：逐单推送，聚合结果。

    Returns: 展示给用户的结果文案。
    Raises: UploadError —— 网络中断 / 鉴权失败 / 全部业务校验失败。
    """

    def report(step: str, status: str, message: str = "") -> None:
        if logger is not None:
            logger.log(step, status, message)
        if progress is not None:
            try:
                progress(step, status, message)
            except Exception:  # noqa: BLE001
                pass

    if not api_key:
        raise UploadError("缺少 API 凭证（apiKey），请先在设置页保存", kind=ErrorKind.FATAL)
    if not config.data_source_from:
        raise UploadError("缺少 dataSourceFrom，请先在设置页填写", kind=ErrorKind.FATAL)

    path = Path(file_path)
    df = pd.read_excel(path)
    total = len(df)
    if total == 0:
        raise UploadError(f"{path.name} 里没有数据行", kind=ErrorKind.FATAL)

    succeeded: List[str] = []
    failed: List[str] = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        order_no = _text(row.get("客户订单号"))
        report("upload", "start", f"[{i}/{total}] 推送订单 {order_no}")
        payload = _build_payload(row.to_dict(), config)
        try:
            sdcc_no = _post_order(payload, api_key, config.api_base_url)
        except UploadError as exc:
            if exc.retryable or "鉴权" in exc.message:
                # 网络/鉴权问题：剩余单打了也白打，整批中止交给调度器重试
                if logger is not None:
                    logger.fail("upload", f"订单 {order_no} 推送中止：{exc.message}", detail=exc.detail)
                raise
            failed.append(f"{order_no}：{exc.message}")
            report("upload", "fail", f"[{i}/{total}] {order_no} 失败：{exc.message}")
            continue
        succeeded.append(sdcc_no)
        report("upload", "success", f"[{i}/{total}] {order_no} → {sdcc_no}")

    if not failed:
        return f"API 推送完成：{total} 单全部成功"
    if not succeeded:
        raise UploadError(
            f"全部 {total} 单业务校验失败：{failed[0]} 等",
            kind=ErrorKind.FATAL,
            detail="\n".join(failed[:10]),
        )
    # 部分成功：算成功并在文案里列出失败单（重推=改单，之后手动重传也安全）
    return f"API 推送完成：成功 {len(succeeded)} 单，失败 {len(failed)} 单（{'；'.join(failed[:3])}）"


if __name__ == "__main__":  # pragma: no cover - UAT 手动测试入口
    import argparse
    from types import SimpleNamespace

    parser = argparse.ArgumentParser(description="SDCC API 入单推送（命令行测试）")
    parser.add_argument("file", help="SDCC订单 Excel 路径")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--data-source-from", required=True)
    parser.add_argument("--item-code", required=True)
    parser.add_argument("--base-url", default="https://apitest.i.sinotrans.com")
    parser.add_argument("--project", default="中海壳牌深圳")
    args = parser.parse_args()

    config = SimpleNamespace(
        api_base_url=args.base_url,
        data_source_from=args.data_source_from,
        api_item_code=args.item_code,
        project=args.project,
    )
    try:
        message = upload_file_api(args.file, config, key_id=args.key_id, api_key=args.api_key)
    except UploadError as error:
        raise SystemExit(f"推送失败：{error}")
    print(message)
