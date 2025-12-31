# ytdlp_gui.py
# Feature-rich yt-dlp GUI for macOS (PySide6 + yt-dlp API)
#
# Setup:
#   brew install ffmpeg
#   python3 -m pip install -U yt-dlp PySide6
#
# Run:
#   python3 ytdlp_gui.py

import os
import re
import sys
import time
import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QFileDialog, QComboBox, QCheckBox,
    QProgressBar, QListWidget, QListWidgetItem, QMessageBox, QGroupBox, QSpinBox
)

import yt_dlp


# -----------------------------
# Utilities
# -----------------------------
def safe_strip_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def human_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    u = 0
    n = float(n)
    while n >= 1024 and u < len(units) - 1:
        n /= 1024
        u += 1
    return f"{n:.2f} {units[u]}"


def parse_rate_limit(s: str) -> int | None:
    """
    Convert strings like "500K", "2M", "1.5M", "3G" to bytes/sec.
    Returns None if empty/invalid.
    """
    s = s.strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG])?B?", s, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}.get(unit, 1)
    return int(val * mult)


# -----------------------------
# Signals / Models
# -----------------------------
class WorkerSignals(QObject):
    log = Signal(str)
    progress = Signal(float, str)  # percent, status
    formats_ready = Signal(list)   # list of (format_id, display)
    item_started = Signal(str)
    item_done = Signal(str)
    error = Signal(str)
    finished = Signal()


@dataclass
class DownloadItem:
    url: str


# -----------------------------
# yt-dlp runner
# -----------------------------
class YtDlpRunner:
    def __init__(self, signals: WorkerSignals, get_cancel_flag):
        self.signals = signals
        self.get_cancel_flag = get_cancel_flag
        self._last_update = 0.0

    def _progress_hook(self, d):
        if self.get_cancel_flag():
            raise yt_dlp.utils.DownloadError("Canceled by user")

        status = d.get("status")
        if status == "downloading":
            now = time.time()
            if now - self._last_update < 0.15:
                return
            self._last_update = now

            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100.0) if total else 0.0

            speed = d.get("speed")
            eta = d.get("eta")
            msg = (
                f"{pct:5.1f}%  |  {human_bytes(downloaded)} / {human_bytes(total)}"
                f"  |  {human_bytes(speed)}/s  |  ETA {eta if eta is not None else '?'}s"
            )
            self.signals.progress.emit(pct, msg)

        elif status == "finished":
            self.signals.progress.emit(100.0, "Download finished. Post-processing…")

    def list_formats(self, url: str, base_opts: dict):
        opts = dict(base_opts)
        opts.update({"skip_download": True, "quiet": True, "no_warnings": True})

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        fmts = []
        for f in (info.get("formats") or []):
            fid = f.get("format_id", "")
            ext = f.get("ext", "")
            res = f.get("resolution") or ""
            note = f.get("format_note") or ""

            acodec = f.get("acodec") or ""
            vcodec = f.get("vcodec") or ""
            abr = f.get("abr")
            tbr = f.get("tbr")
            fps = f.get("fps")
            filesize = f.get("filesize") or f.get("filesize_approx")

            flags = []
            if vcodec and vcodec != "none":
                flags.append(vcodec)
            if acodec and acodec != "none":
                flags.append(acodec)
            if abr:
                flags.append(f"abr:{abr}")
            if tbr:
                flags.append(f"tbr:{tbr}")
            if fps:
                flags.append(f"{fps}fps")
            if filesize:
                flags.append(human_bytes(filesize))

            flags_txt = " | ".join(flags)
            display = f"{fid:>5}  {ext:<4}  {res:<10}  {note:<12}  {flags_txt}"
            fmts.append((fid, display))

        def sort_key(tup):
            _, display = tup
            m = re.search(r"(\d{3,4})p", display)
            p = int(m.group(1)) if m else 0
            return (-p, display)

        fmts.sort(key=sort_key)
        self.signals.formats_ready.emit(fmts)

    def download(self, url: str, opts: dict):
        opts = dict(opts)
        hooks = list(opts.get("progress_hooks", []))
        hooks.append(self._progress_hook)
        opts["progress_hooks"] = hooks

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])


