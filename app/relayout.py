"""SRT 断行排版：按显示宽度把每个字幕块正文排成最多两行。

规则：

- 显示宽度：东亚宽字符（East Asian Width 为 W/F）计 2，其余计 1；
  组合字符（Unicode 类别 M）宽度计 0，且必须附着在前字之后，不可拆开。
- 连续窄体拉丁/希腊/西里尔字母（及数字）构成的单词不可拆分；
  单词内部的撇号（如 don't、l'amour）同样视为单词的一部分。
- 合法断点仅有两类：① 空白处（换行后空白折叠丢弃）；
  ② 两个相邻词组之间（如中日韩字之间、字与词之间）。
- 枚举全部合法断点并过滤掉任一行超宽的排法，再按
  「句末标点优先（句末 3 > 句中标点 2 > 无 0）、
  两行宽度差最小、差相同则首行更满」综合选优，
  不做固定字数贪心截断。空白断点本身不加额外优先级
  （纯拉丁文本的候选本就全部位于空格处）。
- 整段一行能放下时保持一行；不存在任何合法两行排法时，
  保留原正文并标记「需人工处理」。

不改动时间码与文字内容本身，仅调整换行位置（原块内多行先按
窄体字母/数字两侧相接补空格的方式还原为整句再排版）。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .parsers import SrtItem

# 断点优先级
LEVEL_NONE = 0       # 无标点依托（含普通空白断点）
LEVEL_CLAUSE = 2     # 句中标点：，、；：,;: 等
LEVEL_SENTENCE = 3   # 句末标点：。！？….!? 等

# 允许作为单词内部连接符的窄字符（两侧均为字母时不拆）
_GLUE_CHARS = {"'", "’", "＇"}

# 闭合符号（引号/括号）：本身不是断点，其断点级别继承前面最近的标点。
# Unicode 类别 Pf（结束引号）/ Pe（结束括号）自动覆盖，
# 这里再补直引号等不带类别的常见闭合符号。
_CLOSING_CHARS = set("”’」』）)】》〉｝\"'›»‛")

_SENTENCE_END = set("。！？…．.!?")
_CLAUSE_END = set("，、；：,;")


@dataclass
class _Cluster:
    """一个「字簇」：基字符 + 紧随的组合字符序列。"""

    chars: str           # 完整字簇文本
    width: int           # 显示宽度（组合字符计 0）
    word_base: bool      # 是否为窄体字母/数字（组词用）


@dataclass
class RelayoutRow:
    """单个字幕块的排版结果。"""

    index: int                       # SRT 声明序号（导出时保留）
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    original_text: str               # 原正文（多行以空格连接，供预览）
    status: str                      # single / wrapped / manual
    lines: tuple[str, ...]           # 排版后正文行
    body_lines: tuple[str, ...]      # 导出用正文行（manual 时为原行）
    reason: str = ""                 # manual 时的原因说明

    @property
    def result_text(self) -> str:
        return "\n".join(self.lines)

    @property
    def line_count(self) -> int:
        return len(self.lines)


# ---------- 字符宽度与组词 ----------

def _char_width(ch: str) -> int:
    """东亚宽字符（W/F）计 2，其余计 1。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _is_word_base(ch: str, width: int) -> bool:
    """窄体拉丁/希腊/西里尔字母或窄体十进制数字：连续出现即构成不可拆单词。"""
    if width != 1:
        return False
    cat = unicodedata.category(ch)
    if cat == "Nd":
        return True
    if cat.startswith("L"):
        return unicodedata.name(ch, "").startswith(
            ("LATIN", "CYRILLIC", "GREEK")
        )
    return False


def _is_narrow_alnum(ch: str) -> bool:
    """窄体字母/数字（用于跨原行还原整句时判断是否需要补空格）。"""
    if not ch or _char_width(ch) != 1:
        return False
    return ch.isalnum()


def _gap_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


# ---------- 原块多行还原为整句 ----------

