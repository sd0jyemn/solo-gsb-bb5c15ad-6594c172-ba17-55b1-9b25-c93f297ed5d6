"""SRT 时间避让：minimax 平移算法单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.shift import build_shift_srt, shift_subtitles

client = TestClient(app)


def _srt(blocks):
    """blocks: [(start_ms, end_ms, text), ...]，序号按顺序声明。"""
    from app.parsers import format_tc

    out = []
    for i, (start, end, text) in enumerate(blocks, start=1):
        out.append(f"{i}\n{format_tc(start)} --> {format_tc(end)}\n{text}")
    return "\n\n".join(out) + "\n"


def _starts(rows):
    return [r.new_start_ms for r in rows]


def _shifts(rows):
    return [r.shift_ms for r in rows]


# ---------- 无需避让 ----------

def test_no_conflict_keeps_everything():
    items = parse_srt(_srt([(1000, 3000, "甲"), (4000, 6000, "乙")]))
    rows = shift_subtitles(items, min_gap_ms=200, max_shift_ms=1000)
    assert _starts(rows) == [1000, 4000]
    assert _shifts(rows) == [0, 0]


# ---------- minimax：不把冲突推给后面的块 ----------

def test_minimax_spreads_shift_instead_of_pushing_back():
    # 三块各 1s、入点间隔 100ms 的重叠链。
    # 逐块最早时间贪心得 [5000, 6000, 7000]，最大平移 1800 全压给末块；
    # minimax 把平移分摊为 [-900, 0, +900]，最大平移 900。
    items = parse_srt(
        _srt([(5000, 6000, "一"), (5100, 6100, "二"), (5200, 6200, "三")])
    )
    rows = shift_subtitles(items, min_gap_ms=0, max_shift_ms=3_600_000)
    assert _starts(rows) == [4100, 5100, 6100]
    assert _shifts(rows) == [-900, 0, 900]
    assert max(abs(s) for s in _shifts(rows)) == 900


def test_gap_enforced_and_split_both_ways():
    # 原重叠 1000ms：前块前移 1000、后块后移 1000，间隔恰好撑到 1000ms
    items = parse_srt(_srt([(10000, 13000, "甲"), (12000, 14000, "乙")]))
    rows = shift_subtitles(items, min_gap_ms=1000, max_shift_ms=5000)
    assert _starts(rows) == [9000, 13000]
    assert _shifts(rows) == [-1000, 1000]
    assert rows[1].new_start_ms - rows[0].new_end_ms == 1000


def test_non_negative_time_floor():
    # 首块入点为 0：不能被推到负时间，链只能向后排
    items = parse_srt(_srt([(0, 1000, "甲"), (100, 1100, "乙")]))
    rows = shift_subtitles(items, min_gap_ms=0, max_shift_ms=2000)
    assert _starts(rows) == [0, 1000]
    assert all(r.new_start_ms >= 0 for r in rows)


# ---------- 平移量相同时按原入点就近 ----------

def test_tie_break_keeps_far_block_untouched():
    # 避让只涉及前两块：第三块离得远，最早时间表会把它无谓前移，
    # 就近选择应让它留在原入点（平移量相同的可行轴中取贴近原入点的）
    items = parse_srt(
        _srt([(0, 2000, "一"), (5000, 7000, "二"), (20000, 22000, "三")])
    )
    rows = shift_subtitles(items, min_gap_ms=5000, max_shift_ms=60_000)
    assert _starts(rows) == [0, 7000, 20000]
    assert _shifts(rows) == [0, 2000, 0]


def test_unaffected_middle_block_not_moved():
    # 中间块本就满足间隔，不应被牵连平移
    items = parse_srt(
        _srt(
            [
                (5000, 6000, "一"),
                (6100, 7200, "二"),
                (7300, 8500, "三"),
                (20000, 22000, "四"),
            ]
        )
    )
    rows = shift_subtitles(items, min_gap_ms=200, max_shift_ms=1000)
    assert _starts(rows) == [4900, 6100, 7400, 20000]
    assert _shifts(rows) == [-100, 0, 100, 0]


# ---------- 保持时长 / 顺序 / 正文 ----------

def test_duration_order_text_preserved():
    items = parse_srt(
        _srt([(5000, 6500, "第一句"), (6000, 7200, "第二句"), (9000, 10500, "第三句")])
    )
    rows = shift_subtitles(items, min_gap_ms=300, max_shift_ms=5000)
    for row, item in zip(rows, items):
        assert row.new_end_ms - row.new_start_ms == item.end_ms - item.start_ms
        assert row.text == item.raw_text
        assert row.index == item.index
    starts = _starts(rows)
    assert starts == sorted(starts)
    # 相邻间隔均达到最小块间隔
    for prev, cur in zip(rows, rows[1:]):
        assert cur.new_start_ms - prev.new_end_ms >= 300


# ---------- 冲突与非法输入 ----------

def test_infeasible_reports_first_conflict_block():
    # 平移上限 100ms 远不够排开 1s 时长的两块
    items = parse_srt(_srt([(0, 1000, "甲"), (100, 1100, "乙")]))
    with pytest.raises(ValueError, match="第 2 块"):
        shift_subtitles(items, min_gap_ms=0, max_shift_ms=100)


def test_infeasible_points_to_later_block():
    # 前两块放得下，冲突在第三块
    items = parse_srt(
        _srt([(0, 1000, "一"), (5000, 6000, "二"), (5100, 6100, "三")])
    )
    with pytest.raises(ValueError, match="第 3 块"):
        shift_subtitles(items, min_gap_ms=0, max_shift_ms=50)


def test_zero_or_negative_duration_rejected():
    items = parse_srt(_srt([(1000, 1000, "零时长")]))
    with pytest.raises(ValueError, match="入点不早于出点"):
        shift_subtitles(items, min_gap_ms=0, max_shift_ms=1000)


def test_negative_params_rejected():
    items = parse_srt(_srt([(1000, 2000, "甲")]))
    with pytest.raises(ValueError):
        shift_subtitles(items, min_gap_ms=-1, max_shift_ms=1000)
    with pytest.raises(ValueError):
        shift_subtitles(items, min_gap_ms=0, max_shift_ms=-1)


# ---------- 导出 ----------

def test_build_shift_srt_keeps_index_and_body():
    srt = "\n".join(
        [
            "5",
            "00:00:05,000 --> 00:00:06,000",
            "第一行",
            "第二行",
            "",
            "9",
            "00:00:05,500 --> 00:00:07,000",
            "另一块",
            "",
        ]
    )
    items = parse_srt(srt)
    rows = shift_subtitles(items, min_gap_ms=200, max_shift_ms=5000)
    out = build_shift_srt(rows)
    blocks = out.strip().split("\n\n")
    assert len(blocks) == 2
    # 保留原声明序号 5、9 与多行正文，仅时间码变化；
    # 重叠 700ms 由 minimax 分摊：前块前移 350、后块后移 350
    assert blocks[0].splitlines() == [
        "5", "00:00:04,650 --> 00:00:05,650", "第一行", "第二行",
    ]
    assert blocks[1].splitlines() == [
        "9", "00:00:05,850 --> 00:00:07,350", "另一块",
    ]


# ---------- 接口 ----------

SRT_OVERLAP = _srt(
    [
        (5000, 6000, "第一句字幕"),
        (6100, 7200, "第二句字幕"),
        (7300, 8500, "第三句字幕"),
        (20000, 22000, "离得很远的一句"),
    ]
)


def test_api_shift_preview():
    resp = client.post(
        "/api/shift",
        json={"srt": SRT_OVERLAP, "min_gap_ms": 200, "max_shift_ms": 1000},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"] == {
        "total": 4,
        "min_gap_ms": 200,
        "max_shift_ms": 1000,
        "max_applied_shift_ms": 100,
        "shifted": 2,
    }
    rows = data["rows"]
    assert [r["index"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["old_start_tc"] == "00:00:05,000"
    assert rows[0]["new_start_tc"] == "00:00:04,900"
    assert rows[0]["shift_ms"] == -100
    assert rows[2]["shift_ms"] == 100
    assert rows[3]["shift_ms"] == 0  # 远处块不动
    assert rows[0]["text"] == "第一句字幕"


def test_api_shift_srt_download():
    resp = client.post(
        "/api/shift.srt",
        json={"srt": SRT_OVERLAP, "min_gap_ms": 200, "max_shift_ms": 1000},
    )
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    blocks = text.strip().split("\n\n")
    assert len(blocks) == 4
    assert blocks[0].splitlines()[:2] == ["1", "00:00:04,900 --> 00:00:05,900"]
    assert blocks[2].splitlines()[:2] == ["3", "00:00:07,400 --> 00:00:08,600"]
    assert blocks[3].splitlines()[:2] == ["4", "00:00:20,000 --> 00:00:22,000"]
    assert "离得很远的一句" in text


def test_api_shift_conflict_400_with_block_number():
    resp = client.post(
        "/api/shift",
        json={"srt": SRT_OVERLAP, "min_gap_ms": 200, "max_shift_ms": 10},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "第 2 块" in detail and "平移上限" in detail
    # 导出接口同样拒绝
    resp = client.post(
        "/api/shift.srt",
        json={"srt": SRT_OVERLAP, "min_gap_ms": 200, "max_shift_ms": 10},
    )
    assert resp.status_code == 400
    assert "第 2 块" in resp.json()["detail"]


def test_api_shift_bad_params_400():
    for bad in (
        {"min_gap_ms": -1},
        {"min_gap_ms": 60_001},
        {"max_shift_ms": -1},
        {"max_shift_ms": 3_600_001},
    ):
        resp = client.post("/api/shift", json={"srt": SRT_OVERLAP, **bad})
        assert resp.status_code in (400, 422)


def test_api_shift_empty_srt_400():
    resp = client.post(
        "/api/shift", json={"srt": "", "min_gap_ms": 200, "max_shift_ms": 1000}
    )
    assert resp.status_code == 400


def test_api_shift_bad_srt_400():
    resp = client.post(
        "/api/shift",
        json={"srt": "没有时间码", "min_gap_ms": 200, "max_shift_ms": 1000},
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]
