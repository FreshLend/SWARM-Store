import threading
import json
import re
import html
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QListWidget, QListWidgetItem,
    QTextEdit, QPushButton, QInputDialog, QMessageBox,
    QLabel, QFrame, QSizePolicy, QSplitter
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSize
from PySide6.QtGui import QFont, QTextOption, QTextDocument
from core.base_module import BaseModule
from core.module_api import get_bus
from core.i18n import tr
from core.utils import color

def T(key, **kwargs):
    return tr(key, module="chat", **kwargs)

SESSIONS_FILE = Path("config") / "chat_sessions.json"

class ChatInput(QTextEdit):
    send_triggered = Signal()
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_triggered.emit()
                event.accept()
        else:
            super().keyPressEvent(event)

class WrappedLabel(QLabel):
    def __init__(self, text=""):
        super().__init__(text)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
        self.setMinimumWidth(80)

    def sizeHint(self):
        if not self.text():
            return QSize(0, 0)
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setHtml(self.text())
        doc.setTextWidth(-1)
        ideal_w = doc.idealWidth()
        max_w = 600
        width = min(ideal_w, max_w)
        width = max(width, 80)
        doc.setTextWidth(width)
        height = doc.size().height()
        return QSize(int(width), int(height))

    def heightForWidth(self, width):
        if not self.text():
            return 0
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setHtml(self.text())
        doc.setTextWidth(width)
        return int(doc.size().height())

class MessageBubble(QWidget):
    def __init__(self, text, is_user):
        super().__init__()
        self.outer_layout = QHBoxLayout(self)
        self.outer_layout.setContentsMargins(0, 4, 0, 4)
        self.bubble = QFrame()
        self.bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
        self.bubble_layout = QVBoxLayout(self.bubble)
        self.bubble_layout.setContentsMargins(12, 8, 12, 8)
        self.label = WrappedLabel()
        self.bubble_layout.addWidget(self.label)
        
        if is_user:
            text_color = color('sel_fg')
            self.bubble.setStyleSheet(
                f"QFrame {{ background-color: {color('border_hi')}; "
                f"border-radius: 12px; border-bottom-right-radius: 2px; }}"
            )
            self.outer_layout.addStretch()
            self.outer_layout.addWidget(self.bubble)
        else:
            text_color = color('fg')
            self.bubble.setStyleSheet(
                f"QFrame {{ background-color: {color('bg3')}; "
                f"border-radius: 12px; border-bottom-left-radius: 2px; }}"
            )
            self.outer_layout.addWidget(self.bubble)
            self.outer_layout.addStretch()
            
        self.label.setStyleSheet(f"background: transparent; color: {text_color};")
        self.setText(text)

    def setText(self, text, is_markdown=False):
        if not text:
            self.label.setText("")
            return
        
        text = html.unescape(text)
        safe_text = html.escape(text, quote=False)
        
        safe_text = safe_text.replace("&amp;quot;", '"').replace("&amp;quote;", '"')
        safe_text = safe_text.replace("&quot;", '"').replace("&quote;", '"')
        safe_text = safe_text.replace("&#x27;", "'").replace("&#x22;", '"')
        
        safe_text = re.sub(
            r'\[\[EXEC:\s*(.*?)\s*\]\]', 
            r'<span style="color: #f59e0b; font-weight: bold; background-color: #1f2937; padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace;">[EXEC] \1</span>', 
            safe_text
        )

        safe_text = re.sub(r'([^\s]{25})', r'\1&#8203;', safe_text)

        if is_markdown:
            safe_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_text)
            safe_text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', safe_text)
            safe_text = safe_text.replace("\n", "<br>")
            
        self.label.setText(safe_text)
        self.label.updateGeometry()

class ChatSignals(QObject):
    token_received = Signal(str, str) 
    finished = Signal(str, str)

