import importlib.util
import os
import re
import queue
import tempfile
import multiprocessing
import torch
import torchaudio as ta
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit,
    QGroupBox, QFormLayout, QCheckBox, QPlainTextEdit, QPushButton, QFileDialog,
    QMessageBox
)
from core.base_module import BaseModule
from core.module_api import get_bus
from core.i18n import tr
from core.settings import get_settings

class EditableComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.showPopup()
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        self.lineEdit().setReadOnly(False)
        super().keyPressEvent(event)

    def hidePopup(self):
        self.lineEdit().setReadOnly(True)
        super().hidePopup()
        

def T(key, **kwargs):
    return tr(key, module="tts", **kwargs)

def _tts_process_main(config, text_queue, log_queue):
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
    except Exception as e:
        log_queue.put(f"Audio init failed: {e}")
        return

    engine = config.get("engine")
    model_instance = None

    if engine == "chatterbox":
        try:
            import chatterbox.models.tokenizers.tokenizer as cb_tokenizer
            if cb_tokenizer._russian_stresser is None:
                try:
                    from ruaccent import RUAccent
                    
                    class RUAccentWrapper:
                        def __init__(self):
                            self.accent = RUAccent()
                            self.accent.load(omograph_model_size='turbo', use_dictionary=True)
                        def process(self, text):
                            return self.accent.process_all(text)
                        def stress_text(self, text):
                            return self.accent.process_all(text)
                            
                    cb_tokenizer._russian_stresser = RUAccentWrapper()
                    log_queue.put("Successfully patched Chatterbox to use ruaccent.")
                except Exception as e:
                    log_queue.put(f"Failed to patch Chatterbox with ruaccent: {e}")

            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            
            log_queue.put("Loading Chatterbox Multilingual model...")
            device = config.get("device", "auto")
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            
            model_instance = ChatterboxMultilingualTTS.from_pretrained(device=device)
            log_queue.put(f"Chatterbox model loaded successfully on {device}.")
        except ImportError:
            log_queue.put("chatterbox-tts not installed.")
            return
        except Exception as e:
            log_queue.put(f"Failed to load Chatterbox: {e}")
            return

    while True:
        try:
            text = text_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        
        if text is None:
            break

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3" if engine == "openai" else ".wav", delete=False)
            tmp_path = tmp.name
            tmp.close()

            if engine == "openai":
                import requests
                headers = {
                    "Authorization": f"Bearer {config.get('api_key')}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": config.get("model"),
                    "input": text,
                    "voice": config.get("voice"),
                    "response_format": "mp3"
                }
                url = f"{config.get('base_url', 'https://api.openai.com/v1').rstrip('/')}/audio/speech"
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
            else:
                lang_id = "en"
                if any('\u0400' <= c <= '\u04FF' for c in text):
                    lang_id = "ru"
                    
                kwargs = {
                    "language_id": lang_id,
                    "repetition_penalty": 1.2,
                    "temperature": 0.7
                }
                
                tmp_wav_path = None
                ref_audio = config.get("ref_audio")
                if ref_audio and os.path.exists(ref_audio):
                    if not ref_audio.lower().endswith(".wav"):
                        try:
                            wav_data, sr = ta.load(ref_audio)
                            tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                            tmp_wav_path = tmp_wav.name
                            tmp_wav.close()
                            ta.save(tmp_wav_path, wav_data, sr)
                            kwargs["audio_prompt_path"] = tmp_wav_path
                        except Exception as e:
                            log_queue.put(f"Failed to convert ref audio: {e}")
                    else:
                        kwargs["audio_prompt_path"] = ref_audio
                        
                try:
                    wav = model_instance.generate(text, **kwargs)
                    ta.save(tmp_path, wav, model_instance.sr)
                finally:
                    if tmp_wav_path and os.path.exists(tmp_wav_path):
                        try:
                            os.remove(tmp_wav_path)
                        except Exception:
                            pass

            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(50)
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
            
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        except Exception as e:
            log_queue.put(f"TTS error: {e}")

    try:
        pygame.mixer.quit()
    except Exception:
        pass
    
    if model_instance is not None:
        del model_instance
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


