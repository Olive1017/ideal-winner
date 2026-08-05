import pandas as pd
import re
import os
from datetime import timedelta


class ShellConverter:
    """壳牌订单数据转换器"""

    def __init__(self, shell_file=None, car_file=None, output_file="SDCC导入版.xlsx"):
        """
        初始化转换器

        Args:
            shell_file: 壳牌订单文件路径
            car_file: 车型映射文件路径
            output_file: 输出文件路径（为 None 时不保存）
        """
        self.shell_file = shell_file
        self.car_file = car_file
        self.output_file = output_file
        self.df_output = None  # 保存转换后的数


    def _read_car_mapping(self):
        """读取车型映射"""
        try:
            df_car = pd.read_excel(self.car_file, dtype=str)
            df_car = df_car.rename(columns={"交运单": "交货单号", "车型": "车型"})

            def clean_delivery_no(s):
                if pd.isna(s):
                    return ""
                s = str(s).strip()
                s = s.lstrip("'")
                s = s.zfill(10)
                return s

            df_car["交货单号"] = df_car["交货单号"].apply(clean_delivery_no)
            car_dict = dict(zip(df_car["交货单号"], df_car["车型"]))
            return car_dict
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _extract_digits(self, s):
        """提取开头的数字串"""
        if pd.isna(s):
            return ""
        match = re.match(r"^\d+", str(s))
        return match.group(0) if match else ""

    def _extract_first_order_no(self, s):
        """
        提取第一个订单号（当有多个订单号时只保留第一个）

        支持的分隔符：逗号、空格、分号、顿号
        """
        if pd.isna(s):
            return ""

        s = str(s).strip()

        # 尝试多种常见分隔符
        separators = [",", " ", ";", "、"]

        for sep in separators:
            if sep in s:
                # 按分隔符分割，取第一个并去除前后空格
                first_order = s.split(sep)[0].strip()
                return first_order if first_order else s

        # 没有找到分隔符，返回原值
        return s

    def convert(self):
        """
        执行数据转换

        Returns:
            tuple: (success: bool, message: str, output_path: str)

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 数据格式错误
        """
        # 验证文件
        if not self.shell_file or not os.path.exists(self.shell_file):
            raise FileNotFoundError("壳牌订单文件不存在")

        # 读取壳牌数据
        df = pd.read_excel(self.shell_file, sheet_name="运单", dtype=str)

        # 读取车型映射
        car_dict = self._read_car_mapping()
        car_file_status = "成功" if car_dict else "未找到或为空"

       

        # 提取所需列
        actual_cols = {
            "运单号": "运单号",
            "交货单号": "交货单号",
            "SAP订单号": "SAP订单号",
            "发货方": "发货方",
            "目的地地址": "送达方地址",
            "物料": "物料",
            "发货量": "发货量",
            "送达方": "送达方",
            "要求到厂日期": "要求到厂日期"
        }
        needed_cols = list(actual_cols.values())
        df_sub = df[needed_cols].copy()

        df_sub["要求到厂日期"] = pd.to_datetime(df_sub["要求到厂日期"], errors="coerce")

        # 应用转换规则
        df_sub["客户订单时间"] = df_sub["要求到厂日期"] - timedelta(days=1)
        df_sub["计划发货时间"] = df_sub["要求到厂日期"] 
        df_sub["计划到达时间"] = df_sub["要求到厂日期"] + timedelta(days=1)

        #df_sub["客户订单号"] = df_sub["SAP订单号"]
        df_sub["客户订单号"] = df_sub["SAP订单号"].apply(self._extract_first_order_no)
        df_sub["客户销售单号"] = df_sub["交货单号"]
        df_sub["运单号"] = df_sub["运单号"]

        df_sub["物料编码"] = df_sub["物料"].apply(self._extract_digits)
        df_sub["物料描述"] = df_sub["物料"]

        df_sub["物料数量"] = pd.to_numeric(df_sub["发货量"], errors="coerce").fillna(0)
        df_sub["物料体积"] = df_sub["物料数量"] * 3
        df_sub["物料重量"] = df_sub["物料数量"] * 1000

        df_sub["始发地编号"] = df_sub["发货方"]
        df_sub["始发地名称"] = ""
        df_sub["目的地编号"] = ""
        df_sub["目的地名称"] = df_sub["送达方"]
        df_sub["目的地地址"] = df_sub["送达方地址"]

        df_sub["订单类型"] = "整车运输"

        df_sub["应收车型"] = df_sub["交货单号"].map(car_dict).fillna("未知")
        df_sub["应付车型"] = df_sub["交货单号"].map(car_dict).fillna("未知")

        # 构建输出
        output_columns = [
            "客户订单时间", "计划发货时间", "计划到达时间", "客户订单号", "客户销售单号",
            "运单号", "物料编码", "物料描述", "物料数量", "物料体积", "物料重量",
            "始发地编号", "目的地编号", "始发地名称", "目的地名称", "目的地地址",
            "订单类型", "应收车型", "应付车型"
        ]
        df_output = df_sub[output_columns].copy()

        # 日期格式化
        for col in ["客户订单时间", "计划发货时间", "计划到达时间"]:
            df_output[col] = pd.to_datetime(df_output[col]).dt.strftime("%Y-%m-%d")

        # 数值格式化
        for col in ["物料数量", "物料体积", "物料重量"]:
            df_output[col] = df_output[col].round(2)

        # 保存转换后的数据
        self.df_output = df_output

        # 如果有输出路径，则保存文件
        if self.output_file:
            df_output.to_excel(self.output_file, index=False)
            return (True,
                    f"转换成功！共 {len(df_output)} 行订单，车型映射: {car_file_status}",
                    self.output_file)
        else:
            return (True,
                    f"转换成功！共 {len(df_output)} 行订单，车型映射: {car_file_status}")


# ========== 主程序入口（命令行模式） ==========
if __name__ == "__main__":
    SHELL_FILE = "壳牌订单.xlsx"
    CAR_FILE = "车型文件.xlsx"
    OUTPUT_FILE = "SDCC导入模板.xlsx"

    converter = ShellConverter(SHELL_FILE, CAR_FILE, OUTPUT_FILE)
    try:
        success, message, output_path = converter.convert()
        print(message)
        print(f"输出文件: {output_path}")
    except Exception as e:
        print(f"转换失败: {e}")