import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import os
from convert import ShellConverter


class ShellConverterUI:
    """壳牌订单转换器界面"""

    def __init__(self):
        # 设置主题
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("green")

        # 创建主窗口
        self.root = ctk.CTk()
        self.root.title("壳牌订单转换工具")
        self.root.geometry("650x500")
        self.root.resizable(False, False)

        # 文件路径
        self.shell_file_path = ctk.StringVar(value="")
        self.car_file_path = ctk.StringVar(value="")
        self.output_file_path = ctk.StringVar(value="")
        self.converted_success = False  # 标记是否已成功转换

        # 创建界面
        self._create_widgets()

    def _create_widgets(self):
        """创建界面组件"""
        # 主容器
        main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        # 标题
        title = ctk.CTkLabel(
            main_frame,
            text="壳牌订单转换",
            font=ctk.CTkFont(size=24, weight="bold")
        )
        title.pack(pady=(0, 25))

        # 文件选择区
        file_frame = ctk.CTkFrame(main_frame, corner_radius=12)
        file_frame.pack(fill="x", pady=(0, 20))

        # 壳牌订单文件
        self._create_file_selector(
            file_frame,
            "壳牌订单文件",
            self.shell_file_path,
            lambda: self._select_file([("Excel文件", "*.xlsx"), ("所有文件", "*.*")], "shell_file"),
            pady=(0, 10),
            button_text="上传"
        )

        # 车型映射文件
        self._create_file_selector(
            file_frame,
            "车型映射文件",
            self.car_file_path,
            lambda: self._select_file([("Excel文件", "*.xlsx"), ("所有文件", "*.*")], "car_file"),
            pady=(0, 0),
            button_text="上传"
        )

        # 操作区
        action_frame = ctk.CTkFrame(main_frame, corner_radius=12)
        action_frame.pack(fill="x", pady=(0, 20))

        # 转换按钮
        self.convert_btn = ctk.CTkButton(
            action_frame,
            text="开始转换",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=45,
            command=self._on_convert
        )
        self.convert_btn.pack(fill="x", padx=20, pady=(20, 10))

        # 导出按钮
        self.export_btn = ctk.CTkButton(
            action_frame,
            text="导出",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=45,
            command=self._on_export,
            state="disabled"
        )
        self.export_btn.pack(fill="x", padx=20, pady=(0, 20))

        # 进度条
        self.progress_bar = ctk.CTkProgressBar(action_frame, height=6)
        self.progress_bar.pack(fill="x", padx=20, pady=(0, 20))
        self.progress_bar.set(0)

        # 状态区
        self.status_label = ctk.CTkLabel(
            action_frame,
            text="准备就绪，请选择文件后点击转换",
            font=ctk.CTkFont(size=12),
            text_color="gray"
        )
        self.status_label.pack(pady=(0, 10))

        # 结果区
        self.result_frame = ctk.CTkFrame(main_frame, corner_radius=12)
        self.result_frame.pack(fill="x", pady=(0, 10))

        self.result_label = ctk.CTkLabel(
            self.result_frame,
            text="",
            font=ctk.CTkFont(size=13),
            wraplength=550
        )
        self.result_label.pack(padx=20, pady=20)

    def _create_file_selector(self, parent, label_text, var, command, pady=(0, 10), button_text="浏览"):
        """创建文件选择器组件"""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=pady)

        label = ctk.CTkLabel(frame, text=label_text, font=ctk.CTkFont(size=14, weight="bold"))
        label.pack(anchor="w", pady=(0, 8))

        entry_frame = ctk.CTkFrame(frame, fg_color="transparent")
        entry_frame.pack(fill="x")

        entry = ctk.CTkEntry(entry_frame, textvariable=var, placeholder_text="未选择文件")
        entry.pack(side="left", fill="x", expand=True)

        btn = ctk.CTkButton(
            entry_frame,
            text=button_text,
            width=80,
            command=command
        )
        btn.pack(side="right", padx=(10, 0))

    def _select_file(self, filetypes, file_type):
        """选择文件（上传）"""
        path = filedialog.askopenfilename(
            title="选择文件",
            filetypes=filetypes
        )

        if path:
            if file_type == "shell_file":
                self.shell_file_path.set(path)
            elif file_type == "car_file":
                self.car_file_path.set(path)

    def _select_output_file(self):
        """选择输出文件路径（导出）"""
        path = filedialog.asksaveasfilename(
            title="选择保存位置",
            filetypes=[("Excel文件", "*.xlsx"), ("所有文件", "*.*")],
            defaultextension=".xlsx",
            initialfile="SDCC导入版.xlsx"
        )

        if path:
            # 确保路径是绝对路径
            path = os.path.abspath(path)
            self.output_file_path.set(path)
            print(f"已选择输出路径: {path}")  # 调试信息

    def _on_convert(self):
        """转换按钮点击事件"""
        shell_file = self.shell_file_path.get()
        car_file = self.car_file_path.get()

        # 验证文件
        if not shell_file:
            messagebox.showwarning("提示", "请选择壳牌订单文件")
            return

        if not os.path.exists(shell_file):
            messagebox.showerror("错误", "壳牌订单文件不存在")
            return

        # 禁用转换按钮
        self.convert_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self.progress_bar.set(0.3)
        self.status_label.configure(text="正在转换...")
        self.result_label.configure(text="")
        self.root.update()

        try:
            # 执行转换（不保存文件，只转换数据）
            converter = ShellConverter(shell_file, car_file, None)
            # 我们需要修改 convert 方法来返回 DataFrame 而不是直接保存
            success, message = converter.convert()

            if success:
                self.converted_data = converter.df_output  # 保存转换后的数据
                self.progress_bar.set(1)
                self.status_label.configure(text="转换完成！请点击导出按钮保存文件", text_color="green")
                self.result_label.configure(text=message)
                self.export_btn.configure(state="normal")  # 启用导出按钮
                self.converted_success = True
            else:
                self.progress_bar.set(0)
                self.status_label.configure(text="转换失败", text_color="red")
                self.result_label.configure(text=message)

        except FileNotFoundError as e:
            self.progress_bar.set(0)
            self.status_label.configure(text="转换失败", text_color="red")
            self.result_label.configure(text=f"错误: {e}")

        except ValueError as e:
            self.progress_bar.set(0)
            self.status_label.configure(text="转换失败", text_color="red")
            self.result_label.configure(text=f"数据错误: {e}")

        except Exception as e:
            self.progress_bar.set(0)
            self.status_label.configure(text="转换失败", text_color="red")
            self.result_label.configure(text=f"未知错误: {e}")

        finally:
            self.convert_btn.configure(state="normal")

    def _on_export(self):
        """导出按钮点击事件"""
        if not self.converted_success or not hasattr(self, 'converted_data'):
            messagebox.showwarning("提示", "请先完成转换再导出")
            return

        # 让用户选择保存路径
        path = filedialog.asksaveasfilename(
            title="选择保存位置",
            filetypes=[("Excel文件", "*.xlsx"), ("所有文件", "*.*")],
            defaultextension=".xlsx",
            initialfile="SDCC导入版.xlsx"
        )

        if path:
            try:
                path = os.path.abspath(path)
                self.converted_data.to_excel(path, index=False)
                messagebox.showinfo("成功", f"文件已保存到：\n{path}")
                self.status_label.configure(text=f"已导出到: {path}", text_color="green")
            except Exception as e:
                messagebox.showerror("错误", f"保存文件失败：{e}")

    def run(self):
        """运行界面"""
        self.root.mainloop()


# ========== 主程序入口 ==========
if __name__ == "__main__":
    app = ShellConverterUI()
    app.run()
