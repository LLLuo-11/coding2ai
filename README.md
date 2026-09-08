# SciImgConvert · 科研图像批量格式转换工具

面向科研场景的**离线**批量图像格式转换桌面工具（PySide6 GUI + Pillow 引擎）。
解决论文配图、实验成像数据、期刊投稿、PDF 扫描文档之间格式不统一、不合规、
批量处理繁琐的痛点。**完全本地处理：不联网、不上传任何用户文件**，可打包为
独立免环境 EXE。

> 开发技术决策说明见文末「实现要点」。

## 功能特性

- **九类输入 × 九类输出互转**，打通普通图片 / 科研仪器图 / PDF 文档 / 矢量图
- **SCI/期刊投稿级参数**：DPI（150/300/600）、TIFF LZW 无损压缩、JPEG/WEBP 画质
  （10–100）、RGB/灰度转换
- **16 位科研 TIFF → 8 位**：高位保留 / 极差拉伸两档，无损或最大化动态范围
- **内置投稿预设**一键套用
- **批量 + 多线程并行**（1–8 可调），大批量不卡 UI，随时**中止**
- **多页 PDF / 多页 TIFF** 自动分页导出
- **预览**：选中即显示、双击看大图、转换前预估输出（尺寸/页数/打印宽度）
- 拖拽导入、递归子目录、同名自动改名或覆盖、运行日志、一键打开输出目录

### 支持格式

| 方向 | 格式 |
|---|---|
| **输入** | JPG/JPEG、PNG、BMP、WEBP、GIF、TIF/TIFF（8/16 位、多页）、PDF（多页）、SVG、EPS |
| **输出** | PNG、JPEG、WEBP、BMP、TGA、TIFF（LZW/无压缩）、SVG、EPS、PDF |

### 投稿预设

| 预设 | 参数 |
|---|---|
| SCI 彩图投稿 | TIFF · LZW 无损 · 300 DPI · RGB |
| 期刊矢量图 | EPS · 300 DPI · RGB |
| 演示插图 | PNG · 300 DPI · RGB |

## 快速开始

要求：Windows、Python 3.10+。

```bash
pip install -r requirements.txt   # PySide6 + Pillow
python app.py                     # 启动 GUI
```

引擎自检（无 GUI，验证转换保真度，全部输出 PASS）：

```bash
python converter.py
```

## 打包为独立 EXE

```bash
pip install pyinstaller
pyinstaller --noconfirm SciImgConvert.spec   # 产物在 dist/SciImgConvert/
```

## EPS 输入支持

EPS 解析依赖 Ghostscript（需 PostScript 解释器）。引擎定位顺序：
**本目录 `vendor/gs/` → 系统 `PATH`**。

仓库未包含约 41MB 的随包二进制，两种方式启用 EPS 输入：

1. **安装官方 Ghostscript** 并加入 `PATH`（最简单，开发运行即可），或
2. 在 `vendor/gs/` 放置 conda-forge 版自包含目录（打包 EXE 推荐，随包离线）。
   示例：`conda create -p vendor/gsenv -c conda-forge --no-deps ghostscript=10.07.1`
   后将其 `Library/` 内容复制为 `vendor/gs/`，使存在 `vendor/gs/bin/gswin64c.exe`。

> 打包时 `SciImgConvert.spec` 会把 `vendor/gs` 拷入产物；未放回时 EPS 输入
> 会明确报错而非静默失败，其余格式不受影响。

## 实现要点

- **PDF 输出无损**：Pillow 的 PDF 写器硬编码 JPEG(DCT) 有损，本工具自写
  FlateDecode 无损内嵌（灰度→DeviceGray、RGB→DeviceRGB），已用 Ghostscript
  渲染回比验证**像素级无损**。
- **PDF/SVG 输入不绑外部二进制**：用 PySide6 自带 QtPdf（PDFium）按 DPI 渲染、
  QtSvg 解析；SVG→PDF 走 QPrinter **真矢量**。
- **EPS 输出**采用 Pillow 同构封装（`readhexstring` 光栅 EPS）；EPS→PDF 走 gs
  `pdfwrite` 真矢量，EPS→光栅走 gs 渲染。
- 矢量源 → 矢量目标（SVG→PDF、EPS→PDF 等）为真矢量直通；光栅源输出到
  SVG/EPS/PDF 是**无损封装的光栅**容器。

## 项目结构

```
app.py              # PySide6 GUI（导入/参数/预览/并行任务/日志）
converter.py        # 核心转换引擎 + 自检 demo
SciImgConvert.spec  # PyInstaller 打包配置
requirements.txt
```

## 许可证

[MIT](LICENSE)
