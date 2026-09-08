"""科研图像批量格式转换工具 —— GUI（阶段一）。

批量导入 -> 选格式与科研参数 -> 后台 QThread 转换 -> 进度/日志/打开目录。
支持窗口拖拽导入；输出目录为空时自动落到首个源文件所在目录。
"""
from __future__ import annotations

import os
import sys

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

from PySide6.QtCore import QThread, Signal, Qt, QSize
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QComboBox, QLabel, QLineEdit, QSpinBox, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit,
    QProgressBar, QMessageBox, QGroupBox, QAbstractItemView, QCheckBox,
    QDialog, QDialogButtonBox,
)

from PIL import Image

from converter import Options, convert, FORMATS, INPUT_EXT, _MODE_16, _DOWN16_LUT

PRESETS = {
    "自定义": None,
    "SCI 彩图投稿 (TIFF-LZW-300-RGB)": dict(fmt="TIFF", compress="lzw", color="rgb", dpi=300),
    "期刊矢量图 (EPS-300-RGB)": dict(fmt="EPS", color="rgb", dpi=300),
    "演示插图 (PNG-300-RGB)": dict(fmt="PNG", color="rgb", dpi=300),
}
DPI_CHOICES = [150, 300, 600]


class ConvertWorker(QThread):
    """后台批量转换。文件级 ThreadPoolExecutor 并行，仅通过信号与 UI 通信。

    中止：requestInterruption() 后不再派发新任务，已提交未开始的取消，
    正在跑的单个文件跑完即止（Qt/Pillow 单次调用不可中断，按文件粒度）。
    """
    log = Signal(str)
    prog = Signal(int, int)                    # (已完成数, 总数)
    file_done = Signal(int, bool, str)         # (行号, 成功?, 说明)
    all_done = Signal(int, int)                # (成功数, 失败数)

    def __init__(self, srcs: list[str], dst_dir: str, fmt: str, opts: Options,
                 threads: int = 1):
        super().__init__()
        self._srcs, self._dst, self._fmt, self._opts = srcs, dst_dir, fmt, opts
        self._threads = max(1, threads)

    def run(self) -> None:
        n = len(self._srcs)

        def work(i: int, src: str) -> tuple[int, bool, str]:
            try:
                outs = convert(src, self._dst, self._fmt, self._opts)
                total = sum(os.path.getsize(p) for p in outs)
                kb = total / 1024
                vol = f"{kb / 1024:.2f} MB" if kb > 1024 else f"{kb:.0f} KB"
                extra = f" 等{len(outs)}个" if len(outs) > 1 else ""
                return i, True, f"成功 -> {os.path.basename(outs[0])}{extra} ({vol})"
            except Exception as e:  # ConvertError 及其它一律按失败单条继续
                return i, False, f"失败: {e}"

        ok = fail = 0
        done = 0
        stopped = False
        with ThreadPoolExecutor(max_workers=self._threads) as ex:
            futs = [ex.submit(work, i, s) for i, s in enumerate(self._srcs)]
            for f in as_completed(futs):
                if self.isInterruptionRequested():
                    stopped = True
                    for g in futs:
                        g.cancel()            # 排队未开始的取消；已跑完的等 __exit__
                    break
                i, okb, msg = f.result()
                done += 1
                ok += okb
                fail += not okb
                self.file_done.emit(i, okb, msg)
                self.prog.emit(done, n)
        if stopped:
            self.log.emit(f"已中止（{ok} 成功 / {fail} 失败后停止）")
        self.all_done.emit(ok, fail)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("科研图像批量格式转换工具")
        self.resize(1220, 780)
        self._files: list[str] = []
        self._applying_preset = False
        self._worker: ConvertWorker | None = None
        self._pv_cache: dict = {}            # path -> QPixmap（已缩放，供右侧面板）
        self._pv_last: str | None = None
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        c = QWidget()
        self.setCentralWidget(c)
        root = QVBoxLayout(c)

        # 顶部：文件操作 + 列表
        top = QHBoxLayout()
        b_add = QPushButton("添加文件"); b_add.clicked.connect(self._add_files)
        b_dir = QPushButton("添加文件夹"); b_dir.clicked.connect(self._add_folder)
        self.chk_sub = QCheckBox("含子目录")
        self.chk_sub.setChecked(True)
        b_del = QPushButton("移除选中"); b_del.clicked.connect(self._del_selected)
        b_clr = QPushButton("清空列表"); b_clr.clicked.connect(self._clear)
        self._b_del, self._b_clr = b_del, b_clr
        for b in (b_add, b_dir, self.chk_sub, b_del, b_clr):
            top.addWidget(b)
        top.addStretch(1)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["文件", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(self._on_row_open)
        self.table.itemSelectionChanged.connect(self._update_preview_panel)
        self._hint = QLabel("提示：拖入图片批量导入；选中行右侧自动预览，双击看大图")

        # 右侧：参数面板 + 常驻预览面板
        panel = self._build_panel()
        panel.setMinimumWidth(300)
        pv = QGroupBox("预览（选中即显示）")
        pv.setMinimumWidth(300)
        pl = QVBoxLayout(pv)
        self._pv_info = QLabel("未选择文件")
        self._pv_info.setWordWrap(True)
        self._pv_img = QLabel()
        self._pv_img.setAlignment(Qt.AlignCenter)
        self._pv_img.setMinimumHeight(200)
        pl.addWidget(self._pv_info)
        pl.addWidget(self._pv_img, 1)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addLayout(top)
        left.addWidget(self.table, 1)
        left.addWidget(self._hint)
        body.addLayout(left, 1)
        rightcol = QVBoxLayout()
        rightcol.addWidget(panel)
        rightcol.addWidget(pv, 1)
        body.addLayout(rightcol)

        # 输出目录 / 操作
        act = QHBoxLayout()
        act.addWidget(QLabel("输出目录:"))
        self.ed_dir = QLineEdit()
        self.ed_dir.setPlaceholderText("留空则输出到首个源文件所在目录")
        act.addWidget(self.ed_dir, 1)
        b_browse = QPushButton("浏览…"); b_browse.clicked.connect(self._pick_dir)
        b_open = QPushButton("打开目录"); b_open.clicked.connect(self._open_dir)
        act.addWidget(b_browse); act.addWidget(b_open)
        self._b_open = b_open; b_open.setEnabled(False)

        run = QHBoxLayout()
        self.btn_start = QPushButton("开始批量转换")
        self.btn_start.setMinimumHeight(34)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("中止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        b_preview = QPushButton("预估输出")
        b_preview.setToolTip("按当前参数预览每个文件的源尺寸/体积与目标说明（不写文件）")
        b_preview.clicked.connect(self._preview)
        run.addWidget(self.btn_start)
        run.addWidget(self.btn_stop)
        run.addWidget(b_preview)
        run.addWidget(QLabel("并行:"))
        self.sp_threads = QSpinBox(); self.sp_threads.setRange(1, 8); self.sp_threads.setValue(4)
        self.sp_threads.setToolTip("同时转换的文件数；单个大文件的内部操作不可再细分")
        run.addWidget(self.sp_threads)
        self.progress = QProgressBar()
        self.progress.setFormat("待转换 %v/%m")
        self.progress.setValue(0)
        run.addWidget(self.progress, 1)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(5000)

        root.addLayout(body)
        root.addLayout(act)
        root.addLayout(run)
        root.addWidget(QLabel("运行日志:"))
        root.addWidget(self.log_box, 1)

        self.setAcceptDrops(True)

    def _build_panel(self) -> QGroupBox:
        g = QGroupBox("输出参数")
        lay = QGridLayout(g)

        self.cb_preset = QComboBox()
        self.cb_preset.addItems(PRESETS.keys())
        self.cb_preset.currentIndexChanged.connect(self._apply_preset)

        self.cb_fmt = QComboBox()
        self.cb_fmt.addItems(FORMATS.keys())
        self.cb_fmt.currentIndexChanged.connect(self._on_param_changed)

        self.cb_color = QComboBox()
        self.cb_color.addItems(["原样保留", "RGB", "灰度"])
        self.cb_color.setCurrentIndex(0)
        self.cb_color.currentIndexChanged.connect(self._on_param_changed)

        self.sp_quality = QSpinBox(); self.sp_quality.setRange(10, 100); self.sp_quality.setValue(95)
        self.sp_quality.setSuffix(" %")
        self.sp_quality.valueChanged.connect(self._on_param_changed)

        self.cb_compress = QComboBox()
        self.cb_compress.addItems(["LZW 无损", "无压缩"])
        self.cb_compress.currentIndexChanged.connect(self._on_param_changed)

        self.cb_dpi = QComboBox()
        self.cb_dpi.addItems([str(d) for d in DPI_CHOICES])
        self.cb_dpi.setCurrentIndex(1)  # 300
        self.cb_dpi.currentIndexChanged.connect(self._on_param_changed)

        self.cb_mono16 = QComboBox()
        self.cb_mono16.addItems(["高字节原样(定量)", "自动拉伸 min-max"])
        self.cb_mono16.setCurrentIndex(0)
        self.cb_mono16.setToolTip("仅 16 位灰度输入且输出为 8 位格式时生效。\n"
                                 "高字节：数值原样截断（定量分析安全）；窄动态图会偏黑。\n"
                                 "自动拉伸：把亮度范围铺满 0–255（显示友好，改变强度关系）。\n"
                                 "输出 TIFF 原样时始终保留 16 位，不受此影响。")
        self.cb_mono16.currentIndexChanged.connect(self._on_param_changed)

        self.chk_rename = QCheckBox("自动改名 _1/_2 防覆盖")
        self.chk_rename.setChecked(True)
        self.chk_rename.toggled.connect(self._on_param_changed)

        rows = [
            ("投稿预设", self.cb_preset), ("输出格式", self.cb_fmt),
            ("色彩模式", self.cb_color), ("画质(JPEG/WEBP)", self.sp_quality),
            ("TIFF 压缩", self.cb_compress), ("分辨率 DPI", self.cb_dpi),
            ("16位→8位", self.cb_mono16), ("同名冲突", self.chk_rename),
        ]
        for r, (name, w) in enumerate(rows):
            lay.addWidget(QLabel(name), r, 0)
            lay.addWidget(w, r, 1)
        self._sync_params()
        return g

    # ---------- 参数联动 ----------
    def _fmt(self) -> str:
        return self.cb_fmt.currentText()

    def _on_param_changed(self, *_):
        if not self._applying_preset and self.cb_preset.currentIndex() != 0:
            self.cb_preset.setCurrentIndex(0)   # 手动改参 -> 回到自定义
        self._sync_params()

    def _sync_params(self):
        fmt = self._fmt()
        self.sp_quality.setEnabled(fmt in ("JPEG", "WEBP"))
        self.cb_compress.setEnabled(fmt == "TIFF")
        self.cb_dpi.setEnabled(fmt not in ("BMP", "TGA", "WEBP"))

    def _apply_preset(self, idx: int):
        self._applying_preset = True
        p = list(PRESETS.values())[idx]
        if p:
            self.cb_fmt.setCurrentText(p["fmt"])
            self.cb_color.setCurrentIndex({"original": 0, "rgb": 1, "gray": 2}[p["color"]])
            if p.get("compress") == "lzw":
                self.cb_compress.setCurrentIndex(0)
            self.cb_dpi.setCurrentText(str(p["dpi"]))
        self._applying_preset = False
        self._sync_params()

    def _options(self) -> Options:
        return Options(
            color=("original", "rgb", "gray")[self.cb_color.currentIndex()],
            quality=self.sp_quality.value(),
            compress=("lzw", "none")[self.cb_compress.currentIndex()],
            dpi=int(self.cb_dpi.currentText()),
            mono16=("high", "stretch")[self.cb_mono16.currentIndex()],
            if_exists="rename" if self.chk_rename.isChecked() else "overwrite",
        )

    # ---------- 文件列表 ----------
    def _add(self, paths: list[str]):
        for p in paths:
            if p.lower().endswith(tuple(INPUT_EXT)) and p not in self._files:
                self._files.append(p)
                self.table.insertRow(self.table.rowCount())
                self.table.setItem(self.table.rowCount() - 1, 0, QTableWidgetItem(os.path.basename(p)))
                self.table.setItem(self.table.rowCount() - 1, 1, QTableWidgetItem("待转换"))
                self.table.item(self.table.rowCount() - 1, 0).setToolTip(p)
        if self._files and not self.ed_dir.text():
            self.ed_dir.setText(os.path.dirname(self._files[0]))
        if self.table.currentRow() < 0 and self.table.rowCount():
            self.table.setCurrentCell(0, 0)     # 首张自动选中 -> 右侧立即出预览

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片文件", "", "图片文件 (" + " ".join("*" + e for e in sorted(INPUT_EXT)) + ")")
        self._add(paths)

    def _folder_imgs(self, d: str) -> list[str]:
        """收集文件夹下可导入图片；勾选"含子目录"时递归整棵树。"""
        if self.chk_sub.isChecked():
            return sorted(os.path.join(root, f)
                          for root, _dirs, fs in os.walk(d)
                          for f in fs if f.lower().endswith(tuple(INPUT_EXT)))
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if f.lower().endswith(tuple(INPUT_EXT)))

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            found = self._folder_imgs(d)
            self._add(found)
            if found:
                self._log(f"从文件夹导入 {len(found)} 个文件")

    def _del_selected(self):
        if self._worker and self._worker.isRunning():
            return
        for row in sorted({i.row() for i in self.table.selectedItems()}, reverse=True):
            del self._files[row]
            self.table.removeRow(row)
        self._update_preview_panel()

    def _clear(self):
        if self._worker and self._worker.isRunning():
            return
        self._files.clear()
        self.table.setRowCount(0)
        self._pv_cache.clear()
        self._update_preview_panel()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        self._add([p for p in paths if os.path.isdir(p) or p.lower().endswith(tuple(INPUT_EXT))])
        for p in paths:
            if os.path.isdir(p):
                self._add_folder_path(p)
        e.acceptProposedAction()

    # ---------- 转换 ----------
    def _start(self):
        if not self._files:
            QMessageBox.information(self, "提示", "请先添加要转换的图片")
            return
        if self._worker and self._worker.isRunning():
            return
        dst = self.ed_dir.text().strip() or os.path.dirname(self._files[0])
        self.ed_dir.setText(dst)
        opts, fmt = self._options(), self._fmt()
        self.progress.setRange(0, len(self._files))
        self.progress.setValue(0)
        self._set_running(True)
        self._log(f"开始批量转换 {len(self._files)} 个文件 -> {fmt}"
                  f"（并行 {self.sp_threads.value()} 线程）")

        w = ConvertWorker(list(self._files), dst, fmt, opts,
                          threads=self.sp_threads.value())
        w.log.connect(self._log)
        w.prog.connect(self._on_prog)
        w.file_done.connect(self._on_file_done)
        w.all_done.connect(self._on_all_done)
        self._worker = w
        w.start()

    def _on_prog(self, done, total):
        self.progress.setFormat(f"%v/{total}")
        self.progress.setValue(done)

    def _on_file_done(self, row: int, ok: bool, msg: str):
        item = self.table.item(row, 1)
        if item:
            item.setText("成功" if ok else "失败")
            item.setForeground(Qt.darkGreen if ok else Qt.red)
        self._log(msg)

    def _on_all_done(self, ok: int, fail: int):
        self._set_running(False)
        self._log(f"完成：成功 {ok}，失败 {fail}")
        self._b_open.setEnabled(True)
        QMessageBox.information(self, "转换完成",
                                f"成功 {ok} 个，失败 {fail} 个。\n输出目录已就绪，可点「打开目录」查看。")

    # ---------- 预览（不写文件） ----------
    def _preview(self):
        if not self._files:
            QMessageBox.information(self, "提示", "请先添加要转换的图片")
            return
        rows = [self._probe_row(p) for p in self._files]
        same = {}                                  # 批内同名计数（预测改名）
        for p in self._files:
            b = os.path.splitext(os.path.basename(p))[0]
            same[b] = same.get(b, 0) + 1

        dlg = QDialog(self)
        dlg.setWindowTitle("预估输出")
        dlg.resize(980, min(520, 60 + len(rows) * 24))
        lay = QVBoxLayout(dlg)
        tbl = QTableWidget(len(rows), 5)
        tbl.setHorizontalHeaderLabels(["文件", "源尺寸(页数)", "输入体积", "目标(策略/打印尺寸)", "提示"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for r, (src, row) in enumerate(zip(self._files, rows)):
            base = os.path.splitext(os.path.basename(src))[0]
            row[4] = ("批内同名×%d→将自动改名" % same[base] if same[base] > 1 else row[4])
            for c, val in enumerate(row):
                item = QTableWidgetItem(val)
                if c == 0:
                    item.setToolTip(src)
                tbl.setItem(r, c, item)
        lay.addWidget(tbl)
        bt = QDialogButtonBox(QDialogButtonBox.Ok)
        bt.button(QDialogButtonBox.Ok).setText("关闭")
        bt.accepted.connect(dlg.accept)
        lay.addWidget(bt)
        dlg.exec()

    def _probe_row(self, src: str) -> list[str]:
        """只读头信息，估算该文件按当前参数转换的目标说明（不实际转换）。"""
        low = src.lower()
        fmt = self._fmt()
        dpi = int(self.cb_dpi.currentText())
        color = ("原样", "RGB", "灰度")[self.cb_color.currentIndex()]
        mono = ("高字节", "拉伸")[self.cb_mono16.currentIndex()]

        w = h = pages = None
        kind = os.path.splitext(src)[1].upper()
        note = ""
        try:
            if low.endswith((".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")):
                with Image.open(src) as im:
                    w, h = im.size
                    pages = getattr(im, "n_frames", 1)
                    mode = im.mode
                if mode in _MODE_16 and not (fmt == "TIFF" and self.cb_color.currentIndex() == 0):
                    note += f"16→8({mono}) "
                if mode in ("RGBA", "LA") and fmt in ("JPEG", "BMP"):
                    note += "透明压白底 "
            elif low.endswith(".pdf"):
                from PySide6 import QtPdf
                doc = QtPdf.QPdfDocument()
                err = doc.load(src)
                if err == QtPdf.QPdfDocument.Error.None_:
                    pages = doc.pageCount()
                    pt = doc.pagePointSize(0)
                    w, h = round(pt.width() * dpi / 72), round(pt.height() * dpi / 72)
                doc.close()
                kind = "PDF"
                if fmt not in ("PDF", "TIFF"):
                    note += f"按页拆{max(pages, 1)}张 "
            elif low.endswith(".svg"):
                from PySide6.QtCore import QRectF
                from PySide6.QtSvg import QSvgRenderer
                r = QSvgRenderer(src)
                if r.isValid():
                    sz = r.defaultSize()
                    w, h = round(sz.width() * dpi / 96), round(sz.height() * dpi / 96)
                kind = "SVG"
                if fmt not in ("SVG", "PDF"):
                    note += "矢量栅格化 "
            elif low.endswith(".eps"):
                kind = "EPS"
                if fmt not in ("EPS", "PDF"):
                    note += "gs 栅格化 "
        except Exception as e:
            note = f"探测失败: {e}"

        size = os.path.getsize(src)
        kb = size / 1024
        vol = f"{kb / 1024:.1f} MB" if kb > 1024 else f"{kb:.0f} KB"
        dims = f"{w}×{h}" if w else "—"
        if pages and pages > 1:
            dims += f"（{pages}页）"
        target = f"{fmt}·{color}"
        if FORMATS[fmt]["dpi"] and dpi:
            target += f"@{dpi}dpi"
            if w:
                target += f"，宽≈{w * 2.54 / dpi:.2f}cm"
        return [os.path.basename(src), dims, vol, target, note.strip()]

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self.btn_stop.setEnabled(False)
            self._log("已请求中止（正在转换的单个文件会跑完再停）")

    def _set_running(self, on: bool):
        self.btn_start.setEnabled(not on)
        self.btn_start.setText("转换中…" if on else "开始批量转换")
        self.btn_stop.setEnabled(on)
        self.sp_threads.setEnabled(not on)
        self.cb_fmt.setEnabled(not on)
        self.cb_preset.setEnabled(not on)

    def closeEvent(self, e):
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            QMessageBox.information(self, "提示", "正在后台转换，请等待本轮结束或点中止后关闭")
            e.ignore()
            return
        super().closeEvent(e)

    # ---------- 工具 ----------
    # ---------- 图像预览（双击行触发） ----------
    def _on_row_open(self, row: int, _col: int):
        if 0 <= row < len(self._files):
            self._show_preview(self._files[row])

    def _show_preview(self, src: str):
        try:
            img, note = self._render_preview(src)
        except Exception as e:
            QMessageBox.warning(self, "预览失败", f"无法预览该文件:\n{e}")
            return
        if img is None:
            QMessageBox.information(self, "预览", note or "无可用图像帧")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"预览 — {os.path.basename(src)}")
        lay = QVBoxLayout(dlg)
        cap = QLabel(f"{img.size[0]}×{img.size[1]}px" + (f"　{note}" if note else ""))
        lay.addWidget(cap)
        buf = BytesIO()
        img.save(buf, "PNG")
        pm = QPixmap()
        pm.loadFromData(buf.getvalue())
        if pm.width() > 1100 or pm.height() > 720:
            pm = pm.scaled(1100, 720, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        lab = QLabel()
        lab.setPixmap(pm)
        lab.setAlignment(Qt.AlignCenter)
        lay.addWidget(lab)
        bt = QDialogButtonBox(QDialogButtonBox.Ok)
        bt.button(QDialogButtonBox.Ok).setText("关闭")
        bt.accepted.connect(dlg.accept)
        lay.addWidget(bt)
        dlg.resize(min(pm.width() + 40, 1120), min(pm.height() + 110, 800))
        dlg.exec()

    def _update_preview_panel(self):
        """右侧常驻预览：跟随选中行。渲染结果按路径缓存，避免来回切重复计算。"""
        row = self.table.currentRow()
        if not (0 <= row < len(self._files)):
            self._pv_img.clear()
            self._pv_info.setText("未选择文件")
            return
        src = self._files[row]
        if src == self._pv_last and self._pv_img.pixmap() is not None:
            return
        self._pv_last = src
        pm = self._pv_cache.get(src)
        if pm is None:
            img, note = None, ""
            try:
                img, note = self._render_preview(src, box=(640, 520))
            except Exception as e:
                img, note = None, f"预览失败: {e}"
            if img is None:
                self._pv_img.clear()
                self._pv_info.setText(note or "无可用图像帧")
                return
            buf = BytesIO()
            img.save(buf, "PNG")
            pm = QPixmap()
            pm.loadFromData(buf.getvalue())
            pm = pm.scaled(280, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._pv_cache[src] = pm
            if len(self._pv_cache) > 12:      # 简单容量上限，防长时间占用内存
                self._pv_cache.pop(next(iter(self._pv_cache)))
        self._pv_img.setPixmap(pm)
        try:
            dims = self._probe_row(src)[1]
        except Exception:
            dims = ""
        self._pv_info.setText(f"{os.path.basename(src)}　{dims}")

    def _render_preview(self, src: str, box=(1100, 720)) -> tuple[Image.Image | None, str]:
        """渲染可预览的 RGB 图像（首帧/首页），缩进 box 内。"""
        low = src.lower()
        note = ""
        try:
            if low.endswith((".tif", ".tiff", ".png", ".jpg", ".jpeg",
                             ".bmp", ".webp", ".gif")):
                with Image.open(src) as im:
                    pages = getattr(im, "n_frames", 1)
                    if pages > 1:
                        im.seek(0)
                        note = f"共 {pages} 帧，显示第 1 帧"
                    if im.mode in _MODE_16:
                        img = im.convert("I").point(_DOWN16_LUT, mode="L")
                    else:
                        img = im.convert("RGBA")
                    if img.size[0] * img.size[1] > 40_000_000:
                        return None, "图像过大(>40MP)，仅文本预览"
                img = self._flatten_white(img)
                return self._cap(img, box), note
            if low.endswith(".pdf"):
                from PySide6 import QtPdf
                doc = QtPdf.QPdfDocument()
                err = doc.load(src)
                if err != QtPdf.QPdfDocument.Error.None_:
                    return None, f"PDF 无法打开: {err.name}"
                n = doc.pageCount()
                pt = doc.pagePointSize(0)
                dpi = max(24, min(150, round(box[0] * 72 / max(pt.width(), 1))))
                w, h = max(1, round(pt.width() * dpi / 72)), max(1, round(pt.height() * dpi / 72))
                qim = doc.render(0, QSize(w, h)).convertToFormat(QImage.Format.Format_RGB888)
                doc.close()
                img = Image.frombuffer("RGB", (w, h), qim.constBits().tobytes(),
                                       "raw", "RGB", qim.bytesPerLine(), 1)
                if n > 1:
                    note = f"共 {n} 页，显示第 1 页"
                return self._cap(img, box), note
            if low.endswith(".svg"):
                from PySide6.QtCore import QRectF
                from PySide6.QtGui import QGuiApplication, QPainter
                from PySide6.QtSvg import QSvgRenderer
                if QGuiApplication.instance() is None:
                    return None, "需要 Qt 环境预览 SVG"
                r = QSvgRenderer(src)
                if not r.isValid():
                    return None, "SVG 文件无效"
                base = r.defaultSize()
                k = min(1.0, box[0] / max(base.width(), 1), box[1] / max(base.height(), 1))
                w, h = max(1, round(base.width() * k)), max(1, round(base.height() * k))
                qim = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
                qim.fill(Qt.GlobalColor.white)
                p = QPainter(qim)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                r.render(p, QRectF(0, 0, w, h))
                p.end()
                qim = qim.convertToFormat(QImage.Format.Format_RGBA8888)
                img = Image.frombuffer("RGBA", (w, h), qim.constBits().tobytes(),
                                       "raw", "RGBA", qim.bytesPerLine(), 1)
                return self._flatten_white(img), note
            if low.endswith(".eps"):
                from converter import _render_eps_frames
                frames = _render_eps_frames(src, 96)
                if not frames:
                    return None, "EPS 渲染为空"
                return self._cap(frames[0], box), "EPS（已用 Ghostscript 栅格化预览）"
        except Exception as e:
            return None, f"预览失败: {e}"
        return None, "不支持的文件类型"

    @staticmethod
    def _flatten_white(img: Image.Image) -> Image.Image:
        if img.mode in ("RGBA", "LA"):
            base = Image.new("RGBA", img.size, (255, 255, 255, 255))
            return Image.alpha_composite(base, img.convert("RGBA")).convert("RGB")
        return img.convert("RGB")

    @staticmethod
    def _cap(img: Image.Image, box=(1100, 720)) -> Image.Image:
        img = img.convert("RGB")
        img.thumbnail(box, Image.LANCZOS)
        return img

    def _log(self, s: str):
        self.log_box.appendPlainText(s)

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    def _open_dir(self):
        d = self.ed_dir.text().strip()
        if d and os.path.isdir(d):
            os.startfile(d)  # noqa: 仅 Windows 目标平台

    def _add_folder_path(self, d: str):
        self._add(self._folder_imgs(d))


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