class ChatModule(BaseModule):
    def __init__(self):
        self.widget = None
        self.scroll_area = None
        self.chat_container = None
        self.chat_layout = None
        self.input_field = None
        self.send_btn = None
        self.sessions_list = None
        self.sessions = {}
        self.current_session = "Default"
        self.current_ai_text = ""
        self.current_ai_bubble = None
        self.is_thinking = False
        
        self.active_generations = set()
        self.stop_events = {}
        
        self.signals = ChatSignals()
        self.signals.token_received.connect(self._append_token)
        self.signals.finished.connect(self._on_ai_finished)

    def on_load(self, api):
        self._load_sessions()
        api.event.connect(self._on_bus_event)
        api.register_service(
            self.name, "chat.get_sessions", lambda: list(self.sessions.keys()),
            "chat.get_sessions() -> list[str]"
        )

    def _on_bus_event(self, name, payload):
        if name == "chat.inject_message":
            text = payload.get("text", "").strip()
            target_session = payload.get("session_name", self.current_session)
            
            if text:
                if target_session not in self.sessions:
                    self.sessions[target_session] = []
                
                if self.widget:
                    if self.current_session != target_session:
                        self.current_session = target_session
                        self._refresh_sessions_list()
                        self._render_current_session()
                    self.input_field.setPlainText(text)
                    self._send_message()
                else:
                    self._process_user_message(text, target_session, update_ui=False)

    def create_widget(self) -> QWidget:
        self.widget = QWidget()
        main_layout = QVBoxLayout(self.widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        
        sidebar_widget = QWidget()
        sidebar_widget.setStyleSheet(f"background-color: {color('bg')};")
        sidebar_widget.setMinimumWidth(180)
        sidebar_widget.setMaximumWidth(250)
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(10)

        new_btn = QPushButton(T("new_session"))
        new_btn.clicked.connect(self._new_session)
        sidebar_layout.addWidget(new_btn)

        self.sessions_list = QListWidget()
        self.sessions_list.setStyleSheet(
            f"background-color: {color('bg2')}; border: 1px solid {color('border')}; border-radius: 6px;"
        )
        self.sessions_list.itemClicked.connect(self._on_session_clicked)
        sidebar_layout.addWidget(self.sessions_list, 1)

        del_btn = QPushButton(T("delete_session"))
        del_btn.setStyleSheet(f"background-color: {color('danger')}; color: white;")
        del_btn.clicked.connect(self._delete_session)
        sidebar_layout.addWidget(del_btn)

        self.current_session = T("default_session")

        splitter.addWidget(sidebar_widget)

        chat_widget = QWidget()
        chat_layout = QVBoxLayout(chat_widget)
        chat_layout.setContentsMargins(20, 12, 20, 12)
        chat_layout.setSpacing(10)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        
        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_layout.setSpacing(4)
        
        self.scroll_area.setWidget(self.chat_container)
        chat_layout.addWidget(self.scroll_area, 1)

        bottom = QHBoxLayout()
        self.input_field = ChatInput()
        font = QFont("Consolas", 11)
        self.input_field.setFont(font)
        self.input_field.setPlaceholderText(T("placeholder") + " (Shift+Enter for new line)")
        self.input_field.setFixedHeight(80)
        self.input_field.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.input_field.send_triggered.connect(self._send_message)
        bottom.addWidget(self.input_field, 1)

        self.send_btn = QPushButton(T("send"))
        self.send_btn.setFixedHeight(80)
        self.send_btn.clicked.connect(self._send_message)
        bottom.addWidget(self.send_btn)

        chat_layout.addLayout(bottom)
        splitter.addWidget(chat_widget)
        
        splitter.setSizes([200, 800])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        
        main_layout.addWidget(splitter)

        self._load_sessions()
        return self.widget

    def _load_sessions(self):
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        default_name = T("default_session")
        
        if SESSIONS_FILE.exists():
            try:
                with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                    self.sessions = json.load(f)
            except Exception:
                self.sessions = {default_name: []}
        else:
            self.sessions = {default_name: []}
            self._save_sessions()

        if not self.sessions:
            self.sessions = {default_name: []}
        if self.current_session not in self.sessions:
            self.current_session = list(self.sessions.keys())[0]

        if hasattr(self, "sessions_list") and self.sessions_list:
            self._refresh_sessions_list()
            self._render_current_session()

    def _refresh_sessions_list(self):
        if not self.sessions_list:
            return
        self.sessions_list.clear()
        for name in self.sessions.keys():
            item = QListWidgetItem(name)
            self.sessions_list.addItem(item)
            
        for i in range(self.sessions_list.count()):
            if self.sessions_list.item(i).text() == self.current_session:
                self.sessions_list.setCurrentRow(i)
                break

    def _save_sessions(self):
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.sessions, f, ensure_ascii=False, indent=2)

    def _new_session(self):
        name, ok = QInputDialog.getText(self.widget, T("new_session"), T("session_name_prompt"))
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.sessions:
            QMessageBox.warning(self.widget, T("new_session"), T("session_exists"))
            return
        
        self.sessions[name] = []
        self._save_sessions()
        self.current_session = name
        self._refresh_sessions_list()
        self._render_current_session()
        get_bus().emit_event("chat.sessions_changed")

    def _delete_session(self):
        reply = QMessageBox.question(self.widget, T("delete_session"), T("confirm_delete"), 
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            session_to_delete = self.current_session
            
            if session_to_delete in self.active_generations:
                if session_to_delete in self.stop_events:
                    self.stop_events[session_to_delete].set()
                self.active_generations.discard(session_to_delete)

            del self.sessions[session_to_delete]
            
            if not self.sessions:
                self.sessions = {T("default_session"): []}
                
            self.current_session = list(self.sessions.keys())[0]
            self._save_sessions()
            self._refresh_sessions_list()
            self._render_current_session()
            get_bus().emit_event("chat.sessions_changed")

    def _on_session_clicked(self, item):
        self.current_session = item.text()
        self._render_current_session()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _update_ui_state(self):
        if not self.widget or not self.input_field:
            return
        if self.current_session in self.active_generations:
            self.input_field.setEnabled(False)
            self.send_btn.setEnabled(False)
        else:
            self.input_field.setEnabled(True)
            self.send_btn.setEnabled(True)

    def _render_current_session(self):
        self._clear_layout(self.chat_layout)
        self.current_ai_bubble = None
        self.current_ai_text = ""
        
        history = self.sessions.get(self.current_session, [])
        for msg in history:
            role = msg.get("role")
            content = msg.get("content", "").strip()
            is_user = (role == "user")
            bubble = MessageBubble(content, is_user)
            if not is_user:
                bubble.setText(content, is_markdown=True)
            self.chat_layout.addWidget(bubble)
            
        self._update_ui_state()
        self._scroll_to_bottom()

    def retranslate(self):
        if self.widget:
            self.input_field.setPlaceholderText(T("placeholder") + " " + T("shift_enter"))
            self.send_btn.setText(T("send"))

    def _scroll_to_bottom(self):
        QTimer.singleShot(50, self._do_scroll)

    def _do_scroll(self):
        if self.scroll_area:
            sb = self.scroll_area.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _append_token(self, token, session_name):
        if self.current_session != session_name:
            return
            
        if self.is_thinking and self.current_ai_bubble:
            if not token.strip():
                return
            self.is_thinking = False
            self.current_ai_text = ""
            self.current_ai_bubble.bubble.setStyleSheet(
                f"QFrame {{ background-color: {color('bg3')}; "
                f"border-radius: 12px; border-bottom-left-radius: 2px; }}"
            )
            self.current_ai_bubble.label.setStyleSheet(f"background: transparent; color: {color('fg')};")
            self.current_ai_bubble.setText("", is_markdown=False)

        self.current_ai_text += token
        if self.current_ai_bubble:
            self.current_ai_bubble.setText(self.current_ai_text, is_markdown=False)
        
        sb = self.scroll_area.verticalScrollBar()
        if sb.value() >= sb.maximum() - 100:
            sb.setValue(sb.maximum())

    def _on_ai_finished(self, full_text, session_name):
        self.active_generations.discard(session_name)
        if session_name in self.stop_events:
            del self.stop_events[session_name]

        if self.current_session == session_name:
            self._update_ui_state()
            if self.widget:
                self.input_field.setFocus()

        if session_name not in self.sessions:
            return

        clean_text = full_text.strip()
        if clean_text:
            self.sessions[session_name].append({"role": "assistant", "content": clean_text})
            self._save_sessions()
        
        if self.current_ai_bubble and self.current_session == session_name:
            self.current_ai_bubble.setText(clean_text, is_markdown=True)
            self.current_ai_bubble = None
            if self.widget:
                self._scroll_to_bottom()

    def _send_message(self):
        text = self.input_field.toPlainText().strip()
        if not text:
            return
        self.input_field.clear()
        self._process_user_message(text, self.current_session, update_ui=True)

    def _process_user_message(self, text, session_name=None, update_ui=True):
        if session_name is None:
            session_name = self.current_session

        if update_ui and self.widget:
            user_bubble = MessageBubble(text, is_user=True)
            self.chat_layout.addWidget(user_bubble)
            
            self.current_ai_text = ""
            self.current_ai_bubble = MessageBubble(T("thinking"), is_user=False)
            self.current_ai_bubble.bubble.setStyleSheet(
                f"QFrame {{ background-color: {color('bg3')}; "
                f"border-radius: 12px; border-bottom-left-radius: 2px; }}"
            )
            self.current_ai_bubble.label.setStyleSheet(f"background: transparent; color: {color('fg_dim')}; font-style: italic;")
            self.is_thinking = True
            self.chat_layout.addWidget(self.current_ai_bubble)
            self._scroll_to_bottom()
        else:
            self.current_ai_bubble = None
            self.is_thinking = False

        self.sessions[session_name].append({"role": "user", "content": text})
        self._save_sessions()

        try:
            bus = get_bus()
            if not bus.has_service("ai.generate"):
                if update_ui:
                    self._on_ai_finished(f"[Error: {T('no_ai')}]", session_name)
                return

            messages = list(self.sessions[session_name])
            
            self.active_generations.add(session_name)
            self.stop_events[session_name] = threading.Event()
            self._update_ui_state()

            threading.Thread(
                target=self._run_generation, 
                args=(messages, session_name, self.stop_events[session_name]), 
                daemon=True
            ).start()

        except Exception as e:
            if update_ui:
                self._on_ai_finished(f"[Error: {e}]", session_name)

    def _run_generation(self, messages, session_name, stop_event):
        def callback(token):
            if stop_event.is_set():
                raise Exception("__cancelled__")
            self.signals.token_received.emit(token, session_name)

        try:
            result = get_bus().call("ai.generate", messages, callback, session_name=session_name)
            
            if result.get("ok"):
                self.signals.finished.emit(result.get("text", ""), session_name)
            else:
                err = result.get("error", "")
                if "__cancelled__" in err or stop_event.is_set():
                    self.signals.finished.emit("", session_name)
                    return
                self.signals.token_received.emit(f"\n[Error: {err}]", session_name)
                self.signals.finished.emit(f"[Error: {err}]", session_name)
        except Exception as e:
            if "__cancelled__" in str(e) or stop_event.is_set():
                self.signals.finished.emit("", session_name)
                return
            self.signals.token_received.emit(f"\n[Error: {e}]", session_name)
            self.signals.finished.emit(f"[Error: {e}]", session_name)