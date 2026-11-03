import json
import platform
import subprocess
import threading
import html
import re
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QFormLayout, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMessageBox, QComboBox, QPlainTextEdit, QTabWidget
)
from core.base_module import BaseModule
from core.module_api import get_bus
from core.settings import get_settings
from core.i18n import tr

def T(key, **kwargs):
    return tr(key, module="pc_control", **kwargs)

CONFIG_FILE = Path("config") / "pc_commands.json"
SECURITY_FILE = Path("config") / "pc_security.json"

DEFAULT_BLACKLIST = [
    r"rm\s+-rf\s+[/~*]",           
    r"rm\s+-rf\s+--no-preserve-root", 
    r"mkfs",                       
    r"dd\s+if=",                   
    r"shutdown",                   
    r"reboot",                     
    r"poweroff",                   
    r"halt",                       
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", 
    r"chmod\s+-R\s+777\s+/",       
    r"mv\s+/\*\s+/dev/null",       
    r"format\s+[a-zA-Z]:",         
    r"diskpart",                   
    r"del\s+/f\s+/s\s+/q\s+[a-zA-Z]:\\", 
    r"rd\s+/s\s+/q\s+[a-zA-Z]:\\", 
    r"bcdedit",                    
    r"vssadmin\s+delete\s+shadows",
    r"cipher\s+/w",                
    r"takeown\s+/f",               
    r"icacls\s+[a-zA-Z]:\\",       
    r"reg\s+delete\s+HKLM",        
    r"reg\s+delete\s+HKCU",        
]

DEFAULT_WHITELIST = [
    r"^echo",
    r"^dir",
    r"^start",
    r"^powershell",
    r"^systeminfo",
    r"^ipconfig",
    r"^ping",
    r"^tasklist",
    r"^taskkill",
    r"^ls",
    r"^pwd",
    r"^cat"
]

