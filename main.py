import glob
import json
import os
import re
import sys
from datetime import datetime
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

CONFIG_FILE = "config.json"


# --- リソースパスを取得するヘルパー関数（PyInstaller対応） ---
def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# --- 1. 最新のVRChatログファイルを取得 ---
def get_latest_log_file():
    appdata = os.getenv("LOCALAPPDATA")
    if not appdata:
        return None
    log_dir = os.path.abspath(
        os.path.join(appdata, "..", "LocalLow", "VRChat", "VRChat")
    )

    list_of_files = glob.glob(os.path.join(log_dir, "output_log_*.txt"))
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getmtime)


# --- ログから時刻 (HH:MM:SS) を抽出 ---
def extract_time(line: str) -> str:
    match = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", line)
    if match:
        return match.group(1)
    return datetime.now().strftime("%H:%M:%S")


# --- 設定の読み書き ---
def load_config():
    default_config = {"show_history": True, "max_history_count": 7}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                default_config.update(json.load(f))
        except Exception:
            pass
    return default_config


def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"設定保存エラー: {e}")


# --- 2. トークン計算状態管理クラス ---
class TokenTracker:

    def __init__(self, max_history_count=7):
        self.collected_tokens = 0
        self.total_tokens = 0
        self.pending_spawn_count = 0
        self.in_stage = False
        self.boss_fought_in_stage = False  # 各ステージ1回のみの判定用
        self.is_final_boss = False
        self.history_logs = []
        self.max_history_count = max_history_count

    def get_formatted_history(self) -> str:
        display_logs = self.history_logs[-self.max_history_count :]
        return "\n".join(display_logs)

    def set_max_history_count(self, count: int) -> str:
        self.max_history_count = count
        return self.get_formatted_history()

    def add_history(self, log_time: str, text: str):
        self.history_logs.append(f"{log_time} {text}")
        if len(self.history_logs) > 20:
            self.history_logs.pop(0)

    def parse_line(self, line: str) -> tuple[str, str, bool] | None:
        log_time = extract_time(line)

        # 1. ラスボスステージ突入の検知
        if "ECLIPTICA - now in stage: Stage_Bringer on phase: 1" in line:
            self.is_final_boss = True
            self.in_stage = False
            self.add_history(log_time, "now in Stage_Bringer: DONE")
            return "tokens: DONE\nGOOD LUCK", self.get_formatted_history(), True

        # 2. トークン生成ログの検知
        if "spawn token" in line:
            if self.is_final_boss:
                self.is_final_boss = False  # ラスボスモード解除

            self.pending_spawn_count += 1
            self.add_history(log_time, "spawn token: total+1")
            token_text = f"tokens: {self.collected_tokens}/{self.total_tokens}"
            return token_text, self.get_formatted_history(), False

        # ラスボス戦中は計算スキップ
        if self.is_final_boss:
            return None

        # 3. 通常ステージ移行ログの検知
        if "now in stage:" in line:
            self.total_tokens = self.pending_spawn_count
            self.collected_tokens = 0
            self.pending_spawn_count = 0
            self.in_stage = True
            self.boss_fought_in_stage = False
            self.add_history(log_time, "now in stage: reset tokens")
            token_text = f"tokens: {self.collected_tokens}/{self.total_tokens}"
            return token_text, self.get_formatted_history(), False

        # 4. トークン取得（セッション保存）の検知
        if "ECLIPTICA saving SESSION ID" in line:
            if self.in_stage:
                self.collected_tokens += 1
                self.add_history(log_time, "saving SESSION ID: tokens+1")
                token_text = (
                    f"tokens: {self.collected_tokens}/{self.total_tokens}"
                )
                return token_text, self.get_formatted_history(), False
            return None

        # 5. ボス戦突入時の相殺処理 (-1)
        if "now fighting boss" in line:
            if not self.boss_fought_in_stage:
                self.boss_fought_in_stage = True
                self.collected_tokens -= 1
                self.add_history(log_time, "now fighting boss: tokens-1")
                token_text = (
                    f"tokens: {self.collected_tokens}/{self.total_tokens}"
                )
                return token_text, self.get_formatted_history(), False
            return None

        # 6. インターミッション突入時の相殺処理 (-2)
        if "now in intermission" in line:
            self.in_stage = False
            self.collected_tokens -= 2
            self.add_history(log_time, "now in intermission: tokens-2")
            token_text = f"tokens: {self.collected_tokens}/{self.total_tokens}"
            return token_text, self.get_formatted_history(), False

        return None


# --- 3. ログ監視スレッド ---
class LogMonitorThread(QThread):
    # 3つ目の引数(bool)として is_final_boss を追加
    display_update_signal = pyqtSignal(str, str, bool)

    def __init__(self, log_path, max_history_count):
        super().__init__()
        self.log_path = log_path
        self.running = True
        self.tracker = TokenTracker(max_history_count)

    def update_max_history(self, count: int) -> str:
        return self.tracker.set_max_history_count(count)

    def run(self):
        with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)

            while self.running:
                line = f.readline()
                if line:
                    result = self.tracker.parse_line(line.strip())
                    if result:
                        token_text, history_text, is_final_boss = result
                        self.display_update_signal.emit(
                            token_text, history_text, is_final_boss
                        )
                else:
                    self.msleep(100)

    def stop(self):
        self.running = False