class TTSModule(BaseModule):
    def __init__(self):
        self.widget = None
        self.engine_box = None
        self.device_box = None
        self.voice_box = None
        self.model_input = None
        self.api_key_input = None
        self.base_url_input = None
        self.ref_audio_input = None
        self.enable_chk = None
        self.lbl_status = None
        self.log_area = None
        
        self.process = None
        self.text_queue = None
        self.log_queue = None
        self._buffer = ""
        self.is_enabled = False
        self._punct = {'.', '!', '?', '\n', '。', '！', '？'}
        self.settings = get_settings()

        self.is_enabled = self.settings.get("tts", "enabled", default=False)

        self.log_timer = QTimer()
        self.log_timer.setInterval(100)
        self.log_timer.timeout.connect(self._poll_logs)

    def on_load(self, api):
        api.event.connect(self._on_bus_event)

    def _on_bus_event(self, name, payload):
        if not self.is_enabled or not self.process or not self.process.is_alive():
            return
        if name == "ai.token_generated":
            token = payload.get("token", "")
            self._buffer += token
            if any(p in token for p in self._punct):
                text = self._buffer.strip()
                text = re.sub(r'\[\[EXEC:.*?\]\]', '', text).strip()
                if text:
                    self.text_queue.put(text)
                self._buffer = ""
        elif name == "ai.generation_done":
            text = self._buffer.strip()
            text = re.sub(r'\[\[EXEC:.*?\]\]', '', text).strip()
            if text:
                self.text_queue.put(text)
            self._buffer = ""

    def _check_dependencies(self):
        missing = []
        checks = {
            "requests": "requests",
            "pygame": "pygame",
            "torch": "torch",
            "torchaudio": "torchaudio",
            "chatterbox-tts": "chatterbox",
            "ruaccent": "ruaccent"
        }
        for pip_name, import_name in checks.items():
            if importlib.util.find_spec(import_name) is None:
                missing.append(pip_name)
        return missing

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

        self.group_settings = QGroupBox(T("settings"))
        self.form_layout = QFormLayout(self.group_settings)

        self.engine_box = QComboBox()
        self.engine_box.addItems(["OpenAI Compatible", "Chatterbox Multilingual"])
        
        saved_engine = self.settings.get("tts", "engine", default="OpenAI Compatible")
        if saved_engine in ["OpenAI Compatible", "Chatterbox Multilingual"]:
            self.engine_box.setCurrentText(saved_engine)
            
        self.engine_box.currentTextChanged.connect(self._on_engine_changed)
        self.lbl_engine = QLabel(T("engine"))
        self.form_layout.addRow(self.lbl_engine, self.engine_box)

        self.device_box = QComboBox()
        self.device_box.addItems(["auto", "cuda", "cpu"])
        self.device_box.setCurrentText(self.settings.get("tts", "device", default="auto"))
        self.lbl_device = QLabel(T("device"))
        self.form_layout.addRow(self.lbl_device, self.device_box)

        self.voice_box = EditableComboBox() 
        self.voice_box.addItems(["alloy", "echo", "fable", "onyx", "nova", "shimmer"])
        self.voice_box.setCurrentText(self.settings.get("tts", "voice", default="alloy"))
        self.lbl_voice = QLabel(T("voice"))
        self.form_layout.addRow(self.lbl_voice, self.voice_box)

        self.model_input = QLineEdit(self.settings.get("tts", "model", default="tts-1"))
        self.lbl_model = QLabel(T("model"))
        self.form_layout.addRow(self.lbl_model, self.model_input)

        self.api_key_input = QLineEdit(self.settings.get("tts", "api_key", default=""))
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.lbl_api_key = QLabel(T("api_key"))
        self.form_layout.addRow(self.lbl_api_key, self.api_key_input)

        self.base_url_input = QLineEdit(self.settings.get("tts", "base_url", default="https://api.openai.com/v1"))
        self.lbl_base_url = QLabel(T("base_url"))
        self.form_layout.addRow(self.lbl_base_url, self.base_url_input)

        ref_layout = QHBoxLayout()
        self.ref_audio_input = QLineEdit(self.settings.get("tts", "ref_audio", default=""))
        self.ref_audio_btn = QPushButton(T("browse"))
        self.ref_audio_btn.clicked.connect(self._browse_ref_audio)
        ref_layout.addWidget(self.ref_audio_input, 1)
        ref_layout.addWidget(self.ref_audio_btn)
        self.lbl_ref_audio = QLabel(T("ref_audio"))
        self.form_layout.addRow(self.lbl_ref_audio, ref_layout)

        layout.addWidget(self.group_settings)

        self.enable_chk = QCheckBox(T("enable_tts"))
        self.enable_chk.blockSignals(True)
        self.enable_chk.setChecked(self.is_enabled)
        self.enable_chk.blockSignals(False)
        self.enable_chk.toggled.connect(self._toggle_tts)
        layout.addWidget(self.enable_chk)

        self.lbl_status = QLabel(T("status_off") if not self.is_enabled else T("status_on"))
        self.lbl_status.setStyleSheet("font-weight: bold; color: #ff4d6d;" if not self.is_enabled else "font-weight: bold; color: #10b981;")
        layout.addWidget(self.lbl_status)

        test_layout = QHBoxLayout()
        self.test_input = QLineEdit()
        self.test_input.setPlaceholderText(T("test_placeholder"))
        self.test_btn = QPushButton(T("test_btn"))
        self.test_btn.clicked.connect(self._test_tts)
        test_layout.addWidget(self.test_input, 1)
        test_layout.addWidget(self.test_btn)
        layout.addLayout(test_layout)

        self.lbl_log = QLabel(T("log"))
        layout.addWidget(self.lbl_log)

        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)

        self._on_engine_changed(self.engine_box.currentText())

        if self.is_enabled:
            QTimer.singleShot(100, lambda: self._toggle_tts(True))

        return self.widget

    def retranslate(self):
        if not self.widget:
            return
        self.group_settings.setTitle(T("settings"))
        self.lbl_engine.setText(T("engine"))
        self.lbl_device.setText(T("device"))
        self.lbl_voice.setText(T("voice"))
        self.lbl_model.setText(T("model"))
        self.lbl_api_key.setText(T("api_key"))
        self.lbl_base_url.setText(T("base_url"))
        self.lbl_ref_audio.setText(T("ref_audio"))
        self.ref_audio_btn.setText(T("browse"))
        self.enable_chk.setText(T("enable_tts"))
        self.test_btn.setText(T("test_btn"))
        self.test_input.setPlaceholderText(T("test_placeholder"))
        self.lbl_log.setText(T("log"))
        if not self.is_enabled:
            self.lbl_status.setText(T("status_off"))

    def _on_engine_changed(self, engine_text):
        is_openai = engine_text == "OpenAI Compatible"
        self.lbl_device.setVisible(not is_openai)
        self.device_box.setVisible(not is_openai)
        self.lbl_voice.setVisible(is_openai)
        self.voice_box.setVisible(is_openai)
        self.lbl_model.setVisible(is_openai)
        self.model_input.setVisible(is_openai)
        self.lbl_api_key.setVisible(is_openai)
        self.api_key_input.setVisible(is_openai)
        self.lbl_base_url.setVisible(is_openai)
        self.base_url_input.setVisible(is_openai)
        self.lbl_ref_audio.setVisible(not is_openai)
        self.ref_audio_input.setVisible(not is_openai)
        self.ref_audio_btn.setVisible(not is_openai)

    def _browse_ref_audio(self):
        path, _ = QFileDialog.getOpenFileName(self.widget, T("ref_audio"), "", "Audio Files (*.wav *.mp3 *.flac)")
        if path:
            self.ref_audio_input.setText(path)

    def _test_tts(self):
        text = self.test_input.text().strip()
        if not text:
            return
        if not self.is_enabled or not self.process or not self.process.is_alive():
            QMessageBox.warning(self.widget, T("settings"), T("test_warn"))
            return
        self.text_queue.put(text)
        self._log(f"[TEST] {text}")

    def _poll_logs(self):
        if not self.log_queue:
            return
        while True:
            try:
                msg = self.log_queue.get_nowait()
                self._log(msg)
            except queue.Empty:
                break
            except Exception:
                break

    def _toggle_tts(self, checked):
        self.is_enabled = checked
        self.settings.set("tts", "enabled", value=checked) # СОХРАНЯЕМ ГАЛОЧКУ
        
        self.settings.set("tts", "engine", value=self.engine_box.currentText())
        self.settings.set("tts", "device", value=self.device_box.currentText())
        self.settings.set("tts", "voice", value=self.voice_box.currentText())
        self.settings.set("tts", "model", value=self.model_input.text())
        self.settings.set("tts", "api_key", value=self.api_key_input.text())
        self.settings.set("tts", "base_url", value=self.base_url_input.text())
        self.settings.set("tts", "ref_audio", value=self.ref_audio_input.text())
        
        if checked:
            self.enable_chk.blockSignals(True)
            self.enable_chk.setChecked(True)
            self.enable_chk.blockSignals(False)
            
            if self.process and self.process.is_alive():
                return
            
            config = {
                "engine": "openai" if self.engine_box.currentText() == "OpenAI Compatible" else "chatterbox",
                "device": self.device_box.currentText(),
                "voice": self.voice_box.currentText(),
                "model": self.model_input.text(),
                "api_key": self.api_key_input.text(),
                "base_url": self.base_url_input.text(),
                "ref_audio": self.ref_audio_input.text()
            }
            
            self.text_queue = multiprocessing.Queue()
            self.log_queue = multiprocessing.Queue()
            
            self.process = multiprocessing.Process(
                target=_tts_process_main,
                args=(config, self.text_queue, self.log_queue)
            )
            self.process.daemon = True
            self.process.start()
            
            self.log_timer.start()
            self.lbl_status.setText(T("status_on"))
            self.lbl_status.setStyleSheet("font-weight: bold; color: #10b981;")
        else:
            self.enable_chk.blockSignals(True)
            self.enable_chk.setChecked(False)
            self.enable_chk.blockSignals(False)
            
            self.lbl_status.setText(T("status_stopping"))
            self.lbl_status.setStyleSheet("font-weight: bold; color: #f59e0b;")
            self.log_timer.stop()
            
            if self.process and self.process.is_alive():
                try:
                    self.text_queue.put(None)
                    self.process.join(timeout=2.0)
                except Exception:
                    pass
                    
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=1.0)
            
            self.process = None
            self.text_queue = None
            self.log_queue = None
            
            self.lbl_status.setText(T("status_off"))
            self.lbl_status.setStyleSheet("font-weight: bold; color: #ff4d6d;")
            self._buffer = ""
            
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def _log(self, msg):
        if self.log_area:
            self.log_area.appendPlainText(msg)

    def on_shutdown(self):
        self.settings.set("tts", "enabled", value=self.is_enabled)
        self.log_timer.stop()
        if self.process and self.process.is_alive():
            try:
                self.text_queue.put(None)
                self.process.join(timeout=2.0)
            except Exception:
                pass
            
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1.0)
        self.process = None
        self.text_queue = None
        self.log_queue = None