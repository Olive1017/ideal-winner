from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd

from .models import ConvertError, ConvertResult

# --------------------------------------------------------------------------- #
# 常量：源表结构
# --------------------------------------------------------------------------- #

SHEET_NAME = "运单"

REQUIRED_SOURCE_COLUMNS = [
    "运单号",
    "交货单号",
    "SAP订单号",
    "发货方",
    "送达方地址",
    "物料",
    "发货量",
    "送达方",
    "要求到厂日期",
]

# 车型表历史上用过「交运单」这个表头，两个都认
CAR_DELIVERY_ALIASES = ("交货单号", "交运单")
CAR_TYPE_ALIASES = ("车型",)

# --------------------------------------------------------------------------- #
# 常量：输出结构（顺序即 SDCC 导入模板的列顺序，不要随意调整）
# --------------------------------------------------------------------------- #

OUTPUT_COLUMNS = [
    "客户订单时间",
    "计划发货时间",
    "计划到达时间",
    "客户订单号",
    "客户销售单号",
    "运单号",
    "物料编码",
    "物料描述",
    "物料数量",
    "物料体积",
    "物料重量",
    "始发地编号",
    "目的地编号",
    "始发地名称",
    "目的地名称",
    "目的地地址",
    "订单类型",
    "应收车型",
    "应付车型",
]

DATE_COLUMNS = ("客户订单时间", "计划发货时间", "计划到达时间")
NUMERIC_COLUMNS = ("物料数量", "物料体积", "物料重量")

# --------------------------------------------------------------------------- #
# 常量：业务规则
# --------------------------------------------------------------------------- #

# 三个时间都以源表的「要求到厂日期」为基准做天数偏移
OFFSET_ORDER_TIME_DAYS = -1  # 客户订单时间
OFFSET_SHIP_TIME_DAYS = 0  # 计划发货时间
OFFSET_ARRIVE_TIME_DAYS = 1  # 计划到达时间

VOLUME_PER_UNIT = 3  # 物料体积 = 物料数量 × 3
WEIGHT_PER_UNIT = 1000  # 物料重量 = 物料数量 × 1000

ORDER_TYPE = "整车运输"
UNKNOWN_CAR_TYPE = "未知"
DELIVERY_NO_WIDTH = 10  # 交货单号补零后的位数
DATE_FORMAT = "%Y-%m-%d"

# 一个单元格里塞多个订单号时的分隔符（含全角）
_ORDER_NO_SPLIT_RE = re.compile(r"[,\uFF0C;\uFF1B\u3001\s]+")

# 绝对不允许出现在上传文件里的占位字符串
_PLACEHOLDER_VALUES = {"NaT", "nat", "nan", "NaN", "None", "<NA>"}

PathLike = Union[str, Path]


# --------------------------------------------------------------------------- #
# 字段清洗
# --------------------------------------------------------------------------- #


def clean_text(value) -> str:
    """去掉 NaN、首尾空白，以及 Excel 文本单元格常见的前导单引号。"""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    while text.startswith("'"):
        text = text[1:].strip()
    return text


def delivery_key(value) -> str:
    """交货单号的匹配键。

    车型映射表和订单表两侧必须都走这个函数，否则会匹配不上并静默变成「未知」。
    这正是旧版最隐蔽的一个 bug：车型表补零到 10 位，订单表却拿原值去 map。
    """
    text = clean_text(value)
    if not text:
        return ""
    # Excel 把单号读成数字时会带上 .0 尾巴
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(DELIVERY_NO_WIDTH) if text.isdigit() else text


def extract_leading_digits(value) -> str:
    """物料编码：取开头连续的数字串。"""
    match = re.match(r"^\d+", clean_text(value))
    return match.group(0) if match else ""


def extract_first_order_no(value) -> str:
    """一个单元格有多个订单号时只保留第一个。"""
    text = clean_text(value)
    if not text:
        return ""
    parts = [p for p in _ORDER_NO_SPLIT_RE.split(text) if p]
    return parts[0] if parts else text


# --------------------------------------------------------------------------- #
# 车型映射
# --------------------------------------------------------------------------- #