def flatten_body(body_lines: tuple[str, ...] | list[str]) -> str:
    """把块内多行还原为整句。

    沿用解析时「多行以空格连接」的入口约定：仅当上行末与下行首都是
    窄体字母/数字（即被换行拆开的西文词/数字）时补一个空格，
    中日韩文字直接相接，不凭空增删文字。
    """
    if not body_lines:
        return ""
    text = body_lines[0]
    for nxt in body_lines[1:]:
        if text and nxt and _is_narrow_alnum(text[-1]) and _is_narrow_alnum(nxt[0]):
            text += " "
        text += nxt
    return text


# ---------- 切字簇 ----------

def _tokenize(text: str) -> tuple[list[_Cluster], list[str]]:
    """切分为字簇序列与等长的前置空白序列。

    - 组合字符附着到前一个字簇（即使其自身宽字符计宽，组合符计 0）；
    - 连续空白合并为一段 gap；首尾空白丢弃；
    - gaps[i] 为第 i 个字簇之前的空白文本（gaps[0] 恒为 ""）。
    """
    clusters: list[_Cluster] = []
    gaps: list[str] = []
    pending_ws: list[str] = []

    for ch in text:
        if ch.isspace():
            if clusters:  # 字簇出现之前的前导空白直接丢弃
                pending_ws.append(ch)
            continue
        if (
            unicodedata.category(ch).startswith("M")
            and clusters
            and not pending_ws   # 不跨过空白附着
        ):
            clusters[-1].chars += ch
            continue
        width = _char_width(ch)
        clusters.append(
            _Cluster(chars=ch, width=width, word_base=_is_word_base(ch, width))
        )
        gaps.append("".join(pending_ws))
        pending_ws = []
    return clusters, gaps


def _word_groups(
    clusters: list[_Cluster], gaps: list[str]
) -> list[int]:
    """给每个字簇分配词组编号；非词字符为 -1。

    连续窄体字母/数字属同一词组；被撇号连接且两侧都是字母的
    （don't、l'amour）同样并入同一词组，保证单词不被拆开。
    """
    n = len(clusters)
    groups = [-1] * n
    gid = 0
    i = 0
    while i < n:
        if not clusters[i].word_base:
            i += 1
            continue
        groups[i] = gid
        j = i
        while j + 1 < n:
            if clusters[j + 1].word_base and gaps[j + 1] == "":
                j += 1
                groups[j] = gid
                continue
            # 撇号连接：字母 + 撇号 + 字母，且中间均无空白
            if (
                j + 2 < n
                and gaps[j + 1] == ""
                and gaps[j + 2] == ""
                and clusters[j + 1].chars in _GLUE_CHARS
                and clusters[j + 2].word_base
            ):
                groups[j + 1] = gid
                groups[j + 2] = gid
                j += 2
                continue
            break
        gid += 1
        i = j + 1
    return groups


def _break_levels(clusters: list[_Cluster]) -> list[int]:
    """每个字簇作为「行尾」时的断点级别。

    闭合引号/括号自身不带标点级别，继承其前面最近的句末/句中标点级别
    （如「你好。」”」整体仍按句末断点对待）。
    """
    levels = [LEVEL_NONE] * len(clusters)
    last_punct_level = LEVEL_NONE
    for i, c in enumerate(clusters):
        base = c.chars[0]
        if base in _SENTENCE_END:
            last_punct_level = LEVEL_SENTENCE
            levels[i] = LEVEL_SENTENCE
        elif base in _CLAUSE_END:
            last_punct_level = LEVEL_CLAUSE
            levels[i] = LEVEL_CLAUSE
        elif base in _CLOSING_CHARS or unicodedata.category(base) in ("Pe", "Pf"):
            levels[i] = last_punct_level
        else:
            last_punct_level = LEVEL_NONE
    return levels


# ---------- 单行/两行排版 ----------

def _join(
    clusters: list[_Cluster], gaps: list[str], start: int, end: int
) -> str:
    """按原空白连接 [start, end) 内的字簇。"""
    if start >= end:
        return ""
    parts = [clusters[start].chars]
    for i in range(start + 1, end):
        parts.append(gaps[i])
        parts.append(clusters[i].chars)
    return "".join(parts)


