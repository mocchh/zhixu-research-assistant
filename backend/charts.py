"""Bounded CSV parsing and fixed chart templates; never execute uploaded code."""

from __future__ import annotations

import base64
import csv
import io
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import warnings as python_warnings

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "research-assistant-matplotlib"))

from matplotlib import font_manager, rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure


_MAX_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 10000
_PLOT_LOCK = threading.Lock()
_MISSING = {"", "na", "n/a", "null", "none", "-"}
_PALETTE = ["#166534", "#2563eb", "#d97706"]


def _number(value: str) -> float | None:
    value = value.strip()
    if value.lower() in _MISSING:
        return None
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
        value = value.replace(",", "")
    result = float(value)
    if not math.isfinite(result):
        raise OverflowError("non-finite numeric value")
    return result


def _parse(content: bytes) -> tuple[list[str], list[dict], list[str], dict[str, list[float | None]], list[str]]:
    if not content or len(content) > _MAX_BYTES:
        raise ValueError("CSV 不能为空，文件大小不能超过 2 MiB。")
    notices = []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError:
            raise ValueError("无法解码 CSV，请使用 UTF-8 或 GB18030 编码。") from None
        notices.append("文件已按 GB18030 编码读取。")
    if "\x00" in text:
        raise ValueError("CSV 包含空字节，请确认上传的是文本文件。")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        first = text.splitlines()[0] if text.splitlines() else ""
        delimiter = max((",", ";", "\t"), key=first.count)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        columns = [column.strip() for column in next(reader)]
        if len(columns) < 2 or len(columns) > 100:
            raise ValueError("CSV 需要 2–100 列，并且第一行必须为列名。")
        if any(not column for column in columns) or len(set(columns)) != len(columns):
            raise ValueError("CSV 列名不能为空或重复。")
        rows = []
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) > len(columns):
                raise ValueError(f"CSV 第 {reader.line_num} 行字段数超过表头，请检查分隔符或引号。")
            row += [""] * (len(columns) - len(row))
            rows.append(dict(zip(columns, (cell.strip() for cell in row))))
            if len(rows) > _MAX_ROWS:
                raise ValueError("CSV 数据不能超过 10000 行。")
    except (csv.Error, StopIteration):
        raise ValueError("CSV 格式无效，请检查表头、分隔符和成对引号。") from None
    if not rows:
        raise ValueError("CSV 只有表头，没有可绘制的数据。")
    numeric_columns = []
    values = {}
    for column in columns:
        converted = []
        invalid = 0
        present = 0
        for row_index, row in enumerate(rows, start=2):
            try:
                number = _number(row[column])
                converted.append(number)
                present += number is not None
            except OverflowError:
                raise ValueError(f"第 {row_index} 行的「{column}」包含 NaN、Infinity 或超出范围的数字。") from None
            except ValueError:
                invalid += 1
                converted.append(None)
        if not invalid and present:
            numeric_columns.append(column)
            values[column] = converted
        elif invalid and present:
            notices.append(f"「{column}」混有 {invalid} 个非数字值，未将其识别为数值列。")
    return columns, rows, numeric_columns, values, notices