def _read_car_mapping(car_file: Optional[PathLike]) -> Tuple[Dict[str, str], List[str]]:
    """读取车型映射表。

    车型映射文件是可选的；但一旦提供了却读不出来，就必须报错而不是静默返回空字典
    ——否则整批订单的车型都会变成「未知」并被直接上传。
    """
    warnings: List[str] = []

    if not car_file:
        warnings.append(f"未提供车型映射文件，所有订单车型将填「{UNKNOWN_CAR_TYPE}」")
        return {}, warnings

    path = Path(car_file)
    if not path.exists():
        raise ConvertError(f"车型映射文件不存在：{path}")

    try:
        df = pd.read_excel(path, dtype=str)
    except Exception as exc:  # noqa: BLE001 - 需要把底层原因原样透出给用户
        raise ConvertError(f"车型映射文件读取失败：{exc}") from exc

    df.columns = [str(col).strip() for col in df.columns]

    delivery_col = next((c for c in CAR_DELIVERY_ALIASES if c in df.columns), None)
    type_col = next((c for c in CAR_TYPE_ALIASES if c in df.columns), None)
    if delivery_col is None or type_col is None:
        raise ConvertError(
            "车型映射文件缺少必需列。需要交货单号列（"
            + " 或 ".join(CAR_DELIVERY_ALIASES)
            + f"）和「{CAR_TYPE_ALIASES[0]}」列；实际列为："
            + "、".join(df.columns)
        )

    mapping: Dict[str, str] = {}
    conflicts = 0
    for raw_key, raw_type in zip(df[delivery_col], df[type_col]):
        key = delivery_key(raw_key)
        car_type = clean_text(raw_type)
        if not key or not car_type:
            continue
        if key in mapping and mapping[key] != car_type:
            conflicts += 1
        mapping[key] = car_type

    if not mapping:
        warnings.append(f"车型映射文件没有有效数据，所有订单车型将填「{UNKNOWN_CAR_TYPE}」")
    if conflicts:
        warnings.append(f"车型映射文件有 {conflicts} 个交货单号重复且车型不一致，已取最后一条")

    return mapping, warnings


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _excel_row_numbers(index) -> List[int]:
    """把 DataFrame 的 0 基索引换算成 Excel 行号（第 1 行是表头）。"""
    return [int(i) + 2 for i in index]


