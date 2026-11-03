import json
import os
import platform
import subprocess
import threading
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QFormLayout, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QLineEdit, QMessageBox
)
from PySide6.QtCore import Qt
from core.base_module import BaseModule
from core.module_api import get_bus
from core.settings import get_settings
from core.i18n import tr

def T(key, **kwargs):
    return tr(key, module="pc_control", **kwargs)

CONFIG_FILE = Path("config") / "pc_commands.json"

class PCControlModule(BaseModule):
    def __init__(self):
        self.widget = None
        self.allow_exec_chk = None
        self.allow_dangerous_chk = None
        self.cmd_table = None
        self.lbl_status = None
        self.settings = get_settings()
        self.custom_cmds = []
        self._buffer = ""

    def on_load(self, api):
        self._load_commands()
        api.event.connect(self._on_bus_event)
        api.event.connect(self._on_ai_token)
        self._update_system_prompt()

    def _get_os_info(self):
        system = platform.system()
        if system == "Windows":
            return "Windows", "powershell"
        elif system == "Linux":
            return "Linux", "bash"
        elif system == "Darwin":
            return "macOS", "zsh"
        return system, "bash"

    def _update_system_prompt(self):
        is_allowed = self.settings.get("pc_control", "allow_exec", default=False)
        
        if not is_allowed:
            get_bus().call("ai.remove_system_prompt", "pc_control")
            return

        os_name, shell = self._get_os_info()
        
        prompt = (
            f"{T('sys_prompt_intro', os_name=os_name)}\n"
            f"{T('sys_prompt_exec')}\n"
            f"{T('sys_prompt_no_backticks')}\n"
            f"{T('sys_prompt_example')}\n"
        )

        if self.custom_cmds:
            prompt += f"\n{T('sys_prompt_custom')}\n"
            for cmd in self.custom_cmds:
                prompt += f"- {cmd['name']}: {cmd['desc']} {T('sys_prompt_custom_runs', cmd=cmd['cmd'])}\n"
            prompt += f"{T('sys_prompt_custom_use')}\n"

        get_bus().call("ai.add_system_prompt", "pc_control", prompt)

    def _on_ai_token(self, name, payload):
        if name == "ai.token_generated":
            token = payload.get("token", "")
            self._buffer += token

    def _on_bus_event(self, name, payload):
        if name == "ai.generation_done":
            text = payload.get("text", "")
            session_name = payload.get("session_name", "Default")
            self._buffer = ""
            self._process_commands(text, session_name)

    def _is_dangerous(self, cmd):
        if self.allow_dangerous_chk and self.allow_dangerous_chk.isChecked():
            return False
            
        blacklist = [
            "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "shutdown", "reboot", "poweroff",
            "del /f /s /q c:\\", "format c:", "rd /s /q c:\\"
        ]
        cmd_lower = cmd.lower()
        for b in blacklist:
            if b in cmd_lower:
                return True
        return False

    def _process_commands(self, text, session_name):
        import re
        pattern = r"\[\[EXEC:\s*(.*?)\s*\]\]"
        matches = re.findall(pattern, text)

        if not matches:
            return

        for cmd in matches:
            if self._is_dangerous(cmd):
                get_bus().emit_event(
                    "chat.inject_message", 
                    text=f"[System Block] Command '{cmd}' was blocked for safety.",
                    session_name=session_name
                )
                continue

            def run_cmd(command):
                os_name, shell = self._get_os_info()
                try:
                    if os_name == "Windows" and command.lower().startswith("start"):
                        import subprocess
                        subprocess.Popen(command, shell=True)
                        output = "Application launched in background."
                    elif os_name == "Linux" and command.strip().endswith("&"):
                        import subprocess
                        subprocess.Popen(command, shell=True)
                        output = "Application launched in background."
                    else:
                        if os_name == "Windows":
                            process = subprocess.run(command, shell=True, capture_output=True, text=True, encoding="cp866")
                        else:
                            process = subprocess.run(command, shell=True, capture_output=True, text=True, encoding="utf-8")
                        
                        output = process.stdout or process.stderr
                        if not output.strip():
                            output = "Command executed successfully (no output)."
                    
                    if len(output) > 4000:
                        output = output[:4000] + "\n... [output truncated]"
                        
                except Exception as e:
                    output = f"Execution failed: {e}"

                get_bus().emit_event(
                    "chat.inject_message",
                    text=f"`[System Output for '{command}']`:\n{output}", 
                    session_name=session_name
                )

            threading.Thread(target=run_cmd, args=(cmd,), daemon=True).start()

    def _load_commands(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self.custom_cmds = json.load(f)
            except Exception:
                self.custom_cmds = []
        else:
            self.custom_cmds = []

    def _save_commands(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.custom_cmds, f, ensure_ascii=False, indent=2)

    def create_widget(self) -> QWidget:
        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        group_main = QGroupBox(T("settings"))
        form_main = QFormLayout(group_main)

        self.allow_exec_chk = QCheckBox(T("allow_exec"))
        self.allow_exec_chk.setChecked(self.settings.get("pc_control", "allow_exec", default=False))
        self.allow_exec_chk.toggled.connect(self._on_toggle_exec)
        form_main.addRow(self.allow_exec_chk)

        self.allow_dangerous_chk = QCheckBox(T("allow_dangerous"))
        self.allow_dangerous_chk.setChecked(self.settings.get("pc_control", "allow_dangerous", default=False))
        self.allow_dangerous_chk.toggled.connect(self._on_toggle_dangerous)
        form_main.addRow(self.allow_dangerous_chk)

        layout.addWidget(group_main)

        group_cmds = QGroupBox(T("custom_cmds"))
        vbox_cmds = QVBoxLayout(group_cmds)

        self.cmd_table = QTableWidget()
        self.cmd_table.setColumnCount(3)
        self.cmd_table.setHorizontalHeaderLabels([T("col_name"), T("col_desc"), T("col_cmd")])
        self.cmd_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.cmd_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cmd_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        vbox_cmds.addWidget(self.cmd_table)

        btn_layout = QHBoxLayout()
        add_btn = QPushButton(T("add_cmd"))
        add_btn.clicked.connect(self._add_cmd_row)
        del_btn = QPushButton("X")
        del_btn.setFixedWidth(40)
        del_btn.setStyleSheet("background-color: #ef4444; color: white;")
        del_btn.clicked.connect(self._del_cmd_row)
        btn_layout.addWidget(add_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(del_btn)
        vbox_cmds.addLayout(btn_layout)

        layout.addWidget(group_cmds)

        self.lbl_status = QLabel(T("status_active") if self.allow_exec_chk.isChecked() else T("status_inactive"))
        self.lbl_status.setStyleSheet("font-weight: bold; color: #10b981;" if self.allow_exec_chk.isChecked() else "font-weight: bold; color: #ff4d6d;")
        layout.addWidget(self.lbl_status)

        save_btn = QPushButton(T("save"))
        save_btn.clicked.connect(self._save_all)
        layout.addWidget(save_btn)

        self._refresh_table()
        self._update_system_prompt()
        return self.widget

    def retranslate(self):
        if not self.widget:
            return
        self.allow_exec_chk.setText(T("allow_exec"))
        self.allow_dangerous_chk.setText(T("allow_dangerous"))
        self.cmd_table.setHorizontalHeaderLabels([T("col_name"), T("col_desc"), T("col_cmd")])
        
        if self.allow_exec_chk.isChecked():
            self.lbl_status.setText(T("status_active"))
        else:
            self.lbl_status.setText(T("status_inactive"))
            
        self._update_system_prompt()

    def _refresh_table(self):
        self.cmd_table.setRowCount(len(self.custom_cmds))
        for i, cmd in enumerate(self.custom_cmds):
            self.cmd_table.setItem(i, 0, QTableWidgetItem(cmd.get("name", "")))
            self.cmd_table.setItem(i, 1, QTableWidgetItem(cmd.get("desc", "")))
            self.cmd_table.setItem(i, 2, QTableWidgetItem(cmd.get("cmd", "")))

    def _add_cmd_row(self):
        self.cmd_table.insertRow(self.cmd_table.rowCount())

    def _del_cmd_row(self):
        sel = self.cmd_table.selectionModel().selectedRows()
        if sel:
            self.cmd_table.removeRow(sel[0].row())

    def _on_toggle_exec(self, checked):
        self.settings.set("pc_control", "allow_exec", value=checked)
        if checked:
            self.lbl_status.setText(T("status_active"))
            self.lbl_status.setStyleSheet("font-weight: bold; color: #10b981;")
        else:
            self.lbl_status.setText(T("status_inactive"))
            self.lbl_status.setStyleSheet("font-weight: bold; color: #ff4d6d;")
        self._update_system_prompt()

    def _on_toggle_dangerous(self, checked):
        self.settings.set("pc_control", "allow_dangerous", value=checked)

    def _save_all(self):
        self.custom_cmds = []
        for i in range(self.cmd_table.rowCount()):
            name = self.cmd_table.item(i, 0)
            desc = self.cmd_table.item(i, 1)
            cmd = self.cmd_table.item(i, 2)
            
            if name and cmd:
                self.custom_cmds.append({
                    "name": name.text().strip(),
                    "desc": desc.text().strip() if desc else "",
                    "cmd": cmd.text().strip()
                })
        
        self._save_commands()
        self._update_system_prompt()
        QMessageBox.information(self.widget, T("settings"), T("saved"))

    def on_shutdown(self):
        try:
            get_bus().call("ai.remove_system_prompt", "pc_control")
        except Exception:
            pass