def layout_block(text: str, max_width: int) -> tuple[str, tuple[str, ...], str]:
    """把一段正文排成最多两行。

    返回 ``(status, lines, reason)``：status 为
    ``single`` / ``wrapped`` / ``manual``；manual 时 lines 为 (原文,)。
    """
    clusters, gaps = _tokenize(text)
    n = len(clusters)
    if n == 0:
        return "manual", (text,), "正文为空"

    groups = _word_groups(clusters, gaps)
    levels = _break_levels(clusters)

    # 前缀宽度：cw[i] = 前 i 个字簇宽度和；
    # gw[i] = gaps[1..i] 的宽度和（gaps[0] 恒空）
    cw = [0] * (n + 1)
    gw = [0] * n
    for i, c in enumerate(clusters):
        cw[i + 1] = cw[i] + c.width
    for i in range(1, n):
        gw[i] = gw[i - 1] + _gap_width(gaps[i])

    total = cw[n] + (gw[n - 1] if n > 1 else 0)
    if total <= max_width:
        return "single", (_join(clusters, gaps, 0, n),), ""

    # 枚举所有合法断点 k：第一行取前 k 个字簇，k ∈ [1, n-1]
    feasible: list[tuple[int, int, int, int]] = []  # (级别, -宽度差, -首行宽, k)
    saw_legal = False
    for k in range(1, n):
        # 同一词组内部（连续字母/数字、撇号连接）不可断
        if groups[k - 1] >= 0 and groups[k - 1] == groups[k]:
            continue
        saw_legal = True
        # 第一行：含 gaps[1..k-1]，断点处 gaps[k] 换行丢弃
        w1 = cw[k] + (gw[k - 1] if k - 1 >= 1 else 0)
        # 第二行：含 gaps[k+1..n-1]
        w2 = cw[n] - cw[k] + (gw[n - 1] - gw[k])
        if w1 > max_width or w2 > max_width:
            continue
        feasible.append((levels[k - 1], -abs(w1 - w2), -w1, k))

    if not feasible:
        if not saw_legal:
            reason = "整段为不可拆分的连续单词（或无任何合法断点），请人工断行"
        else:
            reason = (
                f"所有合法断点都无法使两行宽度均不超过 {max_width}"
                "（最窄排法仍超宽），请人工断行"
            )
        return "manual", (text.strip(),), reason

    _, _, _, k = max(feasible)
    line1 = _join(clusters, gaps, 0, k)
    line2 = _join(clusters, gaps, k, n)
    return "wrapped", (line1, line2), ""


# ---------- 逐块排版与导出 ----------

def relayout_subtitles(
    items: list[SrtItem], max_width: int
) -> list[RelayoutRow]:
    """逐块排版，不改动时间码；无合法排法的块保留原正文并标记人工处理。"""
    rows: list[RelayoutRow] = []
    for item in items:
        text = flatten_body(tuple(item.body_lines) or (item.raw_text,))
        status, lines, reason = layout_block(text, max_width)
        if status == "manual":
            # 保留原正文（原多行结构），不改写文字
            body = tuple(item.body_lines) or (item.raw_text,)
            preview_lines = body
        else:
            body = lines
            preview_lines = lines
        rows.append(
            RelayoutRow(
                index=item.index,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                start_tc=item.start_tc,
                end_tc=item.end_tc,
                original_text=item.raw_text,
                status=status,
                lines=tuple(preview_lines),
                body_lines=tuple(body),
                reason=reason,
            )
        )
    return rows


def build_relayout_srt(rows: list[RelayoutRow]) -> str:
    """生成排版后的 SRT：保留原序号与时间码，仅正文换行可能变化。"""
    blocks = []
    for row in rows:
        lines = [str(row.index), f"{row.start_tc} --> {row.end_tc}"]
        lines.extend(row.body_lines or (row.original_text,))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
