"""SRT 时间轴重定时：按校准点做分段线性映射。

- 校准点为按顺序排列的「原时间 → 目标时间」对，两列时间都须严格递增；
- 相邻校准点之间对每个时间做分段线性映射；
- 区间之外沿用最近区段的斜率外推；只有一个校准点时整体平移；
- 结果统一取整到毫秒（四舍五入，0.5 向上）；
- 映射后若出现负时间、入点不早于出点或字幕入点顺序倒退，
  抛出带具体块号的 ValueError，由调用方拒绝导出。
"""
from __future__ import annotations

import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction

from .parsers import SrtItem, format_tc

# HH:MM:SS 或 HH:MM:SS,mmm / HH:MM:SS.mmm（毫秒位 1~3 位）
_TC_RE = re.compile(
    r"^\s*(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?\s*$"
)

MAX_ERROR_DETAILS = 10    # 校验失败时最多列出的问题条数


@dataclass
class CalibPoint:
    source_ms: int        # 原时间（毫秒）
    target_ms: int        # 目标时间（毫秒）


@dataclass
class RetimeRow:
    new_index: int        # 重新编号（从 1 开始）
    index: int            # 原 SRT 声明的序号
    old_start_tc: str
    old_end_tc: str
    new_start_ms: int
    new_end_ms: int
    new_start_tc: str
    new_end_tc: str
    text: str                       # 正文（多行以空格连接，供预览）
    body_lines: tuple[str, ...]     # 原始正文行（供导出，保留换行）


def parse_timecode(text: str) -> int:
    """把 HH:MM:SS(,mmm) 时间码解析为毫秒，非法格式抛 ValueError。"""
    match = _TC_RE.match(text or "")
    if not match:
        raise ValueError(f"无法解析时间码「{text}」，格式应为 HH:MM:SS,mmm")
    h, m, s, ms = match.groups()
    if int(m) >= 60 or int(s) >= 60:
        raise ValueError(f"时间码「{text}」的分、秒须小于 60")
    return (
        int(h) * 3600000
        + int(m) * 60000
        + int(s) * 1000
        + int((ms or "0").ljust(3, "0"))
    )


def validate_points(points: list[CalibPoint]) -> None:
    """校验校准点：至少一个，且原时间、目标时间两列都严格递增。"""
    if not points:
        raise ValueError("至少需要一个校准点")
    for i in range(1, len(points)):
        prev, cur = points[i - 1], points[i]
        if cur.source_ms <= prev.source_ms:
            raise ValueError(
                f"第 {i + 1} 个校准点原时间 {format_tc(cur.source_ms)} 未严格大于"
                f"第 {i} 个的 {format_tc(prev.source_ms)}：两列时间都必须严格递增"
            )
        if cur.target_ms <= prev.target_ms:
            raise ValueError(
                f"第 {i + 1} 个校准点目标时间 {format_tc(cur.target_ms)} 未严格大于"
                f"第 {i} 个的 {format_tc(prev.target_ms)}：两列时间都必须严格递增"
            )


def _round_ms(value: Fraction) -> int:
    """四舍五入到整数毫秒（0.5 向上）。"""
    return math.floor(value + Fraction(1, 2))


def map_time(t_ms: int, points: list[CalibPoint]) -> int:
    """把原时间映射为目标时间并取整到毫秒。

    相邻校准点间分段线性插值；区间外沿用最近区段斜率外推；
    只有一个校准点时整体平移。要求 points 已通过 validate_points。
    """
    if len(points) == 1:
        only = points[0]
        return t_ms + (only.target_ms - only.source_ms)
    sources = [p.source_ms for p in points]
    # t 落在 [sources[i], sources[i+1]] 区段；区间外钳到首/末区段做外推
    i = bisect_right(sources, t_ms) - 1
    i = max(0, min(i, len(points) - 2))
    p0, p1 = points[i], points[i + 1]
    slope = Fraction(p1.target_ms - p0.target_ms, p1.source_ms - p0.source_ms)
    return _round_ms(p0.target_ms + (t_ms - p0.source_ms) * slope)


def _fmt_signed(ms: int) -> str:
    """格式化可能为负的毫秒值，用于错误信息。"""
    return f"-{format_tc(-ms)}" if ms < 0 else format_tc(ms)


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def retime_subtitles(
    items: list[SrtItem], points: list[CalibPoint]
) -> list[RetimeRow]:
    """对每个字幕块的入点、出点做映射，并校验结果合法性。

    任一字幕块映射后出现负时间、入点不早于出点或入点顺序倒退，
    抛出带具体块号的 ValueError（调用方应拒绝导出）。
    """
    validate_points(points)
    rows: list[RetimeRow] = []
    errors: list[str] = []
    prev_start: int | None = None
    for pos, item in enumerate(items, start=1):
        new_start = map_time(item.start_ms, points)
        new_end = map_time(item.end_ms, points)
        label = _block_label(pos, item.index)
        if new_start < 0 or new_end < 0:
            errors.append(
                f"{label}映射后出现负时间"
                f"（{_fmt_signed(new_start)} --> {_fmt_signed(new_end)}）"
            )
        elif new_start >= new_end:
            errors.append(
                f"{label}映射后入点不早于出点"
                f"（{format_tc(new_start)} --> {format_tc(new_end)}）"
            )
        if prev_start is not None and new_start < prev_start:
            errors.append(
                f"{label}映射后入点 {_fmt_signed(new_start)} 早于前一块入点"
                f" {_fmt_signed(prev_start)}，字幕入点顺序倒退"
            )
        prev_start = new_start
        rows.append(
            RetimeRow(
                new_index=pos,
                index=item.index,
                old_start_tc=item.start_tc,
                old_end_tc=item.end_tc,
                new_start_ms=new_start,
                new_end_ms=new_end,
                new_start_tc=format_tc(new_start) if new_start >= 0 else "",
                new_end_tc=format_tc(new_end) if new_end >= 0 else "",
                text=item.raw_text,
                body_lines=item.body_lines,
            )
        )
    if errors:
        shown = "；".join(errors[:MAX_ERROR_DETAILS])
        if len(errors) > MAX_ERROR_DETAILS:
            shown += f"；……共 {len(errors)} 处问题"
        raise ValueError(shown)
    return rows


def build_srt(rows: list[RetimeRow]) -> str:
    """生成重定时后的 SRT：保留字幕正文（含多行），从 1 重新编号。"""
    blocks = []
    for row in rows:
        lines = [str(row.new_index), f"{row.new_start_tc} --> {row.new_end_tc}"]
        lines.extend(row.body_lines or (row.text,))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
