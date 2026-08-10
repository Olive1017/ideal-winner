"""设置页：账号密码、项目模板、浏览器与开机自启。

密码不进配置文件，只进 Windows 凭据管理器；输入框里也不回显原密码，
只用一个「已保存」状态提示。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout, QWidget
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


class SettingsPage(QWidget):
    """设置页。保存后把新 Config 发给主窗口，由主窗口去重建调度器。"""

    configSaved = Signal(object)  # Config

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._config = config
        self._build_ui()
        self.load_config(config)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("设置", self))
        layout.addWidget(self._build_account_card())
        layout.addWidget(self._build_import_card())
        layout.addWidget(self._build_advanced_card())

        buttons = QHBoxLayout()
        self.save_btn = PrimaryPushButton("保存设置", self)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(self.save_btn)

        reset_btn = PushButton("恢复默认值", self)
        reset_btn.clicked.connect(self._reset_defaults)
        buttons.addWidget(reset_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addStretch(1)

    def _build_account_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(10)
        inner.addWidget(StrongBodyLabel("SDCC 账号", card))

        form = QFormLayout()
        form.setSpacing(10)

        self.username_edit = LineEdit(card)
        self.username_edit.setPlaceholderText("用户编号 / 手机号 / 邮箱")
        form.addRow(BodyLabel("账号", card), self.username_edit)

        self.password_edit = PasswordLineEdit(card)
        self.password_edit.setPlaceholderText("留空则不修改已保存的密码")
        form.addRow(BodyLabel("密码", card), self.password_edit)

        inner.addLayout(form)

        self.password_hint = CaptionLabel("", card)
        inner.addWidget(self.password_hint)
        inner.addWidget(
            CaptionLabel("密码保存在 Windows 凭据管理器，不会写进配置文件或日志", card)
        )
        return card

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
            CaptionLabel("需与 SDCC 导入弹窗里下拉选项的文字完全一致，否则会选不中", card)
        )
        return card

    def _build_advanced_card(self) -> CardWidget:
        card = CardWidget(self)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(12)
        inner.addWidget(StrongBodyLabel("运行方式", card))

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

    def _switch_row(self, card, layout, title: str, caption: str) -> SwitchButton:
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
        self.username_edit.setText(config.username)
        self.project_edit.setText(config.project)
        self.template_edit.setText(config.template)
        self.login_url_edit.setText(config.login_url)
        self.headless_switch.setChecked(config.headless)
        self.tray_switch.setChecked(config.minimize_to_tray)
        self.autostart_switch.setChecked(autostart.is_enabled())
        self._refresh_password_hint()

    def _refresh_password_hint(self) -> None:
        username = self.username_edit.text().strip()
        if not credentials.available():
            self.password_hint.setText("⚠ 未安装 keyring，无法保存密码：pip install keyring")
        elif username and credentials.has_password(username):
            self.password_hint.setText("✓ 已保存密码")
        else:
            self.password_hint.setText("尚未保存密码")

    def _save(self) -> None:
        username = self.username_edit.text().strip()
        if not username:
            self._warn("请先填写 SDCC 账号")
            return

        config = self._config
        config.username = username
        config.project = self.project_edit.text().strip()
        config.template = self.template_edit.text().strip()
        config.login_url = self.login_url_edit.text().strip()
        config.headless = self.headless_switch.isChecked()
        config.minimize_to_tray = self.tray_switch.isChecked()
        config.autostart = self.autostart_switch.isChecked()
        config.save()

        password = self.password_edit.text()
        if password:
            if credentials.set_password(username, password):
                self.password_edit.clear()
            else:
                self._warn("密码保存失败，其余设置已保存")

        if autostart.supported():
            autostart.apply(config.autostart)

        self._refresh_password_hint()
        self.configSaved.emit(config)
        InfoBar.success("已保存", "设置已生效", duration=2500,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _reset_defaults(self) -> None:
        defaults = Config()
        self.project_edit.setText(defaults.project)
        self.template_edit.setText(defaults.template)
        self.login_url_edit.setText(defaults.login_url)
        self.headless_switch.setChecked(defaults.headless)
        self.tray_switch.setChecked(defaults.minimize_to_tray)

    def _warn(self, message: str) -> None:
        InfoBar.warning("提示", message, duration=4000,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)
