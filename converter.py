"""核心转换引擎：光栅/TIFF/PDF/SVG/EPS 互转。

科研图像批量格式转换工具。矢量直通(EPS/PDF/SVG)与位图封装(SVG/PDF/EPS 输出)
见 convert() 派发。EPS 输入依赖 Ghostscript（vendor/gs 随包或 PATH 中的 gswin64c）。
引擎每条转换要么抛 ConvertError，要么产出文件。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass

from PIL import Image

# 输出格式能力表。dpi=True 表示该格式可写入分辨率（PDF/SVG 用于物理尺寸换算）。
# PDF/SVG 有独立写出路径，此表主要供 _prep 判断 alpha 保留。
FORMATS: dict[str, dict] = {
    "PNG":  dict(ext=".png",  alpha=True,  dpi=True),
    "JPEG": dict(ext=".jpg",  alpha=False, dpi=True),
    "WEBP": dict(ext=".webp", alpha=True,  dpi=False),
    "BMP":  dict(ext=".bmp",  alpha=False, dpi=False),
    "TGA":  dict(ext=".tga",  alpha=True,  dpi=False),
    "TIFF": dict(ext=".tiff", alpha=True,  dpi=True),
    "PDF":  dict(ext=".pdf",  alpha=False, dpi=True),
    "SVG":  dict(ext=".svg",  alpha=True,  dpi=True),
    "EPS":  dict(ext=".eps",  alpha=False, dpi=True),
}

# 文档规定支持的输入扩展名（GUI 导入过滤用）。
INPUT_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
             ".tif", ".tiff", ".pdf", ".svg", ".eps"}

TIFF_COMPRESS = {"lzw": "tiff_lzw", "none": None}  # SCI 默认 LZW 无损

# 16 位灰度模式族（Pillow 按字节序分 I;16 / I;16B / I;16L / I;16N）。
_MODE_16 = ("I;16", "I;16B", "I;16L", "I;16N", "I;16S")

# 16->8 位按高字节缩放的查找表（point 对 I;16 只支持仿射，故先转 I 再走 LUT）。
_DOWN16_LUT = [v >> 8 for v in range(65536)]

# min-max 拉伸 LUT 缓存（key=(lo,hi)）。拉伸把 12/14bit 装 16bit 容器的窄动态
# 范围铺满 0–255，否则按高字节会塌成 0–15 近乎全黑。并行批处理时多线程写缓存，
# 用锁保护。
_stretch_lock = threading.Lock()
_stretch_cache: dict[tuple[int, int], list[int]] = {}


def _stretch_lut(lo: int, hi: int) -> list[int]:
    """把 [lo,hi] 线性映射到 [0,255] 的 65536 项 LUT（命中缓存，线程安全）。"""
    k = (lo, hi)
    with _stretch_lock:
        if k in _stretch_cache:
            return _stretch_cache[k]
        span = hi - lo
        mid = [0] * lo + [(v - lo) * 255 // span for v in range(lo, hi + 1)]
        mid += [255] * (65536 - len(mid))
        _stretch_cache[k] = mid
        return mid


class ConvertError(Exception):
    """单文件转换失败，message 面向用户展示。"""


@dataclass
class Options:
    color: str = "original"   # original | rgb | gray
    quality: int = 95          # JPEG/WEBP 画质 10–100
    compress: str = "lzw"      # TIFF 压缩: none | lzw
    dpi: int = 300             # 输出分辨率元数据（仅对支持格式生效）
    mono16: str = "high"       # 16位灰度降8位: high(高字节原样) | stretch(min-max拉伸)
    if_exists: str = "overwrite"  # 目标已存在: overwrite | rename(自动加 _1/_2)


def _gs_exe() -> str:
    """定位 Ghostscript 可执行文件。

    查找顺序：随包 vendor/gs（开发目录或 PyInstaller EXE 旁）-> PATH。
    EPS 输入需要它；找不到返回空串，由调用方抛 ConvertError。
    """
    frozen = getattr(sys, "frozen", False)
    if frozen:  # PyInstaller 6 onedir: 随包数据在 EXE 旁的 _internal/ 下
        bases = [os.path.join(os.path.dirname(sys.executable), "_internal"),
                 os.path.dirname(sys.executable)]
    else:
        bases = [os.path.dirname(os.path.abspath(__file__))]
    for base in bases:
        rel = os.path.join(base, "vendor", "gs", "bin", "gswin64c.exe")
        if os.path.exists(rel):
            return rel
    for name in ("gswin64c", "gswin32c", "gs"):
        w = shutil.which(name)
        if w:
            return w
    return ""


def _gs_run(args: list[str]) -> tuple[int, str]:
    """运行 Ghostscript，返回 (退出码, stderr 末尾)。路径均用 Windows 原生形式。"""
    exe = _gs_exe()
    if not exe:
        raise ConvertError("EPS 需要 Ghostscript：请把 gswin64c 加入 PATH，"
                           "或在本程序目录放置 vendor/gs（随包自带）")
    import subprocess
    proc = subprocess.run([exe, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stderr or proc.stdout).strip()[-400:]


def _render_eps_frames(src: str, dpi: int) -> list[Image.Image]:
    """用 Ghostscript 把 EPS 渲染成 RGB 帧（按 bbox 裁剪，物理 pt×dpi/72）。

    EPS 为单页。失败（如损坏/非 PostScript）归一到 ConvertError。
    """
    out = os.path.join(tempfile.mkdtemp(prefix="cv_epsin_"), "page.png")
    rc, err = _gs_run(["-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dEPSCrop",
                       "-sDEVICE=png16m", f"-r{dpi or 72}", f"-sOutputFile={out}", src])
    if rc != 0 or not os.path.exists(out):
        raise ConvertError(f"EPS 解析失败(Ghostscript): {err or f'退出码 {rc}'}")
    try:
        with Image.open(out) as im:
            return [im.convert("RGB")]
    except Exception as e:
        raise ConvertError(f"EPS 渲染结果无法读取: {e}") from e


def _flatten(im: Image.Image) -> Image.Image:
    """透明图合成到白底 -> RGB（JPEG/BMP 等不支持 alpha 的格式用）。"""
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    base = Image.new("RGBA", im.size, (255, 255, 255))
    return Image.alpha_composite(base, im).convert("RGB")


def _prep(im: Image.Image, fmt: str, o: Options) -> Image.Image:
    """把一帧原始图归一为可安全保存成 fmt 的图像。入口必为独立副本。"""
    im = im.copy()
    mode = im.mode
    want_gray = o.color == "gray"
    want_rgb = o.color == "rgb"

    # 16 位灰度：仅输出 TIFF 且要求原样时才保留 16 位，否则降为 8 位。
    if mode in _MODE_16:
        if fmt == "TIFF" and not want_gray and not want_rgb:
            return im
        im = im.convert("I")
        if o.mono16 == "stretch":
            lo, hi = im.getextrema()
            # 无动态范围（纯色/全黑）退化：按高字节，避免除零后乱拉。
            lut = _stretch_lut(lo, hi) if hi > lo else _DOWN16_LUT
        else:
            lut = _DOWN16_LUT
        im = im.point(lut, mode="L")
        mode = "L"

    if want_gray:
        return im if mode == "L" else im.convert("L")

    # CMYK 多数目标格式不支持，TIFF 原样时保留。
    if mode == "CMYK":
        if fmt != "TIFF":
            im = im.convert("RGB")
            mode = "RGB"
        else:
            return im

    # 调色板展开，还原透明。
    if mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        mode = im.mode

    if want_rgb:
        return _flatten(im) if mode in ("RGBA", "LA", "PA") else im.convert("RGB")

    # original：由目标格式决定 alpha 去留。
    if not FORMATS[fmt]["alpha"] and mode in ("RGBA", "LA"):
        return _flatten(im)
    if mode not in ("RGB", "RGBA", "L", "LA"):
        return im.convert("RGB")
    return im


def _save_kwargs(fmt: str, o: Options) -> dict:
    kw: dict = {}
    if FORMATS[fmt]["dpi"] and o.dpi:
        kw["dpi"] = (o.dpi, o.dpi)
    if fmt == "TIFF":
        kw["compression"] = TIFF_COMPRESS[o.compress]
    if fmt in ("JPEG", "WEBP"):
        kw["quality"] = o.quality
    return kw


def _render_pdf_pages(src: str, dpi: int) -> list[Image.Image]:
    """用 QtPdf 按目标 DPI 把 PDF 各页渲染成 RGB 图像（PySide6 随包，免 poppler）。

    QtPdf 仅在 GUI/带 Qt 的环境可用；懒加载保持引擎其余路径无 Qt 依赖。
    """
    try:
        from PySide6 import QtPdf
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtGui import QImage, QPainter
    except ImportError as e:
        raise ConvertError("PDF 解析依赖 PySide6.QtPdf，当前环境未安装") from e

    doc = QtPdf.QPdfDocument()
    err = doc.load(src)
    if err != QtPdf.QPdfDocument.Error.None_:
        raise ConvertError(f"PDF 无法打开: {err.name}")
    scale = (dpi or 72) / 72.0
    frames = []
    try:
        for i in range(doc.pageCount()):
            pts = doc.pagePointSize(i)
            w, h = max(1, round(pts.width() * scale)), max(1, round(pts.height() * scale))
            raw = doc.render(i, QSize(w, h))   # ARGB32，页面未绘制区为透明
            # PDF 纸面本身是透明，查看器一律垫白底；直接转 RGB888 会把透明区变黑，
            # 故先用 QPainter 在白色画布上合成，再取不透明 RGB。
            qim = QImage(w, h, QImage.Format.Format_RGB888)
            qim.fill(Qt.GlobalColor.white)
            p = QPainter(qim)
            p.drawImage(0, 0, raw)
            p.end()
            data = qim.constBits().tobytes()
            frames.append(Image.frombuffer("RGB", (w, h), data, "raw", "RGB",
                                           qim.bytesPerLine(), 1))
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(f"PDF 渲染失败: {e}") from e
    finally:
        doc.close()
    return frames


def _prepared_frames(src: str, fmt: str, o: Options) -> tuple[list[Image.Image], bool]:
    """读源并归一化为可保存图像列表，返回 (帧, 是否多帧需拆页)。"""
    low = src.lower()
    if low.endswith(".pdf"):
        raw = _render_pdf_pages(src, o.dpi)
        return [_prep(f, fmt, o) for f in raw], len(raw) > 1
    if low.endswith(".svg"):
        raw = _render_svg_pages(src, o.dpi)
        return [_prep(f, fmt, o) for f in raw], False
    if low.endswith(".eps"):
        raw = _render_eps_frames(src, o.dpi)
        return [_prep(f, fmt, o) for f in raw], False
    try:
        with Image.open(src) as im:
            n_frames = getattr(im, "n_frames", 1)
            # 仅多页 TIFF 按多帧处理；动图 GIF 折叠为首帧（科研配图无动图需求）。
            multi = low.endswith((".tif", ".tiff")) and n_frames > 1
            n_take = n_frames if multi else 1
            frames = []
            for i in range(n_take):
                im.seek(i)
                frames.append(_prep(im, fmt, o))
    except ConvertError:
        raise
    except Exception as e:  # 损坏/无法解析的源文件，归一到带语义的错误
        raise ConvertError(f"无法读取源文件: {e}") from e
    return frames, multi


def _render_svg_pages(src: str, dpi: int) -> list[Image.Image]:
    """用 QtSvg 把 SVG 渲染成 RGBA 图像（保留透明背景）。

    输出像素 = 矢量原生尺寸 × dpi/96（CSS px 基准）。QtSvg 内部把 in/cm 等
    单位按其 90dpi 换算，故带物理单位的 SVG 绝对尺寸约有 ±6% 误差；
    ponytail: 对物理尺寸极敏感的场景再按 viewBox/单位自定义解析。
    """
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError as e:
        raise ConvertError("SVG 解析依赖 PySide6.QtSvg，当前环境未安装") from e
    if QGuiApplication.instance() is None:  # 引擎独立调用时兜底
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QGuiApplication([])

    try:
        r = QSvgRenderer(src)
        if not r.isValid():
            raise ConvertError("SVG 文件无效")
        scale = (dpi or 96) / 96.0
        w = max(1, round(r.defaultSize().width() * scale))
        h = max(1, round(r.defaultSize().height() * scale))
        qim = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        qim.fill(Qt.GlobalColor.transparent)
        p = QPainter(qim)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r.render(p, QRectF(0, 0, w, h))
        p.end()
        qim = qim.convertToFormat(QImage.Format.Format_RGBA8888)
        data = qim.constBits().tobytes()
        return [Image.frombuffer("RGBA", (w, h), data, "raw", "RGBA",
                                 qim.bytesPerLine(), 1)]
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(f"SVG 渲染失败: {e}") from e


def _svg_to_pdf_vector(src: str, path: str) -> None:
    """SVG -> PDF 矢量保真导出（Qt 把矢量指令直接写入 PDF，非点阵封装）。"""
    from PySide6.QtCore import QRectF, QSizeF
    from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPageSize
    from PySide6.QtPrintSupport import QPrinter
    from PySide6.QtSvg import QSvgRenderer
    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QGuiApplication([])

    r = QSvgRenderer(src)
    if not r.isValid():
        raise ConvertError("SVG 文件无效")
    css = r.defaultSize()
    pr = QPrinter(QPrinter.PrinterMode.HighResolution)
    pr.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    pr.setOutputFileName(path)
    # Qt 尺寸 -> 物理点数(72/in)，按 CSS px 96dpi 基准换算
    pr.setPageSize(QPageSize(QSizeF(css.width() * 72.0 / 96.0,
                                    css.height() * 72.0 / 96.0),
                             QPageSize.Unit.Point))
    p = QPainter(pr)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(QRectF(0, 0, pr.width(), pr.height()), QColor("white"))
    r.render(p, QRectF(0, 0, pr.width(), pr.height()))
    p.end()


def _write_pdf(frames: list[Image.Image], path: str, o: Options) -> None:
    """位图帧封装成多页 PDF（自写：FlateDecode 无损内嵌，物理尺寸 = 像素/dpi×72pt）。

    不用 Pillow 的 PDF 写器：Pillow 12 对 L/RGB/CMYK 一律 DCTDecode(JPEG)、
    RGBA/LA 用 JPXDecode —— 都有损，期刊投稿 PDF 不接受。帧在此路径已归一为
    L/RGB（_prep 负责 16 位/alpha/CMYK 归约），逐帧 zlib 压缩内嵌。
    """
    import zlib

    dpi = o.dpi or 72
    n = len(frames)

    def pt(v: int) -> str:                     # px -> 物理 pt（MediaBox/绘制用）
        return f"{v * 72.0 / dpi:.4f}"

    # 先备好每页内容流与图像对象体（对象号：1 Catalog, 2 Pages,
    # 页 3..3+n-1, 内容 3+n.., 图像 3+2n..）
    bodies: list[tuple[int, bytes]] = []        # (obj_no, body bytes)
    bodies.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    bodies.append((2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()))
    page_no, content_no, image_no = 3, 3 + n, 3 + 2 * n
    for i, frame in enumerate(frames):
        gray = frame.mode == "L"
        px = frame if gray else frame.convert("RGB")
        w, h = px.size
        wp, hp = pt(w), pt(h)
        stream = f"q {wp} 0 0 {hp} 0 0 cm /Im{i} Do Q".encode()
        bodies.append((content_no + i, b"<< /Length %d >>\nstream\n" % len(stream)
                       + stream + b"\nendstream"))
        raw = zlib.compress(px.tobytes())
        cs = b"/DeviceGray" if gray else b"/DeviceRGB"
        bodies.append((image_no + i, b"<< /Type /XObject /Subtype /Image /Width %d /Height %d"
                       b" /ColorSpace %s /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
                       % (w, h, cs, len(raw)) + raw + b"\nendstream"))
        res = b" ".join(b"/Im%d %d 0 R" % (j, image_no + j) for j in range(n))
        bodies.append((page_no + i, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {wp} {hp}]"
                       f" /Resources << /XObject << {res.decode()} >> >>"
                       f" /Contents {content_no + i} 0 R >>".encode()))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num, body in sorted(bodies):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (n * 3 + 3)
    out += b"0000000000 65535 f \n"
    for num in sorted(offsets):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n * 3 + 3, xref)
    with open(path, "wb") as f:
        f.write(bytes(out))


def _out(dst_dir: str, base: str, ext: str, o: Options) -> str:
    """目标写出路径。o.if_exists='rename' 时已存在则自动加 _1/_2/_…，防静默覆盖。"""
    path = os.path.join(dst_dir, base + ext)
    if o.if_exists != "rename" or not os.path.exists(path):
        return path
    i = 1
    while os.path.exists(os.path.join(dst_dir, f"{base}_{i}{ext}")):
        i += 1
    return os.path.join(dst_dir, f"{base}_{i}{ext}")


def _write_svg(frames: list[Image.Image], name: str, dst_dir: str, o: Options) -> list[str]:
    """位图帧封装成 SVG（无损 PNG 内嵌 base64）。SVG 无多页，多帧按 _pN 拆分。"""
    import base64
    from io import BytesIO

    dpi = o.dpi or 96
    paths = []
    targets = [name] if len(frames) == 1 else [f"{name}_p{i}" for i in range(1, len(frames) + 1)]
    for frame, base in zip(frames, targets):
        buf = BytesIO()
        frame.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        w, h = frame.size
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
               f'width="{w / dpi:.4f}in" height="{h / dpi:.4f}in" '
               f'viewBox="0 0 {w} {h}">\n'
               f'  <image width="{w}" height="{h}" href="data:image/png;base64,{b64}"/>\n'
               f"</svg>\n")
        path = _out(dst_dir, base, ".svg", o)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        paths.append(path)
    return paths


def _write_eps(frames: list[Image.Image], name: str, dst_dir: str, o: Options) -> list[str]:
    """位图帧封装成 EPS（RGB/灰度，逐行 hex + readhexstring 数据源）。

    与 Pillow 的 EPS 编码同构 —— 实测 Ghostscript 可渲染（曾用 ASCII85 +
    colorimage 被 gs 10.07 拒绝/渲染空白，见 tech-stack 记忆）。光栅 EPS 行业
    惯例 1px = 1pt（72dpi），打印尺寸由排版端决定，故 o.dpi 对 EPS 几何不生效。
    矢量源走此路径先栅格化，即"容器封装"非真矢量。EPS 无多页，多帧拆 _pN。
    """
    paths = []
    targets = [name] if len(frames) == 1 else [f"{name}_p{i}" for i in range(1, len(frames) + 1)]
    for frame, base in zip(frames, targets):
        gray = frame.mode == "L"
        px = frame if gray else frame.convert("RGB")
        w, h = px.size
        nc = 1 if gray else 3
        op = "image" if gray else "false 3 colorimage"
        hex_rows = px.tobytes().hex()          # 2× 源大小；整块也行，readhexstring 忽略换行
        body = "\n".join(hex_rows[i:i + 4096] for i in range(0, len(hex_rows), 4096))
        eps = (f"%!PS-Adobe-3.0 EPSF-3.0\n"
               f"%%BoundingBox: 0 0 {w} {h}\n"
               f"%%Creator: SciImgBatchConvert\n"
               f"%%EndComments\n"
               f"gsave\n10 dict begin\n"
               f"/buf {w * nc} string def\n"
               f"{w} {h} scale\n{w} {h} 8\n[{w} 0 0 -{h} 0 {h}]\n"
               f"{{ currentfile buf readhexstring pop }} bind\n{op}\n"
               f"{body}\n"
               f"grestore end\n%%EOF\n")
        path = _out(dst_dir, base, ".eps", o)
        with open(path, "w", encoding="ascii") as f:
            f.write(eps)
        paths.append(path)
    return paths


def convert(src: str, dst_dir: str, fmt: str, o: Options) -> list[str]:
    """转换单个源文件，返回实际写出的文件路径列表。

    - PDF/多页 TIFF -> TIFF/PDF：合并多页；-> 单页格式(SVG/PNG/…) 按帧拆分。
    - SVG->PDF/SVG 与 PDF->PDF 矢量/无损直通。
    - 位图 -> PDF/SVG 为"高清嵌入封装"（图像本身非真矢量）。
    16 位降 8 位策略与同名冲突策略见 Options.mono16 / Options.if_exists。
    """
    fmt = fmt.upper()
    if fmt not in FORMATS:
        raise ConvertError(f"尚未支持的输出格式: {fmt}")
    cfg = FORMATS[fmt]
    low = src.lower()
    name = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(dst_dir, exist_ok=True)

    # --- 无损/矢量直通路径 ---
    if fmt == "PDF" and low.endswith(".pdf"):   # PDF->PDF 复制
        return [_copy(src, os.path.join(dst_dir, name + cfg["ext"]))]
    if fmt == "SVG" and low.endswith(".svg"):   # SVG->SVG 复制
        return [_copy(src, os.path.join(dst_dir, name + cfg["ext"]))]
    if fmt == "PDF" and low.endswith(".svg"):   # SVG->PDF 真矢量
        path = _out(dst_dir, name, cfg["ext"], o)
        _svg_to_pdf_vector(src, path)
        return [path]
    if fmt == "EPS" and low.endswith(".eps"):   # EPS->EPS 直通复制
        return [_copy(src, os.path.join(dst_dir, name + cfg["ext"]))]
    if fmt == "PDF" and low.endswith(".eps"):   # EPS->PDF 矢量（gs pdfwrite）
        path = _out(dst_dir, name, cfg["ext"], o)
        rc, err = _gs_run(["-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dEPSCrop",
                           "-sDEVICE=pdfwrite", f"-sOutputFile={path}", src])
        if rc != 0 or not os.path.exists(path):
            raise ConvertError(f"EPS->PDF 失败(Ghostscript): {err or f'退出码 {rc}'}")
        return [path]

    # --- 通用帧管线 ---
    frames, multi = _prepared_frames(src, fmt, o)
    if not frames:
        raise ConvertError("源文件没有可用帧")

    if fmt == "SVG":
        return _write_svg(frames, name, dst_dir, o)
    if fmt == "PDF":
        path = _out(dst_dir, name, cfg["ext"], o)
        _write_pdf(frames, path, o)
        return [path]
    if fmt == "EPS":
        return _write_eps(frames, name, dst_dir, o)

    kw = _save_kwargs(fmt, o)
    if fmt == "TIFF":
        path = _out(dst_dir, name, cfg["ext"], o)
        frames[0].save(path, save_all=True, append_images=frames[1:], **kw)
        return [path]

    if not multi or len(frames) == 1:
        path = _out(dst_dir, name, cfg["ext"], o)
        frames[0].save(path, **kw)
        return [path]

    paths = []
    for i, frame in enumerate(frames, 1):
        path = _out(dst_dir, f"{name}_p{i}", cfg["ext"], o)
        frame.save(path, **kw)
        paths.append(path)
    return paths


def _copy(src: str, dst: str) -> str:
    import shutil
    if os.path.abspath(src) == os.path.abspath(dst):
        return dst                      # 源目标相同（同目录同名），无需复制
    shutil.copyfile(src, dst)
    return dst


def demo() -> None:
    """最小自检：跑通各关键路径，assert 失败即逻辑有误。"""
    import tempfile

    d = tempfile.mkdtemp(prefix="cv_demo_")
    o = Options()

    def rd(p):
        with Image.open(p) as im:
            return im.copy()

    # RGB -> JPEG（画质）与 PNG
    src = os.path.join(d, "rgb.png")
    Image.new("RGB", (8, 8), (200, 30, 30)).save(src)
    out = convert(src, d, "JPEG", o)
    assert rd(out[0]).mode == "RGB" and out[0].endswith(".jpg")

    # 16 位灰度 -> TIFF 保留 16 位(LZW)，读回仍为 I;16
    g16 = os.path.join(d, "gray16.tif")
    a = Image.new("I;16", (8, 8))
    a.putdata([i * 4000 for i in range(64)])
    b = Image.new("I;16", (8, 8))
    b.putdata([65000 - i * 900 for i in range(64)])
    a.save(g16, save_all=True, append_images=[b], compression="tiff_lzw")
    with Image.open(g16) as im:
        assert getattr(im, "n_frames", 1) == 2 and im.mode in ("I;16", "I;16N")
    # -> TIFF: 保留两页、16 位
    t16 = convert(g16, d, "TIFF", o)[0]
    with Image.open(t16) as im:
        assert getattr(im, "n_frames", 1) == 2 and im.mode in ("I;16", "I;16N")
    # -> JPEG: 降为 8 位单张 L
    j = convert(g16, d, "JPEG", o)[0]
    assert rd(j).mode == "L"
    # -> PNG(单页格式): 多页 TIFF 拆页
    ps = convert(g16, d, "PNG", o)
    assert len(ps) == 2 and rd(ps[0]).mode == "L"

    # RGBA -> JPEG 需压透明为白底
    rgba = os.path.join(d, "a.png")
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(rgba)
    jr = convert(rgba, d, "JPEG", o)[0]
    assert rd(jr).mode == "RGB"

    # 灰度转换开关
    gr = convert(src, d, "TIFF", Options(color="gray"))[0]
    assert rd(gr).mode == "L"

    # 16->8 位策略：窄动态(12/14bit 式)图像 高字节≈全黑，stretch 铺满 0–255
    band = os.path.join(d, "band.tif")
    _b = Image.new("I;16", (8, 8))
    _b.putdata([512 + i * 6 for i in range(64)])
    _b.save(band, compression="tiff_lzw")
    hi = rd(convert(band, d, "PNG", Options(mono16="high"))[0])
    st = rd(convert(band, d, "PNG", Options(mono16="stretch"))[0])
    assert hi.mode == st.mode == "L"
    assert hi.getextrema() == (2, 3), f"高字节应近黑 {hi.getextrema()}"
    assert st.getextrema() == (0, 255), f"拉伸应铺满 {st.getextrema()}"

    # 同名冲突：rename 策略自动加 _1，不覆盖已有输出
    r = Options(if_exists="rename")
    p1 = convert(src, d, "PNG", r)[0]          # src=rgb.png -> rgb.png 已存在(源) => rgb_1.png
    p2 = convert(src, d, "PNG", r)[0]          # 再转 => rgb_2.png
    assert os.path.basename(p2) == "rgb_2.png", os.path.basename(p2)
    assert rd(p1) is not None and rd(p2) is not None
    # overwrite（默认）仍覆盖同路径
    po = convert(src, d, "PNG", o)[0]
    assert os.path.basename(po) == "rgb.png"

    # 16 位转 PNG 报正常错误路径不测，损坏文件应有语义错误
    try:
        convert(os.path.join(d, "missing.png"), d, "PNG", o)
        raise AssertionError("损坏文件未抛 ConvertError")
    except ConvertError:
        pass

    print(f"converter demo PASS  ({d})")


def demo_pdf() -> None:
    """PDF 分页导入自检（需 PySide6.QtPdf）。"""
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.instance() is None:
            QGuiApplication([])
    except Exception as e:
        print(f"pdf demo SKIP (无 Qt): {e}")
        return

    import tempfile

    d = tempfile.mkdtemp(prefix="cv_pdf_")
    src = os.path.join(d, "two.pdf")
    Image.new("RGB", (200, 120), (20, 120, 220)).save(
        src, save_all=True, append_images=[Image.new("RGB", (100, 200), (220, 120, 20))],
        resolution=150)

    o = Options()                       # dpi=300
    tif = convert(src, d, "TIFF", o)[0]
    with Image.open(tif) as im:
        assert getattr(im, "n_frames", 1) == 2, "PDF->TIFF 应保留两页"
    ps = convert(src, d, "PNG", o)
    assert len(ps) == 2 and ps[0].endswith("_p1.png") and ps[1].endswith("_p2.png")
    # 300dpi 渲染尺寸校验：96pt 宽页 -> 400px
    with Image.open(ps[0]) as im:
        assert im.size[0] == 400, f"300dpi 页宽应为 400，实际 {im.size[0]}"
    js = convert(src, d, "JPEG", Options(color="gray"))[0]
    with Image.open(js) as im:
        assert im.mode == "L"
    # 白底回归：QtPdf 渲染的页面未绘制区是透明，必须垫白底而非当黑
    # （整页铺满的图不会触发；白纸+局部内容才暴露）。
    patch = os.path.join(d, "patch.pdf")
    gs = _gs_exe()
    if gs:
        import subprocess
        subprocess.run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
                        "-o", patch, "-c",
                        "<< /PageSize [300 200] >> setpagedevice "
                        "0.9 0.1 0.1 setrgbcolor 50 50 100 80 rectfill"],
                       capture_output=True)
        with Image.open(convert(patch, d, "PNG", Options(dpi=72))[0]) as im:
            g = im.convert("L")
            white = g.histogram()[255] / (g.size[0] * g.size[1])
            assert white > 0.8, f"白底 PDF 背景应≈白，纯白占比 {white:.0%}"
    print(f"converter pdf demo PASS  ({d})")


def demo_svg() -> None:
    """SVG 输入渲染 + SVG/PDF 输出自检（需 PySide6）。"""
    try:
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.instance() is None:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            QGuiApplication([])
    except Exception as e:
        print(f"svg demo SKIP (无 Qt): {e}")
        return

    import tempfile

    d = tempfile.mkdtemp(prefix="cv_svg_")
    src = os.path.join(d, "fig.svg")
    with open(src, "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
                '<rect x="10" y="10" width="200" height="80" fill="#1a6bc4"/>'
                '<circle cx="300" cy="100" r="50" fill="#e0b020"/>'
                '<text x="20" y="160" font-size="24" fill="#111">矢量标签</text></svg>')

    o = Options()                      # dpi=300
    # SVG -> PNG 渲染：原生400宽 → 400*300/96≈1250
    png = convert(src, d, "PNG", o)[0]
    with Image.open(png) as im:
        assert im.size[0] == 1250, f"SVG@300dpi 宽应1250，实际 {im.size[0]}"
    # SVG -> PDF 真矢量
    pdf = convert(src, d, "PDF", o)[0]
    _assert_pdf_pages(pdf, 1)
    # SVG -> SVG 直通复制
    svg2 = convert(src, d, "SVG", o)[0]
    assert svg2.endswith(".svg") and os.path.getsize(svg2) == os.path.getsize(src)

    # 位图 -> PDF / -> SVG 嵌入封装
    bmp = os.path.join(d, "photo.png")
    Image.new("RGBA", (60, 40), (0, 0, 0, 0)).save(bmp)
    bp = convert(bmp, d, "PDF", o)[0]
    _assert_pdf_pages(bp, 1)
    bs = convert(bmp, d, "SVG", o)[0]
    assert "data:image/png;base64," in open(bs, encoding="utf-8").read()

    # 多页 PDF -> PDF 复制直通
    pdf2 = convert(pdf, d, "PDF", o)[0]
    _assert_pdf_pages(pdf2, 1)
    print(f"converter svg demo PASS  ({d})")


def demo_eps() -> None:
    """EPS 封装自检：校验头/包围盒/hex 数据与源像素一致（光栅 EPS 按 1px=1pt）。"""
    import tempfile

    d = tempfile.mkdtemp(prefix="cv_eps_")

    src = os.path.join(d, "rgb.png")
    im = Image.new("RGB", (200, 100))
    im.putdata([(x * 3 % 256, y * 5 % 256, (x + y) % 256) for y in range(100) for x in range(200)])
    im.save(src)
    eps = convert(src, d, "EPS", Options())[0]
    text = open(eps, encoding="ascii").read()
    assert text.startswith("%!PS-Adobe-3.0 EPSF-3.0")
    assert "%%BoundingBox: 0 0 200 100" in text, "光栅 EPS 按像素=点数(72dpi)"
    assert "readhexstring" in text and "false 3 colorimage" in text
    hx = text.split("colorimage\n", 1)[1].split("grestore end", 1)[0]
    assert bytes.fromhex("".join(hx.split())) == im.convert("RGB").tobytes(), "EPS 内嵌像素与源不一致"

    # 灰度：image 单分量
    g = convert(src, d, "EPS", Options(color="gray"))[0]
    gt = open(g, encoding="ascii").read()
    assert "false 3 colorimage" not in gt and "\nimage\n" in gt

    # 多帧 TIFF -> EPS 拆分 _pN
    tif = os.path.join(d, "two.tif")
    Image.new("RGB", (100, 80), (1, 2, 3)).save(
        tif, save_all=True, append_images=[Image.new("RGB", (100, 80), (4, 5, 6))])
    ps = convert(tif, d, "EPS", Options())
    assert len(ps) == 2 and all(p.endswith(".eps") for p in ps)
    print(f"converter eps demo PASS  ({d})")


def demo_eps_input() -> None:
    """EPS 输入自检：gs 渲染矢量 EPS -> PNG/PDF，及自产 EPS 往返像素一致。

    需要 Ghostscript（vendor/gs 或 PATH），找不到则 SKIP。
    """
    if not _gs_exe():
        print("eps-input demo SKIP (无 Ghostscript)")
        return
    import tempfile

    d = tempfile.mkdtemp(prefix="cv_epsin_")
    o72 = Options(dpi=72)

    # 1) 矢量 EPS：左下红(0,0-50,50) / 右上绿(50,50-100,100)，bbox 100x100pt
    vsrc = os.path.join(d, "vec.eps")
    with open(vsrc, "w", encoding="ascii") as f:
        f.write("%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\n"
                "0.8 0 0 setrgbcolor 0 0 50 50 rectfill\n"
                "0 0.6 0 setrgbcolor 50 50 50 50 rectfill\n"
                "showpage\n%%EOF\n")
    png = convert(vsrc, d, "PNG", o72)[0]
    with Image.open(png) as p:
        assert p.size == (100, 100), f"72dpi 渲染应 100px，实际 {p.size}"
        assert p.getpixel((25, 75))[:3] == (204, 0, 0), "左下应为红"
        assert p.getpixel((75, 25))[:3] == (0, 153, 0), "右上应为绿"
    pdf = convert(vsrc, d, "PDF", o72)[0]
    _assert_pdf_pages(pdf, 1)                       # gs pdfwrite 产出的 PDF 可被 QtPdf 打开
    svg = convert(vsrc, d, "SVG", o72)[0]
    assert svg.endswith(".svg")

    # 2) 自产光栅 EPS 往返：PNG -> EPS -> PNG。EPS 几何 200x100pt，72dpi 渲染还原 200x100 网格直接比对
    src = os.path.join(d, "rgb.png")
    im = Image.new("RGB", (200, 100))
    im.putdata([(x * 3 % 256, y * 5 % 256, (x + y) % 256) for y in range(100) for x in range(200)])
    for yy in range(20):
        for xx in range(20):
            im.putpixel((xx, yy), (255, 0, 0))      # 左上角红色，校验方向
    im.save(src)
    eps = convert(src, d, "EPS", Options())[0]
    back = convert(eps, d, "PNG", Options(dpi=72))[0]
    with Image.open(back) as b:
        assert b.size == (200, 100), f"72dpi 往返应还原 200x100，实际 {b.size}"
        bb = b.convert("RGB")
        same = sum(1 for a, c in zip(im.getdata(), bb.getdata())
                   if all(abs(x - y) <= 2 for x, y in zip(a, c)))
        assert same > 20000 * 0.99, f"往返像素一致率过低 {same}/20000"
        assert bb.getpixel((0, 0))[:3] == (255, 0, 0), "左上应红（方向正确）"

    # 3) EPS -> EPS 直通复制
    eps2 = convert(eps, d, "EPS", Options())[0]
    assert os.path.getsize(eps2) == os.path.getsize(eps)
    print(f"converter eps-input demo PASS  ({d})")


def _assert_pdf_pages(path: str, n: int) -> None:
    from PySide6 import QtPdf
    doc = QtPdf.QPdfDocument()
    err = doc.load(path)
    assert err == QtPdf.QPdfDocument.Error.None_, f"PDF 无法打开: {err}"
    assert doc.pageCount() == n
    doc.close()


if __name__ == "__main__":
    demo()
    demo_pdf()
    demo_svg()
    demo_eps()
    demo_eps_input()
