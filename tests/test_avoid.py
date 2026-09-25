"""SRT 时间避让：避让算法单元测试（含暴力枚举对拍）+ 接口测试。

运行：python -m pytest tests/ -q
"""
import itertools
import random

import pytest
from fastapi.testclient import TestClient

from app.avoid import avoid_subtitles, build_avoid_srt
from app.main import app
from app.parsers import parse_srt

client = TestClient(app)


def _srt(spans, declared=None):
    """spans: [(start_ms, end_ms, text), ...]；declared 可指定声明序号。"""
    blocks = []
    for i, (s, e, text) in enumerate(spans, start=1):
        idx = declared[i - 1] if declared else i
        blocks.append(f"{idx}\n{_tc(s)} --> {_tc(e)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _tc(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _avoid(spans, gap=0, limit=2000, declared=None):
    return avoid_subtitles(parse_srt(_srt(spans, declared)), gap, limit)


# ---------- 基本避让行为 ----------

def test_no_change_when_already_feasible():
    rows = _avoid([(1000, 2500, "甲"), (3000, 4000, "乙")], gap=500)
    assert [r.shift_ms for r in rows] == [0, 0]
    assert rows[0].new_start_tc == "00:00:01,000"
    assert rows[1].new_start_tc == "00:00:03,000"


def test_overlap_split_symmetrically_not_greedy():
    # 逐块贪心会把第 2 块整体后推 500；整体最优是两块各让 250
    rows = _avoid([(1000, 3000, "甲"), (2500, 4000, "乙")], gap=0)
    assert [r.shift_ms for r in rows] == [-250, 250]
    assert rows[0].new_start_tc == "00:00:00,750"
    assert rows[1].new_start_tc == "00:00:02,750"
    # 时长不变
    assert rows[0].new_end_ms - rows[0].new_start_ms == 2000
    assert rows[1].new_end_ms - rows[1].new_start_ms == 1500


def test_min_gap_enforced():
    rows = _avoid([(1000, 2000, "甲"), (2100, 3000, "乙")], gap=500)
    # 间隔 100 < 500：第 1 块前移、第 2 块后移各 200，最大平移 200
    assert [r.shift_ms for r in rows] == [-200, 200]
    assert rows[1].new_start_ms - rows[0].new_end_ms == 500


def test_nonnegative_time_floor():
    # 第 1 块已在 0 附近，无法前移让位，只能后块独让
    rows = _avoid([(0, 200, "甲"), (100, 300, "乙")], gap=0)
    assert [r.shift_ms for r in rows] == [0, 100]
    assert rows[0].new_start_ms == 0


def test_global_beats_per_block_greedy():
    # 贪心：块2 被顶到 1200（平移 +50）；整体最优：块1 前移 25、块2 后移 25
    rows = _avoid(
        [(1000, 1200, "甲"), (1150, 1250, "乙"), (1300, 1500, "丙")], gap=0
    )
    assert [r.shift_ms for r in rows] == [-25, 25, 0]
    assert max(abs(r.shift_ms) for r in rows) == 25


def test_tie_break_prefers_original_starts():
    # 达到同一最小最大平移量的可行轴有多个时，远处的块应贴近原入点
    rows = _avoid(
        [(1000, 1500, "甲"), (1400, 1900, "乙"), (5000, 5500, "丙")], gap=200
    )
    assert [r.shift_ms for r in rows] == [-150, 150, 0]
    assert rows[2].new_start_ms == 5000


def test_order_duration_text_preserved():
    rows = _avoid(
        [(1000, 3000, "第一行\n第二行"), (2500, 4000, "乙"), (9000, 10000, "丙")],
        gap=100,
    )
    assert [r.text for r in rows] == ["第一行 第二行", "乙", "丙"]
    for r, (s, e, _) in zip(rows, [(1000, 3000, ""), (2500, 4000, ""), (9000, 10000, "")]):
        assert r.new_end_ms - r.new_start_ms == e - s
    # 顺序保持：入点严格递增且间隔满足
    assert rows[0].new_start_ms < rows[1].new_start_ms < rows[2].new_start_ms
    assert rows[1].new_start_ms - rows[0].new_end_ms >= 100


def test_build_avoid_srt_renumbers_and_keeps_body():
    rows = _avoid(
        [(1000, 2000, "第一行\n第二行"), (9000, 10000, "另一块")],
        declared=[5, 9],
    )
    out = build_avoid_srt(rows)
    blocks = out.strip().split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].splitlines() == [
        "1", "00:00:01,000 --> 00:00:02,000", "第一行", "第二行",
    ]
    assert blocks[1].splitlines() == [
        "2", "00:00:09,000 --> 00:00:10,000", "另一块",
    ]


