"""逐行对白稿与 SRT 解析，SRT 保留时间码。"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)


@dataclass
class ScriptItem:
    index: int          # 对白稿行号（从 1 开始）
    raw_text: str       # 原始行文本


@dataclass
class SrtItem:
    index: int          # SRT 块内声明的序号；缺失/非法时按顺序编号
    raw_text: str       # 全部字幕文本行（多行以空格连接）
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    body_lines: tuple[str, ...] = ()   # 原始字幕正文行（保留换行，供导出使用）


def parse_script(text: str) -> list[ScriptItem]:
    """按非空行切分对白稿，逐行成为一个条目。"""
    items: list[ScriptItem] = []
    for line in text.lstrip("﻿").splitlines():
        stripped = line.strip()
        if stripped:
            items.append(ScriptItem(index=len(items) + 1, raw_text=stripped))
    return items


def _ms(h: str, m: str, s: str, ms: str) -> int:
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms.ljust(3, "0"))


def format_tc(ms: int) -> str:
    """把非负毫秒格式化为 SRT 时间码 HH:MM:SS,mmm。"""
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def parse_srt(text: str) -> list[SrtItem]:
    """解析标准 SRT。

    以空行分块；每块中定位 ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` 时间码行，
    其前的单个纯数字行作为序号，其后的所有行作为字幕文本。找不到时间码
    的块抛出 ValueError。
    """
    text = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", text.strip())
    items: list[SrtItem] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        tc_idx = -1
        match = None
        for i, ln in enumerate(lines):
            match = _TIME_RE.search(ln)
            if match:
                tc_idx = i
                break
        if tc_idx < 0 or match is None:
            raise ValueError(
                f"第 {len(items) + 1} 个字幕块缺少有效时间码行，无法解析为 SRT"
            )
        declared = items[-1].index + 1 if items else 1
        if tc_idx >= 1 and re.fullmatch(r"\d{1,6}", lines[tc_idx - 1]):
            declared = int(lines[tc_idx - 1])
        start_ms = _ms(*match.groups()[0:4])
        end_ms = _ms(*match.groups()[4:8])
        body_lines = lines[tc_idx + 1:]
        if not body_lines:
            raise ValueError(f"序号 {declared} 的字幕块没有正文文本")
        items.append(
            SrtItem(
                index=declared,
                raw_text=" ".join(body_lines).strip(),
                start_ms=start_ms,
                end_ms=end_ms,
                start_tc=format_tc(start_ms),
                end_tc=format_tc(end_ms),
                body_lines=tuple(body_lines),
            )
        )
    return items
