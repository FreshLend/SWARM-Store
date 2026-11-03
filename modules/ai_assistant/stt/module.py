import importlib.util
import threading
import time
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QLineEdit, QComboBox, 
    QPushButton, QGroupBox, QFormLayout, QMessageBox, QPlainTextEdit
)
from PySide6.QtCore import Qt, QThread, Signal
from core.base_module import BaseModule
from core.module_api import get_bus
from core.i18n import tr

def T(key, **kwargs):
    return tr(key, module="stt", **kwargs)

class STTWorker(QThread):
    status_update = Signal(str)
    text_recognized = Signal(str)
    worker_crashed = Signal()
    
    def __init__(self, wake_words, model_size, device, language="auto", target_session="Default"):
        super().__init__()
        self.wake_words = [w.strip().lower() for w in wake_words.split(",") if w.strip()]
        self.model_size = model_size
        self.device = device
        self.language = language
        self.target_session = target_session
        self.running = True
        self.model = None
        self.audio_buffer = []
        self.buffer_lock = threading.Lock()

    def run(self):
        try:
            import numpy as np
            self.status_update.emit(T("status_loading"))
            from faster_whisper import WhisperModel
            import sounddevice as sd
            
            self.model = WhisperModel(self.model_size, device=self.device, compute_type="int8", cpu_threads=4)
            
            if not self.running:
                return

            self.status_update.emit(T("status_on"))
            
            def audio_callback(indata, frames, time_info, status):
                if status:
                    print(f"[STT] Audio status: {status}")
                with self.buffer_lock:
                    self.audio_buffer.append(indata.copy())

            with sd.InputStream(samplerate=16000, channels=1, callback=audio_callback, blocksize=4000):
                while self.running:
                    time.sleep(1.5)
                    with self.buffer_lock:
                        if not self.audio_buffer:
                            continue
                        audio_data = np.concatenate(self.audio_buffer, axis=0)
                        self.audio_buffer.clear()
                    
                    audio_data = audio_data.flatten().astype(np.float32)
                    
                    if len(audio_data) < 8000:
                        continue
                    
                    lang_code = None if self.language == "auto" else self.language
                    
                    segments, info = self.model.transcribe(
                        audio_data, 
                        beam_size=1,
                        vad_filter=True,
                        vad_parameters=dict(min_silence_duration_ms=500),
                        condition_on_previous_text=False,
                        temperature=0.0,
                        language=lang_code
                    )
                    
                    text = "".join([segment.text for segment in segments]).strip()
                    
                    if text:
                        if len(text) < 2:
                            continue
                            
                        self.text_recognized.emit(text)
                        text_lower = text.lower()
                        if any(word in text_lower for word in self.wake_words):
                            get_bus().emit_event("chat.inject_message", text=text, session_name=self.target_session)

        except Exception as e:
            self.status_update.emit(f"{T('err_prefix')} {e}")
            self.worker_crashed.emit()
        finally:
            self.status_update.emit(T("status_off"))

    def stop(self):
        self.running = False