# ---------- 不可行与非法输入 ----------

def test_infeasible_points_first_conflict_block():
    # 块2 最早只能排到 500，超出其允许最晚入点 100+100=200
    with pytest.raises(ValueError, match="第 2 块"):
        _avoid([(0, 500, "甲"), (100, 600, "乙")], gap=0, limit=100)


def test_infeasible_message_details():
    with pytest.raises(ValueError) as exc:
        _avoid([(0, 500, "甲"), (100, 600, "乙")], gap=0, limit=100)
    msg = str(exc.value)
    assert "00:00:00,500" in msg   # 最早可行入点
    assert "00:00:00,200" in msg   # 允许最晚入点
    assert "100 毫秒" in msg


def test_infeasible_label_uses_declared_index():
    with pytest.raises(ValueError, match="第 2 块（声明序号 7）"):
        _avoid([(0, 500, "甲"), (100, 600, "乙")], gap=0, limit=100,
               declared=[3, 7])


def test_gap_can_make_infeasible():
    # 间隔要求 400：块2 最早 900，上限只允许到 700
    with pytest.raises(ValueError, match="第 2 块"):
        _avoid([(0, 500, "甲"), (500, 1000, "乙")], gap=400, limit=200)


def test_nonpositive_duration_rejected():
    with pytest.raises(ValueError, match="第 1 块.*入点不早于出点"):
        _avoid([(1000, 1000, "零时长"), (3000, 4000, "乙")])
    with pytest.raises(ValueError, match="第 2 块.*入点不早于出点"):
        _avoid([(1000, 2000, "甲"), (3000, 2500, "倒排")])


def test_negative_params_rejected():
    with pytest.raises(ValueError, match="最小块间隔"):
        _avoid([(0, 100, "甲")], gap=-1)
    with pytest.raises(ValueError, match="单块最大平移量"):
        _avoid([(0, 100, "甲")], limit=-1)


# ---------- 暴力枚举对拍：全段最优性 ----------

def _brute_best(starts, durations, gap, limit):
    """枚举盒内全部整毫秒时间轴，返回最优 (最大平移, 平移总量)；不可行 None。"""
    n = len(starts)
    best = None

    def rec(i, prev, max_s, sum_s):
        nonlocal best
        if best is not None and (max_s, sum_s) >= best:
            return
        if i == n:
            best = (max_s, sum_s)
            return
        for t in range(max(0, starts[i] - limit), starts[i] + limit + 1):
            if i and t - prev < durations[i - 1] + gap:
                continue
            sh = abs(t - starts[i])
            rec(i + 1, t, max(max_s, sh), sum_s + sh)

    rec(0, 0, 0, 0)
    return best


def test_bruteforce_random_small_cases():
    rng = random.Random(20260925)
    compared = infeasible = 0
    for _ in range(400):
        n = rng.randint(1, 4)
        starts = [rng.randint(0, 40) for _ in range(n)]
        durations = [rng.randint(1, 15) for _ in range(n)]
        gap = rng.randint(0, 5)
        limit = rng.randint(0, 25)
        items = parse_srt(_srt([(s, s + d, f"t{i}") for i, (s, d) in
                                enumerate(zip(starts, durations))]))
        try:
            rows = avoid_subtitles(items, gap, limit)
        except ValueError:
            assert _brute_best(starts, durations, gap, limit) is None
            infeasible += 1
            continue
        got = [r.new_start_ms for r in rows]
        for i in range(n):
            assert 0 <= got[i]                          # 非负
            assert abs(got[i] - starts[i]) <= limit     # 平移上限
            if i:
                assert got[i] - got[i - 1] >= durations[i - 1] + gap
        shifts = [abs(got[i] - starts[i]) for i in range(n)]
        best = _brute_best(starts, durations, gap, limit)
        assert best is not None
        # 先比全段最大平移量，再比平移总量（按原入点就近）
        assert (max(shifts), sum(shifts)) == best
        compared += 1
    assert compared > 200 and infeasible > 0