# -----------------------------
# GUI
# -----------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("yt-dlp GUI (macOS)")
        self.resize(1100, 740)

        self.signals = WorkerSignals()
        self.cancel_flag = False
        self.current_thread: threading.Thread | None = None
        self.queue: list[DownloadItem] = []

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)

        # Top: input + queue
        top = QHBoxLayout()
        main.addLayout(top)

        left = QVBoxLayout()
        top.addLayout(left, 2)

        left.addWidget(QLabel("URL(s) — one per line"))
        self.url_box = QTextEdit()
        self.url_box.setPlaceholderText("Paste URLs here (videos/playlists).")
        left.addWidget(self.url_box)

        add_row = QHBoxLayout()
        left.addLayout(add_row)
        self.btn_add = QPushButton("Add to queue")
        self.btn_clear_input = QPushButton("Clear input")
        add_row.addWidget(self.btn_add)
        add_row.addWidget(self.btn_clear_input)

        right = QVBoxLayout()
        top.addLayout(right, 1)

        right.addWidget(QLabel("Queue"))
        self.queue_list = QListWidget()
        right.addWidget(self.queue_list)

        qbtns = QHBoxLayout()
        right.addLayout(qbtns)
        self.btn_remove = QPushButton("Remove selected")
        self.btn_clear_queue = QPushButton("Clear queue")
        qbtns.addWidget(self.btn_remove)
        qbtns.addWidget(self.btn_clear_queue)

        # Options
        opts_box = QGroupBox("Options")
        main.addWidget(opts_box)
        grid = QGridLayout(opts_box)

        # Output folder
        grid.addWidget(QLabel("Output folder"), 0, 0)
        self.out_dir = QLineEdit(os.path.expanduser("~/Downloads"))
        self.btn_browse = QPushButton("Browse")
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir)
        out_row.addWidget(self.btn_browse)
        out_wrap = QWidget()
        out_wrap.setLayout(out_row)
        grid.addWidget(out_wrap, 0, 1, 1, 3)

        # Format selection
        grid.addWidget(QLabel("Format"), 1, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItem("best (default)", "best")
        self.btn_list_formats = QPushButton("List formats for first URL")
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(self.format_combo, 2)
        fmt_row.addWidget(self.btn_list_formats, 1)
        fmt_wrap = QWidget()
        fmt_wrap.setLayout(fmt_row)
        grid.addWidget(fmt_wrap, 1, 1, 1, 3)

        # Audio extraction
        self.chk_extract_audio = QCheckBox("Extract audio")
        self.audio_fmt = QComboBox()
        self.audio_fmt.addItems(["mp3", "m4a", "wav", "flac", "opus"])
        self.audio_bitrate = QComboBox()
        self.audio_bitrate.addItems(["192K", "256K", "320K"])
        audio_row = QHBoxLayout()
        audio_row.addWidget(self.chk_extract_audio)
        audio_row.addWidget(QLabel("Format:"))
        audio_row.addWidget(self.audio_fmt)
        audio_row.addWidget(QLabel("Bitrate:"))
        audio_row.addWidget(self.audio_bitrate)
        audio_wrap = QWidget()
        audio_wrap.setLayout(audio_row)
        grid.addWidget(audio_wrap, 2, 1, 1, 3)

        # Subtitles
        self.chk_subs = QCheckBox("Download subtitles")
        self.chk_auto_subs = QCheckBox("Auto subs")
        self.sub_lang = QLineEdit("en")
        sub_row = QHBoxLayout()
        sub_row.addWidget(self.chk_subs)
        sub_row.addWidget(self.chk_auto_subs)
        sub_row.addWidget(QLabel("Lang (comma-separated):"))
        sub_row.addWidget(self.sub_lang)
        sub_wrap = QWidget()
        sub_wrap.setLayout(sub_row)
        grid.addWidget(sub_wrap, 3, 1, 1, 3)

        # Metadata / thumbnail / chapters file
        self.chk_metadata = QCheckBox("Embed metadata")
        self.chk_thumbnail = QCheckBox("Embed thumbnail")
        self.chk_chapters = QCheckBox("Write info JSON (chapters/metadata)")
        extras_row = QHBoxLayout()
        extras_row.addWidget(self.chk_metadata)
        extras_row.addWidget(self.chk_thumbnail)
        extras_row.addWidget(self.chk_chapters)
        extras_wrap = QWidget()
        extras_wrap.setLayout(extras_row)
        grid.addWidget(extras_wrap, 4, 1, 1, 3)

        # Playlist / rate limit
        self.chk_playlist = QCheckBox("Allow playlists")
        self.chk_playlist.setChecked(True)
        self.rate_limit = QLineEdit("")
        self.rate_limit.setPlaceholderText("e.g. 2M (optional)")
        perf_row = QHBoxLayout()
        perf_row.addWidget(self.chk_playlist)
        perf_row.addWidget(QLabel("Rate limit:"))
        perf_row.addWidget(self.rate_limit)
        perf_wrap = QWidget()
        perf_wrap.setLayout(perf_row)
        grid.addWidget(perf_wrap, 5, 1, 1, 3)

        # Cookies
        grid.addWidget(QLabel("Cookies"), 6, 0)
        self.cookies_path = QLineEdit("")
        self.cookies_path.setPlaceholderText("Optional cookies.txt (Netscape format)")
        self.btn_cookies = QPushButton("Choose…")
        cookies_row = QHBoxLayout()
        cookies_row.addWidget(self.cookies_path)
        cookies_row.addWidget(self.btn_cookies)
        cookies_wrap = QWidget()
        cookies_wrap.setLayout(cookies_row)
        grid.addWidget(cookies_wrap, 6, 1, 1, 3)

        # Run buttons
        run_row = QHBoxLayout()
        main.addLayout(run_row)
        self.btn_download = QPushButton("Download queue")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        run_row.addWidget(self.btn_download)
        run_row.addWidget(self.btn_cancel)

        # Progress + status
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status = QLabel("Idle")
        main.addWidget(self.progress)
        main.addWidget(self.status)

        # Log
        main.addWidget(QLabel("Log"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        main.addWidget(self.log, 1)

        # Wire UI
        self.btn_browse.clicked.connect(self.choose_outdir)
        self.btn_cookies.clicked.connect(self.choose_cookies)
        self.btn_add.clicked.connect(self.add_to_queue)
        self.btn_clear_input.clicked.connect(lambda: self.url_box.setPlainText(""))
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear_queue.clicked.connect(self.clear_queue)
        self.btn_list_formats.clicked.connect(self.list_formats_for_first_url)
        self.btn_download.clicked.connect(self.start_download)
        self.btn_cancel.clicked.connect(self.cancel)

        # Wire signals
        self.signals.log.connect(self.append_log)
        self.signals.progress.connect(self.update_progress)
        self.signals.formats_ready.connect(self.populate_formats)
        self.signals.item_started.connect(self.on_item_started)
        self.signals.item_done.connect(self.on_item_done)
        self.signals.error.connect(self.on_error)
        self.signals.finished.connect(self.on_finished)

    # -----------------------------
    # UI helpers
    # -----------------------------
    def append_log(self, text: str):
        # Fix for your error: QTextCursor is in PySide6.QtGui (not QtCore.Qt)
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    def update_progress(self, pct: float, status: str):
        self.progress.setValue(max(0, min(100, int(pct))))
        self.status.setText(status)

    def on_item_started(self, url: str):
        self.status.setText(f"Downloading: {url}")

    def on_item_done(self, url: str):
        self.signals.log.emit(f"Done: {url}\n")

    def on_error(self, msg: str):
        self.signals.log.emit(f"ERROR: {msg}\n")

    def on_finished(self):
        self.btn_cancel.setEnabled(False)
        self.btn_download.setEnabled(True)
        self.btn_list_formats.setEnabled(True)
        self.status.setText("Canceled" if self.cancel_flag else "Idle")
        self.signals.log.emit("=== Task finished ===\n")

    # -----------------------------
    # UI actions
    # -----------------------------
    def choose_outdir(self):
        path = QFileDialog.getExistingDirectory(self, "Choose Output Folder", self.out_dir.text())
        if path:
            self.out_dir.setText(path)

    def choose_cookies(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cookies.txt", os.path.expanduser("~"),
            "Text Files (*.txt);;All Files (*)"
        )
        if path:
            self.cookies_path.setText(path)

    def add_to_queue(self):
        urls = safe_strip_lines(self.url_box.toPlainText())
        if not urls:
            return
        for u in urls:
            self.queue.append(DownloadItem(url=u))
            self.queue_list.addItem(QListWidgetItem(u))
        self.url_box.setPlainText("")

    def remove_selected(self):
        idxs = sorted([i.row() for i in self.queue_list.selectedIndexes()], reverse=True)
        for idx in idxs:
            self.queue.pop(idx)
            self.queue_list.takeItem(idx)

    def clear_queue(self):
        self.queue.clear()
        self.queue_list.clear()

    def list_formats_for_first_url(self):
        # Prefer URL in input; else first in queue
        urls = safe_strip_lines(self.url_box.toPlainText())
        if urls:
            url = urls[0]
        elif self.queue:
            url = self.queue[0].url
        else:
            QMessageBox.warning(self, "No URL", "Paste a URL in the input box or add something to the queue.")
            return

        self.signals.log.emit(f"Listing formats for: {url}\n")
        self.run_in_thread(target="list_formats", url=url)

    def start_download(self):
        if not self.queue:
            QMessageBox.warning(self, "Queue empty", "Add at least one URL to the queue.")
            return
        outdir = self.out_dir.text().strip()
        if not outdir or not os.path.isdir(outdir):
            QMessageBox.warning(self, "Invalid output folder", "Choose a valid output folder.")
            return

        self.cancel_flag = False
        self.btn_cancel.setEnabled(True)
        self.btn_download.setEnabled(False)
        self.btn_list_formats.setEnabled(False)
        self.progress.setValue(0)
        self.status.setText("Starting…")
        self.signals.log.emit("=== Download started ===\n")

        self.run_in_thread(target="download_queue")

    def cancel(self):
        self.cancel_flag = True
        self.signals.log.emit("Cancel requested…\n")
        self.btn_cancel.setEnabled(False)

    # -----------------------------
    # Thread runner
    # -----------------------------
    def run_in_thread(self, target: str, url: str | None = None):
        if self.current_thread and self.current_thread.is_alive():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return

        def worker():
            runner = YtDlpRunner(self.signals, lambda: self.cancel_flag)
            base_opts = self.build_base_opts()
            try:
                if target == "list_formats":
                    runner.list_formats(url, base_opts)
                elif target == "download_queue":
                    self.download_queue(runner, base_opts)
                else:
                    self.signals.error.emit(f"Unknown task: {target}")
            except Exception as e:
                self.signals.error.emit(str(e))
            finally:
                self.signals.finished.emit()

        self.current_thread = threading.Thread(target=worker, daemon=True)
        self.current_thread.start()

    def populate_formats(self, formats: list):
        self.format_combo.clear()
        self.format_combo.addItem("best (default)", "best")
        for fid, display in formats:
            self.format_combo.addItem(display, fid)
        self.signals.log.emit(f"Loaded {len(formats)} formats.\n")

    def build_base_opts(self) -> dict:
        outdir = self.out_dir.text().strip()
        outtmpl = os.path.join(outdir, "%(title)s.%(ext)s")

        opts: dict = {
            "outtmpl": outtmpl,
            "noplaylist": not self.chk_playlist.isChecked(),
            "retries": 3,
            "fragment_retries": 3,
            "continuedl": True,
            "quiet": True,
            "no_warnings": True,
        }

        # Rate limit
        rl = parse_rate_limit(self.rate_limit.text())
        if rl is not None:
            opts["ratelimit"] = rl

        # Cookies
        cookies = self.cookies_path.text().strip()
        if cookies:
            opts["cookiefile"] = cookies

        # Format choice
        chosen = self.format_combo.currentData()
        if chosen and chosen != "best":
            opts["format"] = str(chosen)
        else:
            opts["format"] = "bv*+ba/b"

        # Subtitles
        if self.chk_subs.isChecked():
            langs = [s.strip() for s in self.sub_lang.text().split(",") if s.strip()]
            if langs:
                opts["subtitleslangs"] = langs
            opts["writesubtitles"] = True
            if self.chk_auto_subs.isChecked():
                opts["writeautomaticsub"] = True

        # Metadata / thumbnail / info json
        if self.chk_metadata.isChecked():
            opts["embedmetadata"] = True
            opts["addmetadata"] = True
        if self.chk_thumbnail.isChecked():
            opts["writethumbnail"] = True
            opts["embedthumbnail"] = True
        if self.chk_chapters.isChecked():
            opts["writeinfojson"] = True

        # Audio extraction
        if self.chk_extract_audio.isChecked():
            afmt = self.audio_fmt.currentText()
            abr = self.audio_bitrate.currentText().replace("K", "")
            opts["format"] = "ba/b"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": afmt,
                    "preferredquality": abr,
                }
            ]

        return opts

    def download_queue(self, runner: YtDlpRunner, base_opts: dict):
        total = len(self.queue)
        for i, item in enumerate(self.queue, start=1):
            if self.cancel_flag:
                self.signals.log.emit("Canceled before next item.\n")
                return

            self.signals.item_started.emit(item.url)
            self.signals.log.emit(f"\n--- [{i}/{total}] {item.url} ---\n")

            opts = dict(base_opts)
            try:
                runner.download(item.url, opts)
                self.signals.item_done.emit(item.url)
            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                if "Canceled by user" in msg:
                    self.signals.log.emit("Canceled.\n")
                    return
                self.signals.log.emit(f"Error: {msg}\n")
            except Exception as e:
                self.signals.log.emit(f"Error: {e}\n")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()