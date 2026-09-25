"""SRT 时间避让：修整重叠或间隔过短的相邻字幕。

规则：

- 每块时长、顺序与正文保持不变，只允许整体平移（入点、出点同步移动）；
- 相邻两块「前块出点 → 后块入点」的间隔不得小于最小块间隔（重叠视为负间隔）；
- 每块平移量（新入点 − 原入点）的绝对值不超过单块最大平移量，时间不得为负；
- 在所有可行时间轴中，先求「全段最小的最大绝对平移量」（minimax，
  对平移上限做整数二分 + 前向传递可行性检查），避免逐块贪心把冲突
  不断推给后面的块；最大平移量相同的可行轴中，再让每块入点尽量贴近
  原入点——即最小化总绝对平移量。该次目标经「间隔约束 → 非降约束」
  变换后化为带盒约束的 L1 保序回归，用大 M 罚项折入盒约束后以
  PAVA（相邻违例块合并、块内取中位数区间）精确求解；
- 平移上限内不存在可行时间轴时，抛出带首个冲突块号的 ValueError，
  由调用方拒绝导出。
"""
from __future__ import annotations

from bisect import insort
from dataclasses import dataclass

from .parsers import SrtItem, format_tc


@dataclass
class ShiftRow:
    """单个字幕块的避让结果。"""

    index: int            # 原 SRT 声明的序号（导出时保留）
    old_start_ms: int
    old_end_ms: int
    old_start_tc: str
    old_end_tc: str
    new_start_ms: int
    new_end_ms: int
    new_start_tc: str
    new_end_tc: str
    shift_ms: int                 # 实际平移量（新入点 − 原入点，带符号）
    text: str                     # 正文（多行以空格连接，供预览）
    body_lines: tuple[str, ...]   # 原始正文行（供导出，保留换行）


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def _earliest_starts(
    starts: list[int], durations: list[int], min_gap_ms: int, max_shift_ms: int
) -> list[int]:
    """给定平移上限，前向传递求每块入点的最早可行时间（含非负约束）。"""
    earliest: list[int] = []
    prev_end_plus_gap: int | None = None
    for i, start in enumerate(starts):
        lo = max(0, start - max_shift_ms)
        if prev_end_plus_gap is not None and prev_end_plus_gap > lo:
            lo = prev_end_plus_gap
        earliest.append(lo)
        prev_end_plus_gap = lo + durations[i] + min_gap_ms
    return earliest


# ---------- 带盒约束的 L1 保序回归（PAVA） ----------
#
# 每点代价为凸分段线性：c_i(t) = |t-y_i| + big·max(0, lo_i-t) + big·max(0, t-hi_i)，
# 基础斜率 -(1+big)，在 lo_i / y_i / hi_i 处斜率分别 +big / +2 / +big。
# 块代价为块内各点代价之和，其最小值点集（中位数区间）由累计斜率过零点给出。
# 块结构：断点表（(值, 斜率增量) 按值排序，同值不去重、区间计算时聚合）、
# 基础斜率、中位数区间、起始下标与长度。


def _refresh_interval(blk: list) -> None:
    """重算块代价的最小值点区间 [mlo, mhi]（累计斜率过零区间）。"""
    pts, base = blk[0], blk[1]
    cs = base
    mlo = mhi = None
    i = 0
    size = len(pts)
    while i < size:
        value = pts[i][0]
        jump = pts[i][1]
        i += 1
        while i < size and pts[i][0] == value:  # 同值断点的斜率增量聚合
            jump += pts[i][1]
            i += 1
        if cs <= 0:
            mhi = value            # 左斜率 <= 0 的最右断点
        cs += jump
        if mlo is None and cs >= 0:
            mlo = value            # 右斜率首次 >= 0 的断点
    blk[2], blk[3] = mlo, mhi


def _merge_pts(a: list, b: list) -> list:
    """合并两个有序断点表：小表逐项插入大表（每个断点最多被移动 O(log n) 次）。"""
    if len(a) < len(b):
        a, b = b, a
    for item in b:
        insort(a, item)
    return a


def _isotonic_l1_boxed(
    ys: list[int], los: list[int], his: list[int]
) -> list[int]:
    """精确求解 min Σ|t_i − y_i|，约束 t 非降且 lo_i ≤ t_i ≤ hi_i（整数域）。

    盒约束以大 M 罚项折入代价：M 大于任何可行解数据代价总和的上界，
    故罚问题最优解必满足盒约束，且在可行解中与目标一致。
    PAVA：追加单点块，相邻两块的中位数区间违例（左块 mhi > 右块 mlo）
    时合并，最终每块取最左中位数作为该块水平。
    调用前需保证问题可行（盒约束与链式约束可同时满足）。
    """
    n = len(ys)
    big = n * (max(his) - min(los) + 1) + 1
    blocks: list[list] = []
    for i in range(n):
        pts = sorted(((los[i], big), (ys[i], 2), (his[i], big)))
        blk = [pts, -(1 + big), 0, 0, i, 1]
        _refresh_interval(blk)
        blocks.append(blk)
        while len(blocks) >= 2 and blocks[-2][3] > blocks[-1][2]:
            top = blocks.pop()
            prev = blocks.pop()
            merged = [
                _merge_pts(prev[0], top[0]),
                prev[1] + top[1],
                0,
                0,
                prev[4],
                prev[5] + top[5],
            ]
            _refresh_interval(merged)
            blocks.append(merged)
    out = [0] * n
    for blk in blocks:
        for i in range(blk[4], blk[4] + blk[5]):
            out[i] = blk[2]
    return out


