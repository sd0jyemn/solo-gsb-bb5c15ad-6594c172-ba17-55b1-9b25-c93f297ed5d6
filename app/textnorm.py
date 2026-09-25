"""文本归一化与归一化字符编辑距离。"""
from __future__ import annotations

import unicodedata

try:
    # rapidfuzz 的 Levenshtein.distance 与经典单字符插入/删除/替换代价一致
    from rapidfuzz.distance.Levenshtein import distance as _levenshtein

    def _edit_distance(a: str, b: str) -> int:
        return int(_levenshtein(a, b))

except ImportError:  # 纯 Python 回退，保证缺少扩展依赖时仍可运行
    def _edit_distance(a: str, b: str) -> int:
        if a == b:
            return 0
        la, lb = len(a), len(b)
        if la == 0:
            return lb
        if lb == 0:
            return la
        prev = list(range(lb + 1))
        for i, ca in enumerate(a, 1):
            cur = [i] + [0] * lb
            for j, cb in enumerate(b, 1):
                cost = 0 if ca == cb else 1
                cur[j] = min(
                    prev[j] + 1,        # 删除
                    cur[j - 1] + 1,     # 插入
                    prev[j - 1] + cost, # 替换/匹配
                )
            prev = cur
        return prev[lb]


def normalize_text(text: str) -> str:
    """去除空白与标点，得到用于比对的归一化字符串。

    处理步骤：NFKC 兼容分解（全角转半角等）→ 小写折叠 →
    逐字符过滤掉 Unicode 类别以 C（控制符）、P（标点）、Z（分隔/空白）
    开头的字符。中文/日文等文字字符原样保留。
    """
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for ch in text.casefold():
        cat = unicodedata.category(ch)
        if cat[0] in ("C", "P", "Z"):
            continue
        out.append(ch)
    return "".join(out)


def normalized_distance(a_norm: str, b_norm: str) -> float:
    """以归一化字符编辑距离（编辑距离 / 较长串长度）作为匹配代价，范围 [0, 1]。

    入参应为已经过 normalize_text 归一化的字符串。结果精确，不做近似
    剪枝，以保证全局对齐取到真正的最低代价。
    """
    if a_norm == b_norm:
        return 0.0
    la, lb = len(a_norm), len(b_norm)
    if la == 0 or lb == 0:
        return 1.0
    return _edit_distance(a_norm, b_norm) / max(la, lb)
