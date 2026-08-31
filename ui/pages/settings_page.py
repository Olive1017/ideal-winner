"""设置页：账号密码、壳牌导出、项目模板、数据文件夹、浏览器与开机自启。

密码不进配置文件，只进 Windows 凭据管理器；输入框里也不回显原密码，
只用一个「已保存」状态提示。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
)

from services import autostart, credentials
from services.config import Config

EXCEL_FILTER = "Excel 文件 (*.xlsx *.xls)"


class SettingsPage(QScrollArea):
    """设置页。保存后把新 Config 发给主窗口，由主窗口去重建调度器。"""

    configSaved = Signal(object)  # Config

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)

        self.setObjectName("settingsPage")

        # 只增加滚动能力，不改变原来的页面布局
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._config = config

        # 原来的内容全部放进滚动区域
        self._content = QWidget()
        self.setWidget(self._content)

        self._build_ui()
        self.load_config(config)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("设置", self._content))
        layout.addWidget(self._build_shell_card())
        layout.addWidget(self._build_import_card())
        layout.addWidget(self._build_advanced_card())

        buttons = QHBoxLayout()

        self.save_btn = PrimaryPushButton("保存设置", self._content)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(self.save_btn)

        reset_btn = PushButton("恢复默认值", self._content)
        reset_btn.clicked.connect(self._reset_defaults)
        buttons.addWidget(reset_btn)

        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addStretch(1)

    def _build_shell_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(10)
        inner.addWidget(StrongBodyLabel("壳牌 LMS（导出源数据）", card))

        form = QFormLayout()
        form.setSpacing(10)

        self.shell_username_edit = LineEdit(card)
        self.shell_username_edit.setPlaceholderText("壳牌 LMS 账号")
        self.shell_username_edit.editingFinished.connect(
            self._refresh_password_hint
        )
        form.addRow(BodyLabel("账号", card), self.shell_username_edit)

        self.shell_password_edit = PasswordLineEdit(card)
        self.shell_password_edit.setPlaceholderText("留空则不修改已保存的密码")
        form.addRow(BodyLabel("密码", card), self.shell_password_edit)

        self.shell_login_url_edit = LineEdit(card)
        form.addRow(BodyLabel("登录地址", card), self.shell_login_url_edit)

        self.export_region_edit = LineEdit(card)
        self.export_region_edit.setPlaceholderText("如：粤西粤北区域")
        form.addRow(BodyLabel("导出区域", card), self.export_region_edit)

        inner.addLayout(form)

        # 车型映射文件：路径 + 浏览
        car_row = QHBoxLayout()
        car_row.setSpacing(8)

        car_row.addWidget(BodyLabel("车型表", card))

        self.car_file_edit = LineEdit(card)
        self.car_file_edit.setPlaceholderText(
            "车型映射 Excel（可留空，车型将全部填「未知」）"
        )
        car_row.addWidget(self.car_file_edit, 1)

        car_browse = PushButton("浏览", card)
        car_browse.clicked.connect(self._browse_car_file)
        car_row.addWidget(car_browse)

        inner.addLayout(car_row)

        self.shell_password_hint = CaptionLabel("", card)
        inner.addWidget(self.shell_password_hint)

        inner.addWidget(
            CaptionLabel(
                "壳牌和 SDCC 是两套独立账号；密码同样只存凭据管理器",
                card,
            )
        )

        return card

    def _browse_car_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择车型映射文件",
            "",
            EXCEL_FILTER,
        )

        if path:
            self.car_file_edit.setText(path)

    def _build_import_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(10)
        inner.addWidget(StrongBodyLabel("导入参数", card))

        form = QFormLayout()
        form.setSpacing(10)

        self.project_edit = LineEdit(card)
        form.addRow(BodyLabel("项目", card), self.project_edit)

        self.template_edit = LineEdit(card)
        form.addRow(BodyLabel("模板", card), self.template_edit)

        self.login_url_edit = LineEdit(card)
        form.addRow(BodyLabel("登录地址", card), self.login_url_edit)

        inner.addLayout(form)

        inner.addWidget(
            CaptionLabel(
                "需与 SDCC 导入弹窗里下拉选项的文字完全一致，否则会选不中",
                card,
            )
        )

        return card

    def _build_advanced_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(12)
        inner.addWidget(StrongBodyLabel("运行方式", card))

        # 数据文件夹：壳牌订单、转换结果、归档的统一存放处
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        dir_row.addWidget(BodyLabel("数据文件夹", card))

        self.data_dir_edit = LineEdit(card)
        self.data_dir_edit.setPlaceholderText(
            "留空则用默认目录（用户目录下的 SDCC订单工具）"
        )
        dir_row.addWidget(self.data_dir_edit, 1)

        dir_browse = PushButton("浏览", card)
        dir_browse.clicked.connect(self._browse_data_dir)
        dir_row.addWidget(dir_browse)

        inner.addLayout(dir_row)
        inner.addWidget(
            CaptionLabel(
                "壳牌订单、转换后的 SDCC 订单和归档都会放进这个文件夹，车型表也建议放这里。"
                "别选 OneDrive/坚果云等同步盘，文件锁和 Excel 读写容易出怪问题",
                card,
            )
        )

        self.headless_switch = self._switch_row(
            card,
            inner,
            "后台静默运行",
            "开启后不弹出浏览器窗口。首次调试建议先关着，方便看到卡在哪一步",
        )

        self.autostart_switch = self._switch_row(
            card,
            inner,
            "开机自启",
            "开机后自动驻留托盘，不弹窗口（仅 Windows）",
        )

        if not autostart.supported():
            self.autostart_switch.setEnabled(False)

        self.tray_switch = self._switch_row(
            card,
            inner,
            "关闭窗口时最小化到托盘",
            "关掉后定时任务仍然生效；关闭本项则点叉直接退出程序",
        )

        return card

    def _browse_data_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择数据文件夹")
        if path:
            self.data_dir_edit.setText(path)

    def _switch_row(
        self,
        card,
        layout,
        title: str,
        caption: str,
    ) -> SwitchButton:
        row = QHBoxLayout()

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(BodyLabel(title, card))
        text.addWidget(CaptionLabel(caption, card))

        row.addLayout(text)
        row.addStretch(1)

        switch = SwitchButton(card)
        switch.setOnText("开")
        switch.setOffText("关")
        row.addWidget(switch)

        layout.addLayout(row)

        return switch

    # -------------------------------------------------------------- 数据
    def load_config(self, config: Config) -> None:
        self._config = config

        self.shell_username_edit.setText(config.shell_username)
        self.shell_login_url_edit.setText(config.shell_login_url)
        self.export_region_edit.setText(config.export_region)
        self.car_file_edit.setText(config.car_file)

        self.project_edit.setText(config.project)
        self.template_edit.setText(config.template)
        self.login_url_edit.setText(config.login_url)

        self.data_dir_edit.setText(config.data_dir)

        self.headless_switch.setChecked(config.headless)
        self.tray_switch.setChecked(config.minimize_to_tray)
        self.autostart_switch.setChecked(autostart.is_enabled())

        self._refresh_password_hint()

    def _refresh_password_hint(self) -> None:
        if not credentials.available():
            msg = "⚠ 未安装 keyring，无法保存密码：pip install keyring"
            self.shell_password_hint.setText(msg)
            return

        shell_user = self.shell_username_edit.text().strip()

        self.shell_password_hint.setText(
            "✓ 已保存密码"
            if shell_user and credentials.has_password(shell_user)
            else "尚未保存密码"
        )

    def _save(self) -> None:
        config = self._config

        config.project = self.project_edit.text().strip()
        config.template = self.template_edit.text().strip()
        config.login_url = self.login_url_edit.text().strip()

        config.shell_username = self.shell_username_edit.text().strip()
        config.shell_login_url = self.shell_login_url_edit.text().strip()
        config.export_region = self.export_region_edit.text().strip()
        config.car_file = self.car_file_edit.text().strip()

        config.data_dir = self.data_dir_edit.text().strip()

        config.headless = self.headless_switch.isChecked()
        config.minimize_to_tray = self.tray_switch.isChecked()
        config.autostart = self.autostart_switch.isChecked()

        config.save()

        shell_password = self.shell_password_edit.text()

        if shell_password:
            shell_user = config.shell_username

            if shell_user and credentials.set_password(
                shell_user,
                shell_password,
            ):
                self.shell_password_edit.clear()
            else:
                self._warn("壳牌密码保存失败（请先填壳牌账号）")

        if autostart.supported():
            autostart.apply(config.autostart)

        self._refresh_password_hint()
        self.configSaved.emit(config)

        InfoBar.success(
            "已保存",
            "设置已生效",
            duration=2500,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _reset_defaults(self) -> None:
        defaults = Config()

        self.project_edit.setText(defaults.project)
        self.template_edit.setText(defaults.template)
        self.login_url_edit.setText(defaults.login_url)
        self.shell_login_url_edit.setText(defaults.shell_login_url)
        self.export_region_edit.setText(defaults.export_region)
        self.data_dir_edit.setText(defaults.data_dir)

        self.headless_switch.setChecked(defaults.headless)
        self.tray_switch.setChecked(defaults.minimize_to_tray)

    def _warn(self, message: str) -> None:
        InfoBar.warning(
            "提示",
            message,
            duration=4000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )
