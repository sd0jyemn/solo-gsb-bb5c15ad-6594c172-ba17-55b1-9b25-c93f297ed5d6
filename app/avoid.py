"""SRT 时间避让：平移修整重叠或间隔过短的相邻字幕块。

约束（保持每块时长、顺序与正文不变，仅整体平移）：

- 新入点非负；
- 相邻块间隔（下一块入点 − 本块出点）不小于最小块间隔 g；
- 每块平移量（新入点 − 原入点）的绝对值不超过单块最大平移量 L。

目标（按优先级）：

1. 全段最大绝对平移量 M 最小——二分 M + 前向最早可行判定；
2. 在所有达到 M 的可行时间轴中，取各块平移量绝对值之和最小者
   （每块按原入点就近落位）：令 p_i 为间距下限前缀和、t_i = u_i + p_i，
   问题化为「u 非降 + 单调盒约束下最小化 Σ|u_i − r_i|」的 L1 保序回归，
   用 PAVA（块水平 = 块内中位数向块内盒截断、相邻违规即合并）精确求解。

两步均为全段整体求解，不做逐块贪心，避免把冲突推给后面的块。
平移上限内不存在可行时间轴时，由前向判定指出首个冲突块，
调用方应拒绝导出。
"""
from __future__ import annotations

from bisect import insort
from dataclasses import dataclass

from .parsers import SrtItem, format_tc


@dataclass
class AvoidRow:
    new_index: int        # 重新编号（从 1 开始）
    index: int            # 原 SRT 声明的序号
    old_start_tc: str
    old_end_tc: str
    new_start_ms: int
    new_end_ms: int
    new_start_tc: str
    new_end_tc: str
    shift_ms: int         # 平移量：新入点 − 原入点（正为后移，负为前移）
    text: str                       # 正文（多行以空格连接，供预览）
    body_lines: tuple[str, ...]     # 原始正文行（供导出，保留换行）


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def _earliest_starts(
    starts: list[int], gaps: list[int], limit: int
) -> tuple[list[int], int]:
    """平移上限 limit 下逐块最早可行入点；返回 (入点序列, 首个冲突下标)。

    冲突下标为 -1 表示可行；否则为首个「最早可行入点仍超出
    原入点 + limit」的块（从 0 计）。首块入点 max(0, s_0 − limit)
    恒落在盒内，冲突只可能出现在后续块（由间距约束顶出）。
    """
    n = len(starts)
    t = [0] * n
    t[0] = max(0, starts[0] - limit)
    for i in range(1, n):
        t[i] = max(max(0, starts[i] - limit), t[i - 1] + gaps[i - 1])
        if t[i] > starts[i] + limit:
            return t, i
    return t, -1


