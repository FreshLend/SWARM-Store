import json
import importlib.util
import os
import sys
import subprocess
from pathlib import Path
import requests
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit, QTextEdit, 
    QPushButton, QGroupBox, QFormLayout, QMessageBox
)
from PySide6.QtCore import Qt
from core.base_module import BaseModule
from core.module_api import get_bus
from core.settings import get_settings
from core.i18n import tr

def T(key, **kwargs):
    return tr(key, module="ai_control", **kwargs)

CONFIG_DIR = Path("config")
KEY_FILE = CONFIG_DIR / "ai_key.bin"
MODELS_DIR = Path("models")

class AIControlModule(BaseModule):
    def __init__(self):
        self.widget = None
        self.provider_box = None
        self.model_input = None
        self.local_model_combo = None
        self.open_folder_btn = None
        self.api_key_input = None
        self.host_input = None
        self.system_prompt_edit = None
        self.extra_prompts_layout = None
        
        self.group_settings = None
        self.lbl_provider = None
        self.lbl_model = None
        self.lbl_local_model = None
        self.lbl_api_key = None
        self.lbl_host = None
        self.lbl_system_prompt = None
        self.save_btn = None
        self.group_extra = None
        
        self.settings = get_settings()
        self._extra_prompts = {}
        self._fernet = None
        
        self._current_provider = "Ollama"
        self._current_model = "llama3"
        self._current_api_key = ""
        self._current_host = "http://localhost:11434"
        
        self._default_prompt = T("default_system_prompt")
        self._system_prompt = self._default_prompt

    def on_load(self, api):
        self._init_encryption()
        self._load_settings()
        
        api.register_service(
            self.name, "ai.generate", self.generate_stream,
            "ai.generate(messages, callback, provider=None, model=None) -> dict"
        )
        api.register_service(
            self.name, "ai.get_config", self.get_config,
            "ai.get_config() -> dict"
        )
        api.register_service(
            self.name, "ai.add_system_prompt", self.add_extra_prompt,
            "ai.add_system_prompt(module_id, prompt) -> bool"
        )
        api.register_service(
            self.name, "ai.remove_system_prompt", self.remove_extra_prompt,
            "ai.remove_system_prompt(module_id) -> bool"
        )

    def _init_encryption(self):
        try:
            from cryptography.fernet import Fernet
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if KEY_FILE.exists():
                key = KEY_FILE.read_bytes()
            else:
                key = Fernet.generate_key()
                KEY_FILE.write_bytes(key)
            self._fernet = Fernet(key)
        except ImportError:
            self._fernet = None

    def _encrypt(self, text):
        if not text or not self._fernet:
            return text
        return self._fernet.encrypt(text.encode()).decode()

    def _decrypt(self, text):
        if not text or not self._fernet:
            return text
        try:
            return self._fernet.decrypt(text.encode()).decode()
        except Exception:
            return ""

    def _load_settings(self):
        cfg = self.settings.get("ai_control", default={})
        self._current_provider = cfg.get("provider", "Ollama")
        self._current_model = cfg.get("model", "llama3")
        self._current_host = cfg.get("host", "http://localhost:11434")
        
        self._default_prompt = T("default_system_prompt")
        self._system_prompt = cfg.get("system_prompt", self._default_prompt)
        
        encrypted_key = cfg.get("api_key", "")
        self._current_api_key = self._decrypt(encrypted_key)

    def _save_settings(self):
        encrypted_key = self._encrypt(self._current_api_key)
        self.settings.set("ai_control", "provider", value=self._current_provider)
        self.settings.set("ai_control", "model", value=self._current_model)
        self.settings.set("ai_control", "api_key", value=encrypted_key)
        self.settings.set("ai_control", "host", value=self._current_host)
        self.settings.set("ai_control", "system_prompt", value=self._system_prompt)

    def get_config(self):
        return {
            "provider": self._current_provider,
            "model": self._current_model,
            "api_key": self._current_api_key,
            "host": self._current_host,
            "system_prompt": self._system_prompt
        }

    def add_extra_prompt(self, module_id, prompt):
        self._extra_prompts[module_id] = prompt
        self._refresh_extra_prompts_ui()
        return True

    def remove_extra_prompt(self, module_id):
        if module_id in self._extra_prompts:
            del self._extra_prompts[module_id]
            self._refresh_extra_prompts_ui()
            return True
        return False

    def _get_full_system_prompt(self):
        full = self._system_prompt.strip()
        for mod_id, text in self._extra_prompts.items():
            full += f"\n\n[{mod_id} Instructions]:\n{text.strip()}"
        return full

    def _check_dependencies(self):
        if importlib.util.find_spec("cryptography") is None:
            return ["cryptography"]
        return []

    def _get_available_providers(self):
        providers = []
        if importlib.util.find_spec("openai") is not None:
            providers.append("OpenAI")
        if importlib.util.find_spec("requests") is not None:
            providers.append("Ollama")
        if importlib.util.find_spec("llama_cpp") is not None:
            providers.append("Local (llama-cpp-python)")
        return providers

    def create_widget(self) -> QWidget:
        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        missing = self._check_dependencies()
        if missing:
            box = QGroupBox(T("missing_deps"))
            bl = QVBoxLayout(box)
            bl.addWidget(QLabel(T("missing_deps_text")))
            for m in missing:
                bl.addWidget(QLabel(f"• {m}"))
            bl.addWidget(QLabel(T("install_cmd")))
            cmd_label = QLabel(f"pip install {' '.join(missing)}")
            cmd_label.setStyleSheet("background: #333; padding: 8px; font-family: Consolas;")
            cmd_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bl.addWidget(cmd_label)
            layout.addWidget(box)
            layout.addStretch()
            return self.widget

        available = self._get_available_providers()
        if not available:
            box = QGroupBox(T("missing_deps"))
            bl = QVBoxLayout(box)
            bl.addWidget(QLabel(T("missing_deps_text")))
            bl.addWidget(QLabel("• openai\n• requests\n• llama-cpp-python"))
            bl.addWidget(QLabel(T("install_cmd")))
            cmd_label = QLabel("pip install openai requests llama-cpp-python")
            cmd_label.setStyleSheet("background: #333; padding: 8px; font-family: Consolas;")
            cmd_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bl.addWidget(cmd_label)
            layout.addWidget(box)
            layout.addStretch()
            return self.widget

        self.group_settings = QGroupBox()
        self.form_layout = QFormLayout(self.group_settings)

        self.provider_box = QComboBox()
        self.provider_box.addItems(available)
        self.provider_box.currentTextChanged.connect(self._on_provider_changed)
        
        self.lbl_provider = QLabel()
        self.form_layout.addRow(self.lbl_provider, self.provider_box)

        self.model_input = QLineEdit(self._current_model)
        self.lbl_model = QLabel()
        self.form_layout.addRow(self.lbl_model, self.model_input)

        local_layout = QHBoxLayout()
        self.local_model_combo = QComboBox()
        
        self.open_folder_btn = QPushButton() 
        self.open_folder_btn.clicked.connect(self._open_models_folder)
        
        local_layout.addWidget(self.local_model_combo, 1)
        local_layout.addWidget(self.open_folder_btn)
        self.lbl_local_model = QLabel()
        self.form_layout.addRow(self.lbl_local_model, local_layout)

        self.api_key_input = QLineEdit(self._current_api_key)
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("sk-...")
        self.lbl_api_key = QLabel()
        self.form_layout.addRow(self.lbl_api_key, self.api_key_input)

        self.host_input = QLineEdit(self._current_host)
        self.lbl_host = QLabel()
        self.form_layout.addRow(self.lbl_host, self.host_input)

        self.system_prompt_edit = QTextEdit()
        self.system_prompt_edit.setPlainText(self._system_prompt)
        self.system_prompt_edit.setFixedHeight(80)
        self.lbl_system_prompt = QLabel()
        self.form_layout.addRow(self.lbl_system_prompt, self.system_prompt_edit)

        layout.addWidget(self.group_settings)

        self.extra_prompts_layout = QVBoxLayout()
        self.group_extra = QGroupBox()
        self.group_extra.setLayout(self.extra_prompts_layout)
        layout.addWidget(self.group_extra)
        
        if self._current_provider in available:
            self.provider_box.setCurrentText(self._current_provider)
        else:
            self.provider_box.setCurrentText(available[0])
            
        self._refresh_extra_prompts_ui()

        self.save_btn = QPushButton()
        self.save_btn.clicked.connect(self._save_config)
        layout.addWidget(self.save_btn)

        layout.addStretch()
        
        self._refresh_local_models()
        self._on_provider_changed(self.provider_box.currentText(), silent=False)
        self.retranslate()
        return self.widget

    def retranslate(self):
        if not self.widget:
            return
            
        self.group_settings.setTitle(T("settings"))
        self.lbl_provider.setText(T("provider"))
        self.lbl_model.setText(T("model"))
        self.lbl_local_model.setText(T("local_model"))
        self.open_folder_btn.setText(T("open_models"))
        self.lbl_api_key.setText(T("api_key"))
        self.lbl_host.setText(T("host"))
        self.lbl_system_prompt.setText(T("system_prompt"))
        self.save_btn.setText(T("save"))
        self.group_extra.setTitle(T("extra_prompts"))
        
        new_default = T("default_system_prompt")
        current_text = self.system_prompt_edit.toPlainText().strip()
        
        if not current_text or current_text == self._default_prompt:
            self.system_prompt_edit.setPlainText(new_default)
            self._system_prompt = new_default
            
        self._default_prompt = new_default
        self._refresh_extra_prompts_ui()

    def _refresh_local_models(self):
        if not self.local_model_combo:
            return
        current = self.local_model_combo.currentText()
        self.local_model_combo.clear()
        MODELS_DIR.mkdir(exist_ok=True)
        for f in MODELS_DIR.glob("*.gguf"):
            self.local_model_combo.addItem(f.name)
        if current:
            idx = self.local_model_combo.findText(current)
            if idx >= 0:
                self.local_model_combo.setCurrentIndex(idx)

    def _open_models_folder(self):
        MODELS_DIR.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(MODELS_DIR.resolve())
        elif sys.platform == "darwin":
            subprocess.run(["open", str(MODELS_DIR.resolve())])
        else:
            subprocess.run(["xdg-open", str(MODELS_DIR.resolve())])

    def _refresh_extra_prompts_ui(self):
        if not self.extra_prompts_layout:
            return
        while self.extra_prompts_layout.count():
            item = self.extra_prompts_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not self._extra_prompts:
            self.extra_prompts_layout.addWidget(QLabel(T("no_extra")))
            return

        for mod_id, text in self._extra_prompts.items():
            lbl = QLabel(f"<b>{mod_id}:</b> {text[:80]}...")
            lbl.setWordWrap(True)
            self.extra_prompts_layout.addWidget(lbl)

    def _on_provider_changed(self, provider, silent=False):
        if silent:
            return
        self._current_provider = provider
        is_local = provider == "Local (llama-cpp-python)"
        
        self.lbl_model.setVisible(not is_local)
        self.model_input.setVisible(not is_local)
        
        self.lbl_local_model.setVisible(is_local)
        self.local_model_combo.setVisible(is_local)
        self.open_folder_btn.setVisible(is_local)
        
        is_openai = provider == "OpenAI"
        self.lbl_api_key.setVisible(is_openai)
        self.api_key_input.setVisible(is_openai)
        self.lbl_host.setVisible(not is_local)
        self.host_input.setVisible(not is_local)
        
        if is_openai:
            if not self.host_input.text() or "localhost" in self.host_input.text():
                self.host_input.setText("https://api.openai.com/v1")
            if not self.model_input.text() or self.model_input.text() == "llama3":
                self.model_input.setText("gpt-4o-mini")
        elif provider == "Ollama":
            if not self.host_input.text() or "openai" in self.host_input.text():
                self.host_input.setText("http://localhost:11434")
        elif is_local:
            self._refresh_local_models()

    def _save_config(self):
        self._current_provider = self.provider_box.currentText()
        if self._current_provider == "Local (llama-cpp-python)":
            self._current_model = self.local_model_combo.currentText()
        else:
            self._current_model = self.model_input.text().strip()
            
        self._current_api_key = self.api_key_input.text().strip()
        self._current_host = self.host_input.text().strip()
        self._system_prompt = self.system_prompt_edit.toPlainText().strip()
        
        self._save_settings()
        get_bus().emit_event("ai.config_changed", provider=self._current_provider, model=self._current_model)
        QMessageBox.information(self.widget, T("settings"), T("saved"))

    def generate_stream(self, messages, callback, provider=None, model=None):
        p = provider or self._current_provider
        
        if p == "Local (llama-cpp-python)":
            m = model or (self.local_model_combo.currentText() if self.local_model_combo else "")
        else:
            m = model or self._current_model
        
        sys_prompt = self._get_full_system_prompt()
        if sys_prompt and (not messages or messages[0].get("role") != "system"):
            messages = [{"role": "system", "content": sys_prompt}] + messages

        try:
            if p == "OpenAI":
                return self._generate_openai(messages, m, callback)
            elif p == "Ollama":
                return self._generate_ollama(messages, m, callback)
            elif p == "Local (llama-cpp-python)":
                return self._generate_local(messages, m, callback)
            return {"ok": False, "error": "Unknown provider"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _generate_openai(self, messages, model, callback):
        from openai import OpenAI
        base_url = self._current_host or None
        client = OpenAI(api_key=self._current_api_key, base_url=base_url)
        full_response = ""
        stream = client.chat.completions.create(model=model, messages=messages, stream=True)
        for chunk in stream:
            if chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_response += token
                callback(token)
                get_bus().emit_event("ai.token_generated", token=token)
        get_bus().emit_event("ai.generation_done", text=full_response)
        return {"ok": True, "text": full_response}

    def _generate_ollama(self, messages, model, callback):
        host = self._current_host or "http://localhost:11434"
        url = f"{host}/api/chat"
        payload = {"model": model, "messages": messages, "stream": True}
        full_response = ""
        with requests.post(url, json=payload, stream=True, timeout=60) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    data = json.loads(line)
                    if "message" in data and "content" in data["message"]:
                        token = data["message"]["content"]
                        full_response += token
                        callback(token)
                        get_bus().emit_event("ai.token_generated", token=token)
                    if data.get("done"):
                        break
        get_bus().emit_event("ai.generation_done", text=full_response)
        return {"ok": True, "text": full_response}

    def _generate_local(self, messages, model, callback):
        from llama_cpp import Llama
        if not model:
            return {"ok": False, "error": "No local model selected"}
        
        model_path = str(MODELS_DIR / model)
        if not os.path.exists(model_path):
            return {"ok": False, "error": f"Model file not found: {model_path}"}
            
        llm = Llama(model_path=model_path, n_ctx=4096, verbose=False)
        prompt = ""
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            prompt += f"{role.capitalize()}: {content}\n"
        prompt += "Assistant: "
        
        full_response = ""
        stream = llm(prompt, max_tokens=1024, stream=True)
        for chunk in stream:
            token = chunk["choices"][0]["text"]
            full_response += token
            callback(token)
            get_bus().emit_event("ai.token_generated", token=token)
        get_bus().emit_event("ai.generation_done", text=full_response)
        return {"ok": True, "text": full_response}