class PCControlModule(BaseModule):
    def __init__(self):
        self.widget = None
        self.allow_exec_chk = None
        self.mode_box = None
        self.blacklist_edit = None
        self.whitelist_edit = None
        self.cmd_table = None
        self.lbl_status = None
        self.settings = get_settings()
        self.custom_cmds = []
        self.blacklist = []
        self.whitelist = []
        self.security_mode = "blacklist"
        self._buffer = ""

    def on_load(self, api):
        self._load_commands()
        self._load_security()
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

    def _is_blocked(self, cmd):
        cmd_lower = cmd.lower()
        
        if self.security_mode == "whitelist":
            for pattern in self.whitelist:
                if pattern.strip() and re.search(pattern, cmd_lower, re.IGNORECASE):
                    return False, "Allowed by whitelist"
            return True, "Not in whitelist"
        else:
            for pattern in self.blacklist:
                if pattern.strip() and re.search(pattern, cmd_lower, re.IGNORECASE):
                    return True, "Matched blacklist"
            return False, "Allowed"

    def _process_commands(self, text, session_name):
        pattern = r"\[\[EXEC:\s*(.*?)\s*\]\]"
        matches = re.findall(pattern, text)

        if not matches:
            return

        for cmd in matches:
            cmd = html.unescape(cmd)
            cmd = cmd.replace("&quot;", '"').replace("&quote;", '"').replace("&amp;", "&")
            cmd = cmd.replace("&#x27;", "'").replace("&#x22;", '"')

            is_blocked, reason = self._is_blocked(cmd)
            
            if is_blocked:
                get_bus().emit_event(
                    "chat.inject_message", 
                    text=f"**[System Block]** Command '{cmd}' was blocked by security policy. ({reason})",
                    session_name=session_name
                )
                continue

            def run_cmd(command):
                os_name, shell = self._get_os_info()
                try:
                    if os_name == "Windows" and command.lower().startswith("start"):
                        subprocess.Popen(command, shell=True)
                        output = "Application launched in background."
                    elif os_name == "Linux" and command.strip().endswith("&"):
                        subprocess.Popen(command, shell=True)
                        output = "Application launched in background."
                    else:
                        if os_name == "Windows":
                            cmd_lower = command.lower()
                            if "powershell" not in cmd_lower:
                                if not cmd_lower.startswith("chcp"):
                                    command = f"chcp 65001 > nul && {command}"

                            process = subprocess.run(command, shell=True, capture_output=True)
                            
                            def smart_decode(data):
                                if not data:
                                    return ""
                                for enc in ("utf-8", "cp866", "cp1251", "utf-16-le", "cp1252", "latin-1"):
                                    try:
                                        return data.decode(enc)
                                    except UnicodeDecodeError:
                                        continue
                                return data.decode("utf-8", errors="replace")
                                
                            out_str = smart_decode(process.stdout)
                            err_str = smart_decode(process.stderr)
                            output = out_str or err_str
                            
                        else:
                            process = subprocess.run(command, shell=True, capture_output=True)
                            output = process.stdout.decode("utf-8", errors="replace") or process.stderr.decode("utf-8", errors="replace")
                        
                        if not output.strip():
                            output = "Command executed successfully (no output)."
                    
                    if len(output) > 4000:
                        output = output[:4000] + "\n... [output truncated]"
                        
                except Exception as e:
                    output = f"Execution failed: {e}"

                output = output.replace("&quot;", '"').replace("&amp;", "&")
                output = output.replace("&#x27;", "'").replace("&#x22;", '"')

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

    def _load_security(self):
        SECURITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if SECURITY_FILE.exists():
            try:
                with open(SECURITY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.security_mode = data.get("mode", "blacklist")
                    self.blacklist = data.get("blacklist", DEFAULT_BLACKLIST)
                    self.whitelist = data.get("whitelist", DEFAULT_WHITELIST)
            except Exception:
                self.security_mode = "blacklist"
                self.blacklist = DEFAULT_BLACKLIST
                self.whitelist = DEFAULT_WHITELIST
        else:
            self.security_mode = "blacklist"
            self.blacklist = DEFAULT_BLACKLIST
            self.whitelist = DEFAULT_WHITELIST
            self._save_security()

    def _save_security(self):
        SECURITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SECURITY_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "mode": self.security_mode,
                "blacklist": self.blacklist,
                "whitelist": self.whitelist
            }, f, ensure_ascii=False, indent=2)

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

        self.mode_box = QComboBox()
        self.mode_box.addItem(T("mode_blacklist"), "blacklist")
        self.mode_box.addItem(T("mode_whitelist"), "whitelist")
        self.mode_box.setCurrentIndex(0 if self.security_mode == "blacklist" else 1)
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        form_main.addRow(T("security_mode"), self.mode_box)

        layout.addWidget(group_main)

        tabs_security = QTabWidget()
        
        self.blacklist_edit = QPlainTextEdit()
        self.blacklist_edit.setPlainText("\n".join(self.blacklist))
        tabs_security.addTab(self.blacklist_edit, T("blacklist"))

        self.whitelist_edit = QPlainTextEdit()
        self.whitelist_edit.setPlainText("\n".join(self.whitelist))
        tabs_security.addTab(self.whitelist_edit, T("whitelist"))

        layout.addWidget(tabs_security)

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
        self.mode_box.setItemText(0, T("mode_blacklist"))
        self.mode_box.setItemText(1, T("mode_whitelist"))
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

    def _on_mode_changed(self):
        self.security_mode = self.mode_box.currentData()

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
        
        self.blacklist = [line.strip() for line in self.blacklist_edit.toPlainText().splitlines() if line.strip()]
        self.whitelist = [line.strip() for line in self.whitelist_edit.toPlainText().splitlines() if line.strip()]
        
        self._save_commands()
        self._save_security()
        self._update_system_prompt()
        QMessageBox.information(self.widget, T("settings"), T("saved"))

    def on_shutdown(self):
        try:
            get_bus().call("ai.remove_system_prompt", "pc_control")
        except Exception:
            pass