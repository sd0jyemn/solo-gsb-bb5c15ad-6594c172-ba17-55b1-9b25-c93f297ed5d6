"""全局单调对齐（Needleman-Wunsch 风格 DP）。

- 对角线代价：两条目归一化文本的归一化字符编辑距离（0~1）
- 跳过任一条目代价：固定 0.45（可由调用方覆盖）
- 两侧条目各最多匹配一次，允许缺项；结果关于序列顺序全局单调
- 支持人工锚点：锚点强制配对，相邻锚点之间仍按常规最低代价对齐

优先使用 rapidfuzz(C++) 批量计算距离矩阵 + numpy 向量化 DP；
缺少 numpy 时自动回退到纯 Python 实现。
"""
from __future__ import annotations

from .textnorm import normalized_distance

GAP_COST = 0.45
_EPS = 1e-9

Ops = list[tuple[str, float, int | None, int | None]]

# 方向标记：1=对角(匹配) 2=向上(a 缺项/跳过 b) 3=向左(b 缺项/跳过 a)
_DIAG, _UP, _LEFT = 1, 2, 3

try:
    import numpy as np
    from rapidfuzz.distance import Levenshtein
    from rapidfuzz.process import cdist

    def _distance_matrix(norms_a: list[str], norms_b: list[str]) -> "np.ndarray":
        # 取 C++ 计算的原始编辑距离矩阵，再按本项目定义
        # （距离 / max(len(a), len(b))）归一化，保证与 normalized_distance 同义。
        dist = cdist(
            norms_a,
            norms_b,
            scorer=Levenshtein.distance,
            workers=-1,
            dtype=np.int32,
        ).astype(np.float64)
        la = np.array([len(s) for s in norms_a], dtype=np.float64)[:, None]
        lb = np.array([len(s) for s in norms_b], dtype=np.float64)[None, :]
        denom = np.maximum(la, lb)
        out = np.divide(
            dist, denom, out=np.zeros_like(dist), where=denom > 0
        )
        # denom=0 只可能是两侧皆空串，距离 0 已由 out=0 覆盖；
        # 一空一非空时 denom>0 且距离等于非空串长度，结果恰为 1。
        return out

    def _align_numpy(
        norms_a: list[str],
        norms_b: list[str],
        gap_cost: float,
    ) -> Ops:
        n, m = len(norms_a), len(norms_b)
        sub = _distance_matrix(norms_a, norms_b)

        back = [bytearray(m + 1) for _ in range(n + 1)]
        prev = np.empty(m + 1, dtype=np.float64)
        prev[0] = 0.0
        prev[1:] = np.arange(1, m + 1, dtype=np.float64) * gap_cost
        back[0][1:] = b"\x03" * m

        for i in range(1, n + 1):
            cur = np.empty(m + 1, dtype=np.float64)
            cur[0] = prev[0] + gap_cost
            back[i][0] = _UP
            row = back[i]
            row_bytes = bytearray(m)

            # 向左候选为 cur[j-1]+g，逐格递推；j=1 时即 cur[0]+g。
            diag_row = prev[:-1] + sub[i - 1]
            up_row = prev[1:] + gap_cost
            left = cur[0] + gap_cost
            for j in range(1, m + 1):
                diag = float(diag_row[j - 1])
                up = float(up_row[j - 1])
                # 必须精确比较：DP 的向左值逐格递推，任何“近似平局”
                # 容差都会沿行传播、破坏最优子结构。
                if diag <= up and diag <= left:
                    cur[j] = diag
                    row_bytes[j - 1] = _DIAG
                elif up <= left:
                    cur[j] = up
                    row_bytes[j - 1] = _UP
                else:
                    cur[j] = left
                    row_bytes[j - 1] = _LEFT
                left = cur[j] + gap_cost
            row[1:] = bytes(row_bytes)
            prev = cur

        ops: Ops = []
        i, j = n, m
        while i > 0 or j > 0:
            d = back[i][j]
            if d == _DIAG:
                cost = float(sub[i - 1, j - 1])
                ops.append(("match", cost, i - 1, j - 1))
                i -= 1
                j -= 1
            elif d == _UP:
                ops.append(("gap_a", gap_cost, i - 1, None))
                i -= 1
            else:
                ops.append(("gap_b", gap_cost, None, j - 1))
                j -= 1
        ops.reverse()
        return ops

    _HAVE_NUMPY = True
except ImportError:  # pragma: no cover - 取决于运行环境
    _HAVE_NUMPY = False