def _assert_no_placeholder_text(df: pd.DataFrame) -> None:
    """兜底检查：不允许 NaT / nan 这类占位字符串被写进上传文件。"""
    for col in df.columns:
        if df[col].dtype != object:
            continue
        hits = df[col].astype(str).str.strip().isin(_PLACEHOLDER_VALUES)
        if hits.any():
            rows = _excel_row_numbers(df.index[hits][:10])
            raise ConvertError(
                f"列「{col}」存在无效值（NaT/nan 等），请检查源数据。"
                f"Excel 行号示例：{rows}"
            )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def convert(shell_file: PathLike, car_file: Optional[PathLike] = None) -> ConvertResult:
    """执行转换，只在内存里产出结果。

    Args:
        shell_file: 壳牌订单 Excel 路径。
        car_file: 车型映射 Excel 路径，可为 None。

    Returns:
        ConvertResult，含 DataFrame、行数统计和告警列表。

    Raises:
        ConvertError: 所有可预期的数据/格式问题，message 可直接展示给用户。
    """
    if not shell_file:
        raise ConvertError("请先选择壳牌订单文件")

    shell_path = Path(shell_file)
    if not shell_path.exists():
        raise ConvertError(f"壳牌订单文件不存在：{shell_path}")

    try:
        df = pd.read_excel(shell_path, sheet_name=SHEET_NAME, dtype=str)
    except ValueError as exc:
        raise ConvertError(
            f"未能读取工作表「{SHEET_NAME}」，请确认文件里存在该工作表。原始错误：{exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConvertError(f"壳牌订单文件读取失败：{exc}") from exc

    df.columns = [str(col).strip() for col in df.columns]

    missing = [c for c in REQUIRED_SOURCE_COLUMNS if c not in df.columns]
    if missing:
        raise ConvertError(
            "壳牌订单文件缺少必需列："
            + "、".join(missing)
            + "。实际列为："
            + "、".join(str(c) for c in df.columns)
        )

    src = df[REQUIRED_SOURCE_COLUMNS].copy()
    src = src.dropna(how="all").reset_index(drop=True)
    if src.empty:
        raise ConvertError("壳牌订单文件里没有可转换的数据行")

    car_map, warnings = _read_car_mapping(car_file)

    # ---- 日期：解析失败必须拦下，不能让 NaT 流到输出 ----
    arrival = pd.to_datetime(src["要求到厂日期"], errors="coerce")
    invalid = arrival.isna()
    if invalid.any():
        rows = _excel_row_numbers(src.index[invalid][:10])
        raise ConvertError(
            f"有 {int(invalid.sum())} 行的「要求到厂日期」无法解析，请先修正后再转换。"
            f"Excel 行号示例：{rows}"
        )

    out = pd.DataFrame(index=src.index)

    out["客户订单时间"] = arrival + timedelta(days=OFFSET_ORDER_TIME_DAYS)
    out["计划发货时间"] = arrival + timedelta(days=OFFSET_SHIP_TIME_DAYS)
    out["计划到达时间"] = arrival + timedelta(days=OFFSET_ARRIVE_TIME_DAYS)

    out["客户订单号"] = src["SAP订单号"].map(extract_first_order_no)

    # 展示值保持原样（只去空白和单引号），补零后的值仅用于匹配车型，
    # 避免改变实际上传到 SDCC 的单号内容。
    delivery_display = src["交货单号"].map(clean_text)
    delivery_lookup = src["交货单号"].map(delivery_key)
    out["客户销售单号"] = delivery_display

    out["运单号"] = src["运单号"].map(clean_text)
    out["物料编码"] = src["物料"].map(extract_leading_digits)
    out["物料描述"] = src["物料"].map(clean_text)

    quantity = pd.to_numeric(src["发货量"], errors="coerce").fillna(0)
    out["物料数量"] = quantity
    out["物料体积"] = quantity * VOLUME_PER_UNIT
    out["物料重量"] = quantity * WEIGHT_PER_UNIT

    out["始发地编号"] = src["发货方"].map(clean_text)
    out["始发地名称"] = ""
    out["目的地编号"] = ""
    out["目的地名称"] = src["送达方"].map(clean_text)
    out["目的地地址"] = src["送达方地址"].map(clean_text)
    out["订单类型"] = ORDER_TYPE

    if car_map:
        car_series = delivery_lookup.map(car_map)
    else:
        car_series = pd.Series([None] * len(src), index=src.index, dtype="object")

    matched_mask = car_series.notna()
    car_series = car_series.where(matched_mask, UNKNOWN_CAR_TYPE)
    out["应收车型"] = car_series
    out["应付车型"] = car_series

    car_matched = int(matched_mask.sum())
    car_unmatched = int(len(src) - car_matched)
    unmatched_samples = [
        str(v) for v in delivery_display[~matched_mask].head(5).tolist()
    ]
    if car_map and car_unmatched:
        message = f"{car_unmatched} 行未匹配到车型，已填「{UNKNOWN_CAR_TYPE}」"
        if unmatched_samples:
            message += "，例如交货单号：" + "、".join(unmatched_samples)
        warnings.append(message)

    out = out[OUTPUT_COLUMNS].copy()

    for col in DATE_COLUMNS:
        out[col] = out[col].dt.strftime(DATE_FORMAT)
    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).round(2)

    _assert_no_placeholder_text(out)

    return ConvertResult(
        df=out.reset_index(drop=True),
        row_count=len(out),
        car_matched=car_matched,
        car_unmatched=car_unmatched,
        unmatched_samples=unmatched_samples,
        warnings=warnings,
    )


def export(df: pd.DataFrame, output_path: PathLike) -> Path:
    """把转换结果写成 Excel。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(path, index=False)
    return path


def convert_and_export(
    shell_file: PathLike,
    car_file: Optional[PathLike],
    output_path: PathLike,
) -> ConvertResult:
    """转换并直接落盘，命令行模式用。"""
    result = convert(shell_file, car_file)
    export(result.df, output_path)
    return result


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="壳牌订单 → SDCC 导入格式")
    parser.add_argument("shell_file", help="壳牌订单 Excel 路径")
    parser.add_argument("-c", "--car-file", default=None, help="车型映射 Excel 路径")
    parser.add_argument("-o", "--output", default="SDCC导入版.xlsx", help="输出路径")
    args = parser.parse_args()

    try:
        cli_result = convert(args.shell_file, args.car_file)
    except ConvertError as error:
        raise SystemExit(f"转换失败：{error}")

    export(cli_result.df, args.output)
    print(cli_result.summary())
    for warning in cli_result.warnings:
        print(f"[警告] {warning}")
    print(f"已输出：{args.output}")