# ---------- 接口 ----------

SRT_OVERLAP = _srt([
    (1000, 3000, "你好，世界！"),
    (2500, 4000, "这一块和上一块重叠了"),
    (10000, 12000, "正常的字幕块"),
])


def test_api_avoid_preview():
    resp = client.post(
        "/api/avoid",
        json={"srt": SRT_OVERLAP, "min_gap_ms": 0, "max_shift_ms": 2000},
    )
    assert resp.status_code == 200
    data = resp.json()
    s = data["summary"]
    assert s["total"] == 3
    assert s["min_gap_ms"] == 0
    assert s["max_shift_ms"] == 2000
    assert s["max_abs_shift_ms"] == 250
    assert s["moved"] == 2
    rows = data["rows"]
    assert [r["new_index"] for r in rows] == [1, 2, 3]
    assert rows[0]["old_start_tc"] == "00:00:01,000"
    assert rows[0]["new_start_tc"] == "00:00:00,750"
    assert rows[0]["shift_ms"] == -250
    assert rows[1]["shift_ms"] == 250
    assert rows[2]["shift_ms"] == 0
    assert rows[1]["text"] == "这一块和上一块重叠了"


def test_api_avoid_defaults():
    resp = client.post("/api/avoid", json={"srt": SRT_OVERLAP})
    assert resp.status_code == 200
    s = resp.json()["summary"]
    assert s["min_gap_ms"] == 0
    assert s["max_shift_ms"] == 2000


def test_api_avoid_download_srt():
    resp = client.post(
        "/api/avoid.srt",
        json={"srt": SRT_OVERLAP, "min_gap_ms": 0, "max_shift_ms": 2000},
    )
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    blocks = text.strip().split("\n\n")
    assert len(blocks) == 3
    assert blocks[0].splitlines()[:2] == [
        "1", "00:00:00,750 --> 00:00:02,750",
    ]
    assert blocks[1].splitlines()[:2] == [
        "2", "00:00:02,750 --> 00:00:04,250",
    ]
    assert "这一块和上一块重叠了" in text


def test_api_avoid_infeasible_400_first_conflict():
    srt = _srt([(0, 500, "甲"), (100, 600, "乙")])
    for path in ("/api/avoid", "/api/avoid.srt"):
        resp = client.post(
            path, json={"srt": srt, "min_gap_ms": 0, "max_shift_ms": 100}
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "第 2 块" in detail
        assert "单块最大平移量" in detail or "最小块间隔" in detail


def test_api_avoid_param_bounds():
    for bad in (-1, 60001):
        resp = client.post(
            "/api/avoid", json={"srt": SRT_OVERLAP, "min_gap_ms": bad}
        )
        assert resp.status_code in (400, 422)
    for bad in (-1, 3600001):
        resp = client.post(
            "/api/avoid", json={"srt": SRT_OVERLAP, "max_shift_ms": bad}
        )
        assert resp.status_code in (400, 422)


def test_api_avoid_empty_and_bad_srt():
    resp = client.post("/api/avoid", json={"srt": ""})
    assert resp.status_code == 400
    resp = client.post("/api/avoid", json={"srt": "没有时间码"})
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]


def test_api_avoid_zero_duration_400():
    srt = "1\n00:00:01,000 --> 00:00:01,000\n零时长\n"
    resp = client.post("/api/avoid", json={"srt": srt})
    assert resp.status_code == 400
    assert "第 1 块" in resp.json()["detail"]