def _align_python(
    norms_a: list[str],
    norms_b: list[str],
    gap_cost: float,
) -> Ops:
    n, m = len(norms_a), len(norms_b)
    back: list[bytearray] = [bytearray(m + 1) for _ in range(n + 1)]
    len_b = [len(s) for s in norms_b]
    # 配对代价 c >= 长度差下界。若下界（严格）大于 2g，则“两侧各跳过”
    # （合计 2g）支配该配对，它不可能出现在最优对齐中，精确距离可省。
    dominate = 1.0 if gap_cost >= 0.5 else 2.0 * gap_cost

    prev = [0.0] * (m + 1)
    for j in range(1, m + 1):
        prev[j] = prev[j - 1] + gap_cost
        back[0][j] = _LEFT
    for i in range(1, n + 1):
        cur = [prev[0] + gap_cost] + [0.0] * m
        back[i][0] = _UP
        ai = norms_a[i - 1]
        la = len(ai)
        row = back[i]
        for j in range(1, m + 1):
            bj = norms_b[j - 1]
            if ai == bj:
                cost = 0.0
            elif la == 0 and len_b[j - 1] == 0:
                cost = 0.0  # 两侧归一化后皆空（仅标点/空白行）视为相同
            else:
                lb = len_b[j - 1]
                lower = abs(la - lb) / max(la, lb) if la and lb else 1.0
                cost = 1.0 if lower > dominate + _EPS else normalized_distance(ai, bj)
            diag = prev[j - 1] + cost
            up = prev[j] + gap_cost
            left = cur[j - 1] + gap_cost
            if diag <= up and diag <= left:
                cur[j] = diag
                row[j] = _DIAG
            elif up <= left:
                cur[j] = up
                row[j] = _UP
            else:
                cur[j] = left
                row[j] = _LEFT
        prev = cur

    ops: Ops = []
    i, j = n, m
    while i > 0 or j > 0:
        d = back[i][j]
        if d == _DIAG:
            cost = normalized_distance(norms_a[i - 1], norms_b[j - 1])
            ops.append(("match", cost, i - 1, j - 1))
            i -= 1
            j -= 1
        elif d == _UP:
            ops.append(("gap_a", gap_cost, i - 1, None))
            i -= 1
        else:
            ops.append(("gap_b", gap_cost, None, j - 1))
            j -= 1
    ops.reverse()
    return ops


def align(
    norms_a: list[str],
    norms_b: list[str],
    gap_cost: float = GAP_COST,
) -> Ops:
    """对两组已归一化文本做最低代价全局单调对齐。

    返回按 a/b 顺序展开的操作列表，每项为 (op, cost, i, j)：
    ("match", 代价, i, j)、("gap_a", gap_cost, i, None)、
    ("gap_b", gap_cost, None, j)，索引均从 0 开始。
    """
    if not norms_a:
        return [("gap_b", gap_cost, None, j) for j in range(len(norms_b))]
    if not norms_b:
        return [("gap_a", gap_cost, i, None) for i in range(len(norms_a))]
    if _HAVE_NUMPY:
        return _align_numpy(norms_a, norms_b, gap_cost)
    return _align_python(norms_a, norms_b, gap_cost)


def normalize_anchors(
    anchors: list[tuple[int, int]],
    n: int,
    m: int,
) -> list[tuple[int, int]]:
    """校验人工锚点并把 1 起始序位转换为 0 起始索引对。

    anchors 为 (对白稿行序位, SRT 块序位)，序位均指解析后条目的顺序
    位置（1..n / 1..m）。要求两列均严格递增；越界、重复或交叉
    （顺序颠倒）时抛出 ValueError，由接口层转成 400。
    """
    out: list[tuple[int, int]] = []
    prev_a = prev_b = 0
    for k, (a, b) in enumerate(anchors, 1):
        if not 1 <= a <= n:
            raise ValueError(
                f"第 {k} 个锚点的对白稿行序位 {a} 超出范围 1~{n}"
            )
        if not 1 <= b <= m:
            raise ValueError(
                f"第 {k} 个锚点的 SRT 块序位 {b} 超出范围 1~{m}"
            )
        if a == prev_a and b == prev_b:
            raise ValueError(f"第 {k} 个锚点 ({a}:{b}) 与前一锚点重复")
        if a <= prev_a or b <= prev_b:
            raise ValueError(
                f"第 {k} 个锚点 ({a}:{b}) 与前一锚点 ({prev_a}:{prev_b}) "
                "交叉：两列序位必须同时严格递增"
            )
        prev_a, prev_b = a, b
        out.append((a - 1, b - 1))
    return out


def align_with_anchors(
    norms_a: list[str],
    norms_b: list[str],
    anchors: list[tuple[int, int]],
    gap_cost: float = GAP_COST,
) -> Ops:
    """带人工锚点的分段对齐。

    anchors 为 0 起始、两列均严格递增的 (i, j) 索引对（先经
    normalize_anchors 校验转换）。相邻锚点之间（含首锚点之前、
    末锚点之后）的片段各自做常规最低代价单调对齐，锚点本身作为
    强制配对（op 为 "anchor"）按顺序拼接——即使两侧文本差异很大
    也绝不跳过；其余条目仍可能表现为双侧缺项。
    """
    ops: Ops = []
    ai = bj = 0
    for i, j in anchors:
        ops.extend(_offset_ops(align(norms_a[ai:i], norms_b[bj:j], gap_cost), ai, bj))
        ops.append(
            ("anchor", normalized_distance(norms_a[i], norms_b[j]), i, j)
        )
        ai, bj = i + 1, j + 1
    ops.extend(_offset_ops(align(norms_a[ai:], norms_b[bj:], gap_cost), ai, bj))
    return ops


def _offset_ops(ops: Ops, di: int, dj: int) -> Ops:
    """把分段对齐结果中的段内索引平移为全局索引。"""
    return [
        (
            op,
            cost,
            i + di if i is not None else None,
            j + dj if j is not None else None,
        )
        for op, cost, i, j in ops
    ]