def _font() -> tuple[str, bool]:
    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        if Path(candidate).is_file():
            try:
                font_manager.fontManager.addfont(candidate)
                return font_manager.FontProperties(fname=candidate).get_name(), True
            except (RuntimeError, OSError):
                pass
    for name in ("PingFang SC", "Arial Unicode MS", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name, True
        except ValueError:
            pass
    return "DejaVu Sans", False


def _format(value: float) -> str:
    return f"{value:.6g}"


def render_csv(content: bytes, kind: str = "line", x: str | None = None, y: str | None = None) -> dict:
    """Render line/bar/scatter plots and a factual caption from uploaded values.

    x defaults to the first column. An explicit y names one numeric column or a
    comma-separated list of at most three; otherwise the first three are used.
    Missing values remain gaps for lines and are skipped for bars/scatter.
    """
    if kind not in {"line", "bar", "scatter"}:
        raise ValueError("图表类型仅支持 line、bar 或 scatter。")
    columns, rows, numeric_columns, values, notices = _parse(content)
    x = x if x is not None else columns[0]
    if x not in columns:
        raise ValueError("指定的 X 轴列不存在。")
    y = y.strip() if y is not None else None
    if y == "":
        y = None
    candidates = [column for column in numeric_columns if column != x]
    if y is None:
        selected = candidates[:3]
    else:
        # Prefer an exact header match, so a quoted CSV header containing a
        # comma remains selectable as a single column.
        selected = [y] if y in columns else [column.strip() for column in y.split(",")]
        if len(selected) > 3 or len(set(selected)) != len(selected):
            raise ValueError("Y 轴最多选择 3 个不同的数值列。")
        if any(column not in columns for column in selected):
            raise ValueError("指定的 Y 轴列不存在。")
        if any(column not in numeric_columns for column in selected):
            raise ValueError("指定的 Y 轴列不是有效数值列，或整列均为空。")
    if not selected:
        raise ValueError("没有可用的 Y 轴数值列，请检查数据或手动选择列。")
    if y is None and len(candidates) > 3:
        notices.append("数值列超过 3 个，图中默认显示前 3 个；可选择单列重新绘制。")
    if kind == "scatter" and x not in numeric_columns:
        raise ValueError("散点图的 X 轴也需要有效数值列。")
    valid_x = [i for i, row in enumerate(rows) if (values[x][i] is not None if x in values else row[x].lower() not in _MISSING)]
    if not valid_x:
        raise ValueError("X 轴列没有有效值。")
    if len(valid_x) < len(rows):
        notices.append(f"已跳过 X 轴缺失的 {len(rows) - len(valid_x)} 行。")
    captions = []
    for column in selected:
        valid = [(i, values[column][i]) for i in valid_x if values[column][i] is not None]
        if not valid:
            raise ValueError(f"「{column}」与 X 轴没有同一行有效数据。")
        numbers = [number for _, number in valid]
        if not math.isfinite(max(numbers) - min(numbers)):
            raise ValueError("数值跨度过大，请先缩放数据再绘图。")
        first, last = numbers[0], numbers[-1]
        delta = last - first
        direction = "增加" if delta > 0 else "减少" if delta < 0 else "不变"
        change = f"，首末值{direction} {_format(abs(delta))}" if delta else "，首末值相同"
        captions.append(f"「{column}」有 {len(numbers)} 个有效值，按 CSV 行序从 {_format(first)} 到 {_format(last)}{change}，最小值 {_format(min(numbers))}，最大值 {_format(max(numbers))}。")
        missing = len(valid_x) - len(numbers)
        if missing:
            notices.append(f"「{column}」缺失 {missing} 个值，折线保留断点，柱状图和散点图跳过缺失点。")
    plotted = valid_x
    if kind == "bar" and len(valid_x) > 200:
        plotted = sorted({valid_x[round(i * (len(valid_x) - 1) / 199)] for i in range(200)})
        notices.append("柱状图按行序等间隔展示 200 行，以保持可读性；图注的统计仍基于全部有效行。")
    try:
        with _PLOT_LOCK, python_warnings.catch_warnings(record=True) as captured:
            python_warnings.simplefilter("always")
            font, has_chinese = _font()
            if not has_chinese:
                notices.append("未找到中文字体，图内原始列名或标签可能缺字；原始数据不受影响。")
            with rc_context({"font.family": font, "axes.unicode_minus": False, "svg.fonttype": "path", "text.usetex": False, "text.parse_math": False}):
                fig = Figure(figsize=(10, 5.6), dpi=140, layout="constrained", facecolor="#ffffff")
                FigureCanvasAgg(fig)
                axis = fig.subplots()
                axis.set_facecolor("#fbfdfb")
                positions = list(range(len(plotted)))
                numeric_x = kind != "bar" and x in numeric_columns
                xs = [values[x][i] for i in plotted] if numeric_x else positions
                for index, column in enumerate(selected):
                    ys = [values[column][i] if values[column][i] is not None else math.nan for i in plotted]
                    color = _PALETTE[index]
                    if kind == "line":
                        axis.plot(xs, ys, label=column, color=color, linewidth=1.8, marker="o" if len(plotted) <= 30 else None, markersize=4)
                    elif kind == "scatter":
                        axis.scatter(xs, ys, label=column, color=color, s=22, alpha=0.75)
                    else:
                        width = 0.8 / len(selected)
                        shifted = [value + (index - (len(selected) - 1) / 2) * width for value in positions]
                        axis.bar(shifted, ys, width=width, label=column, color=color, alpha=0.9)
                if not numeric_x:
                    ticks = sorted({round(i * (len(plotted) - 1) / max(1, min(11, len(plotted) - 1))) for i in range(min(12, len(plotted)))})
                    axis.set_xticks(ticks, [rows[plotted[tick]][x] for tick in ticks], rotation=35 if len(ticks) > 6 else 0, ha="right" if len(ticks) > 6 else "center")
                axis.set_xlabel(x, labelpad=10)
                axis.set_ylabel(selected[0] if len(selected) == 1 else "Value", labelpad=10)
                axis.set_title(" / ".join(selected) + " · " + x, loc="left", fontsize=14, pad=16)
                axis.grid(axis="y", color="#e2e8e2", linewidth=0.6)
                axis.set_axisbelow(True)
                axis.spines["top"].set_visible(False)
                axis.spines["right"].set_visible(False)
                axis.legend(loc="best", frameon=False)
                svg_output, png_output = io.BytesIO(), io.BytesIO()
                fig.savefig(svg_output, format="svg", metadata={"Date": None})
                fig.savefig(png_output, format="png")
                svg = svg_output.getvalue().decode("utf-8")
                png = base64.b64encode(png_output.getvalue()).decode("ascii")
                fig.clear()
            if any("Glyph" in str(w.message) and "missing" in str(w.message) for w in captured):
                notices.append("部分字符未被当前字体覆盖，图中可能缺字；列名与数据预览保留原文。")
    except (ValueError, OverflowError, RuntimeError) as error:
        raise ValueError("数据无法绘制，请检查数值范围和列选择。") from error
    kind_label = {"line": "折线图", "bar": "柱状图", "scatter": "散点图"}[kind]
    caption = f"图：以「{x}」为横轴的{kind_label}。数据共 {len(rows)} 行。" + "".join(captions) + "以上为描述性统计，不能据此推断统计显著性或因果关系。"
    return {"columns": columns, "numeric_columns": numeric_columns, "rows": len(rows), "preview": rows[:10], "svg": svg, "png_base64": png, "caption": caption, "warnings": list(dict.fromkeys(notices))}