class STTModule(BaseModule):
    def __init__(self):
        self.widget = None
        self.wake_input = None
        self.model_box = None
        self.device_box = None
        self.lang_box = None
        self.session_box = None
        self.btn_toggle = None
        self.lbl_status = None
        self.log_area = None
        self.worker = None
        self.is_listening = False

        self.wake_words = "Aura, Аура"
        self.model_size = "base"
        self.device = "cpu"
        self.language = "ru"

    def on_load(self, api):
        api.event.connect(self._on_bus_event)

    def _on_bus_event(self, name, payload):
        if name == "chat.sessions_changed":
            if self.session_box:
                current_text = self.session_box.currentText()
                self._refresh_sessions()
                idx = self.session_box.findText(current_text)
                if idx >= 0:
                    self.session_box.setCurrentIndex(idx)

    def _check_dependencies(self):
        missing = []
        for pkg in ["faster_whisper", "sounddevice", "numpy"]:
            if importlib.util.find_spec(pkg) is None:
                missing.append(pkg)
        return missing

    def _refresh_sessions(self):
        if not self.session_box:
            return
        self.session_box.clear()
        try:
            sessions = get_bus().call("chat.get_sessions")
            if sessions:
                self.session_box.addItems(sessions)
                return
        except Exception:
            pass
        self.session_box.addItem("Default")

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

        self.form_layout = QFormLayout()
        group = QGroupBox(T("settings"))
        group.setLayout(self.form_layout)

        self.wake_input = QLineEdit(self.wake_words)
        self.form_layout.addRow(T("wake_words"), self.wake_input)

        self.model_box = QComboBox()
        self.model_box.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.model_box.setCurrentText(self.model_size)
        self.form_layout.addRow(T("model_size"), self.model_box)

        self.lang_box = QComboBox()
        self.lang_box.addItems(["auto", "ru", "en"])
        self.lang_box.setCurrentText(self.language)
        self.form_layout.addRow(T("language"), self.lang_box)

        self.device_box = QComboBox()
        self.device_box.addItems(["cpu", "cuda"])
        self.device_box.setCurrentText(self.device)
        self.form_layout.addRow(T("device"), self.device_box)

        self.session_box = QComboBox()
        self._refresh_sessions()
        self.form_layout.addRow(T("target_session"), self.session_box)

        layout.addWidget(group)

        self.btn_toggle = QPushButton(T("start_listening"))
        self.btn_toggle.clicked.connect(self._toggle_listening)
        layout.addWidget(self.btn_toggle)

        self.lbl_status = QLabel(T("status_off"))
        self.lbl_status.setStyleSheet("font-weight: bold; color: #ff4d6d;")
        layout.addWidget(self.lbl_status)

        self.lbl_log = QLabel(T("log"))
        layout.addWidget(self.lbl_log)
        
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)

        return self.widget

    def retranslate(self):
        if not self.widget:
            return
            
        self.form_layout.labelForField(self.wake_input).setText(T("wake_words"))
        self.form_layout.labelForField(self.model_box).setText(T("model_size"))
        self.form_layout.labelForField(self.lang_box).setText(T("language"))
        self.form_layout.labelForField(self.device_box).setText(T("device"))
        self.form_layout.labelForField(self.session_box).setText(T("target_session"))
        
        self.lbl_log.setText(T("log"))
        
        if self.is_listening:
            self.btn_toggle.setText(T("stop_listening"))
        else:
            self.btn_toggle.setText(T("start_listening"))
            self.lbl_status.setText(T("status_off"))

    def _reset_ui(self):
        self.is_listening = False
        self.btn_toggle.setText(T("start_listening"))
        self.btn_toggle.setStyleSheet("")
        self.lbl_status.setStyleSheet("font-weight: bold; color: #ff4d6d;")
        self.lbl_status.setText(T("status_off"))
        self.wake_input.setEnabled(True)
        self.model_box.setEnabled(True)
        self.device_box.setEnabled(True)
        self.lang_box.setEnabled(True)
        self.session_box.setEnabled(True)
        self.worker = None

    def _toggle_listening(self):
        if not self.is_listening:
            self.wake_words = self.wake_input.text().strip()
            self.model_size = self.model_box.currentText()
            self.device = self.device_box.currentText()
            self.language = self.lang_box.currentText()
            target_session = self.session_box.currentText() if self.session_box else "Default"
            
            if not self.wake_words:
                QMessageBox.warning(self.widget, T("warn_title"), T("warn_no_wake_words"))
                return

            self.is_listening = True
            self.btn_toggle.setText(T("stop_listening"))
            self.btn_toggle.setStyleSheet("background-color: #ef4444; color: white;")
            self.lbl_status.setStyleSheet("font-weight: bold; color: #10b981;")
            self.lbl_status.setText(T("status_loading"))
            self.wake_input.setEnabled(False)
            self.model_box.setEnabled(False)
            self.device_box.setEnabled(False)
            self.lang_box.setEnabled(False)
            self.session_box.setEnabled(False)

            self.worker = STTWorker(self.wake_words, self.model_size, self.device, self.language, target_session)
            self.worker.status_update.connect(self._on_status)
            self.worker.text_recognized.connect(self._on_text)
            self.worker.worker_crashed.connect(self._reset_ui)
            self.worker.start()
        else:
            self.is_listening = False
            self.btn_toggle.setText(T("start_listening"))
            self.btn_toggle.setStyleSheet("")
            self.lbl_status.setStyleSheet("font-weight: bold; color: #ff4d6d;")
            self.lbl_status.setText(T("status_off"))
            self.wake_input.setEnabled(True)
            self.model_box.setEnabled(True)
            self.device_box.setEnabled(True)
            self.lang_box.setEnabled(True)
            self.session_box.setEnabled(True)

            if self.worker:
                self.worker.stop()
                self.worker.wait(3000)
                self.worker = None

    def _on_status(self, msg):
        self.lbl_status.setText(msg)

    def _on_text(self, text):
        self.log_area.appendPlainText(f"[{time.strftime('%H:%M:%S')}] Recognized: {text}")

    def on_shutdown(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)