def shift_subtitles(
    items: list[SrtItem], min_gap_ms: int, max_shift_ms: int
) -> list[ShiftRow]:
    """求全段最小最大平移量下的可行时间轴，并按原入点就近取定每块。

    无法满足约束（平移上限内不存在可行时间轴）或原块时长非正时，
    抛出带具体块号的 ValueError（调用方应拒绝导出）。
    """
    if min_gap_ms < 0:
        raise ValueError("最小块间隔不能为负")
    if max_shift_ms < 0:
        raise ValueError("单块最大平移量不能为负")
    if not items:
        return []

    starts = [it.start_ms for it in items]
    durations: list[int] = []
    for pos, it in enumerate(items, start=1):
        duration = it.end_ms - it.start_ms
        if duration <= 0:
            label = _block_label(pos, it.index)
            raise ValueError(
                f"{label}入点不早于出点"
                f"（{it.start_tc} --> {it.end_tc}），无法保持时长避让"
            )
        durations.append(duration)

    n = len(items)
    # 平移上限内仍不可行：前向传递求每块最早可行入点，指出首个超限的块
    earliest = _earliest_starts(starts, durations, min_gap_ms, max_shift_ms)
    for i, est in enumerate(earliest):
        if est > starts[i] + max_shift_ms:
            label = _block_label(i + 1, items[i].index)
            lo = max(0, starts[i] - max_shift_ms)
            raise ValueError(
                f"{label}在平移上限 {max_shift_ms} ms 内无法满足最小间隔："
                f"入点最早只能到 {format_tc(est)}，超出允许入点范围 "
                f"[{format_tc(lo)}, {format_tc(starts[i] + max_shift_ms)}]，"
                "请增大单块最大平移量或减小最小块间隔"
            )

    # 整数二分：全段最小的最大绝对平移量。
    # 约束边界均为整数毫秒，最优值必为整数；可行性随上限单调，可二分。
    lo, hi = 0, max_shift_ms
    while lo < hi:
        mid = (lo + hi) // 2
        cand = _earliest_starts(starts, durations, min_gap_ms, mid)
        if all(cand[i] <= starts[i] + mid for i in range(n)):
            hi = mid
        else:
            lo = mid + 1
    best = lo

    # 次目标「就近」：在 |平移| ≤ best 的可行轴中最小化总绝对平移量。
    # 令 t'_i = t_i − C_i（C_i 为前 i−1 块「时长+间隔」累计），
    # 链式间隔约束 t_{i+1} − t_i ≥ 时长_i+间隔 化为 t' 非降，
    # 盒约束随之平移，问题化为带盒约束的 L1 保序回归。
    cum = [0] * n
    acc = 0
    for i in range(n):
        cum[i] = acc
        acc += durations[i] + min_gap_ms
    ys = [starts[i] - cum[i] for i in range(n)]
    los = [max(0, starts[i] - best) - cum[i] for i in range(n)]
    his = [starts[i] + best - cum[i] for i in range(n)]
    levels = _isotonic_l1_boxed(ys, los, his)

    rows: list[ShiftRow] = []
    for i, item in enumerate(items):
        new_start = levels[i] + cum[i]
        new_end = new_start + durations[i]
        rows.append(
            ShiftRow(
                index=item.index,
                old_start_ms=item.start_ms,
                old_end_ms=item.end_ms,
                old_start_tc=item.start_tc,
                old_end_tc=item.end_tc,
                new_start_ms=new_start,
                new_end_ms=new_end,
                new_start_tc=format_tc(new_start),
                new_end_tc=format_tc(new_end),
                shift_ms=new_start - item.start_ms,
                text=item.raw_text,
                body_lines=item.body_lines,
            )
        )
    return rows


def build_shift_srt(rows: list[ShiftRow]) -> str:
    """生成避让后的 SRT：保留原序号与字幕正文（含多行），仅时间码变化。"""
    blocks = []
    for row in rows:
        lines = [str(row.index), f"{row.new_start_tc} --> {row.new_end_tc}"]
        lines.extend(row.body_lines or (row.text,))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