def _median(sorted_vals: list[int]) -> int:
    """下中位数（偶数个时取靠左者；任一中位数对 L1 目标等价）。"""
    return sorted_vals[(len(sorted_vals) - 1) // 2]


def _bounded_l1_isotonic(
    r: list[int], lo: list[int], hi: list[int]
) -> list[int]:
    """min Σ|u_i − r_i|，约束 u 非降且 lo_i ≤ u_i ≤ hi_i（lo、hi 均非降）。

    PAVA：每块水平 = 块内 r 中位数向 [块内最大 lo, 块内最小 hi] 截断；
    相邻两块水平左高右低即合并，直至整体非降。lo、hi 非降保证
    合并后的块盒 [max lo, min hi] 不会倒置（否则左块水平 ≤ 块内
    最小 hi < 块内最大 lo ≤ 右块水平，不可能触发合并）。
    """
    vals: list[list[int]] = []   # 每块升序存放的 r 值
    blo: list[int] = []          # 每块盒下界（块内 lo 最大值）
    bhi: list[int] = []          # 每块盒上界（块内 hi 最小值）
    size: list[int] = []         # 每块覆盖的原始坐标数
    for i in range(len(r)):
        vals.append([r[i]])
        blo.append(lo[i])
        bhi.append(hi[i])
        size.append(1)
        while len(vals) >= 2:
            lv_left = min(max(_median(vals[-2]), blo[-2]), bhi[-2])
            lv_right = min(max(_median(vals[-1]), blo[-1]), bhi[-1])
            if lv_left <= lv_right:
                break
            merged = vals[-2]
            for v in vals[-1]:
                insort(merged, v)
            vals[-2:] = [merged]
            blo[-2:] = [max(blo[-2], blo[-1])]
            bhi[-2:] = [min(bhi[-2], bhi[-1])]
            size[-2:] = [size[-2] + size[-1]]
    out: list[int] = []
    for blk, low, high, k in zip(vals, blo, bhi, size):
        level = min(max(_median(blk), low), high)
        out.extend([level] * k)
    return out


def _solve_starts(
    starts: list[int],
    durations: list[int],
    min_gap_ms: int,
    max_shift_ms: int,
    labels: list[str],
) -> list[int]:
    """求各块新入点；不可行时抛 ValueError（指出首个冲突块）。"""
    n = len(starts)
    # 相邻块入点间距下限：本块时长 + 最小块间隔
    gaps = [durations[i] + min_gap_ms for i in range(n - 1)]

    # 平移上限内仍不可行：前向判定给出首个冲突块
    earliest, conflict = _earliest_starts(starts, gaps, max_shift_ms)
    if conflict >= 0:
        allow = starts[conflict] + max_shift_ms
        raise ValueError(
            f"{labels[conflict]}无法排入时间轴：为满足与前一块的最小间隔，"
            f"其入点最早为 {format_tc(earliest[conflict])}，"
            f"已超出平移上限允许的最晚入点 {format_tc(allow)}"
            f"（原入点 {format_tc(starts[conflict])}，"
            f"单块最大平移量 {max_shift_ms} 毫秒）。"
            f"请增大单块最大平移量或减小最小块间隔"
        )

    # 二分最小的全段最大绝对平移量 M
    lo_m, hi_m = 0, max_shift_ms
    while lo_m < hi_m:
        mid = (lo_m + hi_m) // 2
        if _earliest_starts(starts, gaps, mid)[1] < 0:
            hi_m = mid
        else:
            lo_m = mid + 1
    best = lo_m

    # M 对应的盒约束 [lo, hi] 下的最早/最晚可行入点：
    # 可行域恰为 {early ≤ t ≤ late 且满足间距约束}
    lo = [max(0, s - best) for s in starts]
    hi = [s + best for s in starts]
    early = [0] * n
    early[0] = lo[0]
    for i in range(1, n):
        early[i] = max(lo[i], early[i - 1] + gaps[i - 1])
    late = [0] * n
    late[-1] = hi[-1]
    for i in range(n - 2, -1, -1):
        late[i] = min(hi[i], late[i + 1] - gaps[i])

    # 平移量相同（同为 M）的可行轴中按原入点就近：
    # t_i = u_i + p_i（p 为间距下限前缀和）化为带单调盒的 L1 保序回归
    prefix = [0] * n
    for i in range(1, n):
        prefix[i] = prefix[i - 1] + gaps[i - 1]
    r = [starts[i] - prefix[i] for i in range(n)]
    u = _bounded_l1_isotonic(
        r,
        [early[i] - prefix[i] for i in range(n)],
        [late[i] - prefix[i] for i in range(n)],
    )
    return [u[i] + prefix[i] for i in range(n)]


def avoid_subtitles(
    items: list[SrtItem], min_gap_ms: int, max_shift_ms: int
) -> list[AvoidRow]:
    """整体平移修整相邻块的重叠/过短间隔，保持每块时长、顺序与正文。

    时长非正的块、或平移上限内不存在可行时间轴时，
    抛带具体块号的 ValueError（调用方应拒绝导出）。
    """
    if min_gap_ms < 0:
        raise ValueError("最小块间隔不能为负")
    if max_shift_ms < 0:
        raise ValueError("单块最大平移量不能为负")
    if not items:
        return []

    labels: list[str] = []
    starts: list[int] = []
    durations: list[int] = []
    for pos, item in enumerate(items, start=1):
        label = _block_label(pos, item.index)
        if item.end_ms <= item.start_ms:
            raise ValueError(
                f"{label}入点不早于出点"
                f"（{item.start_tc} --> {item.end_tc}），"
                "时长非正，无法仅通过平移修整"
            )
        labels.append(label)
        starts.append(item.start_ms)
        durations.append(item.end_ms - item.start_ms)

    new_starts = _solve_starts(
        starts, durations, min_gap_ms, max_shift_ms, labels
    )
    rows: list[AvoidRow] = []
    for pos, (item, new_start) in enumerate(zip(items, new_starts), start=1):
        duration = item.end_ms - item.start_ms
        new_end = new_start + duration
        rows.append(
            AvoidRow(
                new_index=pos,
                index=item.index,
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


def build_avoid_srt(rows: list[AvoidRow]) -> str:
    """生成避让后的 SRT：保留字幕正文（含多行），从 1 重新编号。"""
    blocks = []
    for row in rows:
        lines = [str(row.new_index), f"{row.new_start_tc} --> {row.new_end_tc}"]
        lines.extend(row.body_lines or (row.text,))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