# --- 4. 設定ダイアログ ---
class SettingsDialog(QDialog):

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Token Overlay 設定")
        self.setFixedSize(260, 140)

        layout = QVBoxLayout()
        form_layout = QFormLayout()

        self.show_history_cb = QCheckBox()
        self.show_history_cb.setChecked(self.config.get("show_history", True))
        form_layout.addRow("レシート (履歴) 表示:", self.show_history_cb)

        self.history_count_spin = QSpinBox()
        self.history_count_spin.setRange(1, 20)
        self.history_count_spin.setValue(
            self.config.get("max_history_count", 7)
        )
        form_layout.addRow("履歴表示件数:", self.history_count_spin)

        layout.addLayout(form_layout)

        btn_box = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("キャンセル")
        cancel_btn.clicked.connect(self.reject)

        btn_box.addWidget(save_btn)
        btn_box.addWidget(cancel_btn)
        layout.addLayout(btn_box)

        self.setLayout(layout)

    def get_settings(self):
        return {
            "show_history": self.show_history_cb.isChecked(),
            "max_history_count": self.history_count_spin.value(),
        }


# --- 5. オーバーレイUI ---
class TokenOverlayWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.latest_history_text = ""
        self.init_ui()

        log_path = get_latest_log_file()
        if log_path:
            print(f"監視対象ログ: {log_path}")
            self.monitor_thread = LogMonitorThread(
                log_path, self.config.get("max_history_count", 7)
            )
            self.monitor_thread.display_update_signal.connect(
                self.update_token_label
            )
            self.monitor_thread.start()
        else:
            self.token_label.setText("Log NotFound")

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.history_label = QLabel("", self)
        self.history_label.setStyleSheet("""
            QLabel {
                color: #CCCCCC;
                font-size: 13px;
                font-family: 'Consolas', 'Yu Gothic UI', monospace;
                background-color: rgba(0, 0, 0, 0.70);
                border-radius: 4px;
                padding: 6px 10px;
            }
        """)
        self.history_label.setVisible(self.config.get("show_history", True))

        self.token_label = QLabel("tokens: 0/0", self)
        self.token_label.setStyleSheet("""
            QLabel {
                color: #FFFFFF;
                font-size: 20px;
                font-weight: bold;
                font-family: 'Consolas', 'Yu Gothic UI', sans-serif;
                background-color: rgba(0, 0, 0, 0.75);
                border-radius: 6px;
                padding: 6px 12px;
            }
        """)

        layout.addWidget(self.history_label)
        layout.addWidget(self.token_label)
        self.setLayout(layout)

        self.adjust_position()

    def update_token_label(
        self, token_text: str, history_text: str, is_final_boss: bool
    ):
        self.latest_history_text = history_text
        self.token_label.setText(token_text)

        # ラスボス戦中の場合は強制的にレシートを非表示
        if is_final_boss:
            self.history_label.setVisible(False)
        else:
            # 通常時はユーザーの設定（show_history）に従って表示
            if self.config.get("show_history", True) and history_text:
                self.history_label.setText(history_text)
                self.history_label.setVisible(True)
            else:
                self.history_label.setVisible(False)

        self.adjust_position()

    def open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_config = dialog.get_settings()
            self.config.update(new_config)
            save_config(self.config)

            # 件数変更をスレッドに伝え、即座に更新された履歴テキストを受け取る
            if hasattr(self, "monitor_thread"):
                self.latest_history_text = (
                    self.monitor_thread.update_max_history(
                        self.config["max_history_count"]
                    )
                )

            # 保存した瞬間に画面の表示を即時更新
            if self.config["show_history"] and self.latest_history_text:
                self.history_label.setText(self.latest_history_text)
                self.history_label.setVisible(True)
            else:
                self.history_label.setVisible(False)

            self.adjust_position()

    def adjust_position(self):
        self.adjustSize()

        screen = QApplication.primaryScreen().geometry()
        margin = 30
        x = screen.width() - self.width() - margin
        y = screen.height() - self.height() - margin
        self.move(x, y)

    def closeEvent(self, event):
        if hasattr(self, "monitor_thread"):
            self.monitor_thread.stop()
            self.monitor_thread.wait()
        event.accept()


# --- 6. 実行処理 ＆ タスクトレイ設定 ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # アイコンファイルのパスを取得
    icon_path = resource_path("icon.ico")
    app_icon = QIcon(icon_path)

    # アプリ全体＆タスクバー用のアイコンを設定
    app.setWindowIcon(app_icon)

    window = TokenOverlayWindow()
    # ウィンドウ個別にもアイコンを設定（タスクバー表示用）
    window.setWindowIcon(app_icon)
    window.show()

    # システムタスクトレイの設定（作成したアイコンを使用）
    tray_icon = QSystemTrayIcon(app_icon, app)
    tray_icon.setToolTip("Ecliptica Token Tracker")

    tray_menu = QMenu()

    settings_action = QAction("設定", app)
    settings_action.triggered.connect(window.open_settings)
    tray_menu.addAction(settings_action)

    tray_menu.addSeparator()

    quit_action = QAction("終了", app)
    quit_action.triggered.connect(app.quit)
    tray_menu.addAction(quit_action)

    tray_icon.setContextMenu(tray_menu)
    tray_icon.activated.connect(
        lambda reason: window.open_settings()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )
    tray_icon.show()

    sys.exit(app.exec())