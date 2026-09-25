"""SRT 时间轴重定时：映射算法单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.retime import (
    CalibPoint,
    build_srt,
    map_time,
    parse_timecode,
    retime_subtitles,
    validate_points,
)

client = TestClient(app)


def _pt(source_ms, target_ms):
    return CalibPoint(source_ms=source_ms, target_ms=target_ms)


# ---------- 时间码解析 ----------

def test_parse_timecode():
    assert parse_timecode("00:00:01,000") == 1000
    assert parse_timecode("00:00:01") == 1000
    assert parse_timecode("01:02:03.5") == 3600000 + 120000 + 3000 + 500
    assert parse_timecode(" 00:00:10,05 ") == 10050  # 两位毫秒按 050 处理
    assert parse_timecode("2:00:00,000") == 7200000


def test_parse_timecode_invalid():
    for bad in ("", "abc", "00:00", "00:00:61,000", "00:60:00,000", "1:2:3:4"):
        with pytest.raises(ValueError):
            parse_timecode(bad)


# ---------- 校准点校验 ----------

def test_points_must_be_strictly_increasing():
    validate_points([_pt(1000, 2000)])  # 单点合法
    validate_points([_pt(1000, 2000), _pt(3000, 4000)])
    # 原时间列不递增
    with pytest.raises(ValueError, match="严格递增"):
        validate_points([_pt(1000, 2000), _pt(1000, 3000)])
    with pytest.raises(ValueError, match="严格递增"):
        validate_points([_pt(3000, 2000), _pt(1000, 3000)])
    # 目标时间列不递增
    with pytest.raises(ValueError, match="严格递增"):
        validate_points([_pt(1000, 2000), _pt(3000, 2000)])
    with pytest.raises(ValueError, match="严格递增"):
        validate_points([_pt(1000, 5000), _pt(3000, 4000)])
    with pytest.raises(ValueError, match="至少需要一个"):
        validate_points([])


# ---------- 分段线性映射 ----------

def test_map_single_point_shifts_everything():
    points = [_pt(1000, 2500)]  # 整体后移 1500ms
    assert map_time(0, points) == 1500
    assert map_time(1000, points) == 2500
    assert map_time(4000, points) == 5500


def test_map_interpolates_between_points():
    points = [_pt(1000, 2000), _pt(7000, 10500)]  # 斜率 8500/6000 = 17/12
    assert map_time(1000, points) == 2000    # 校准点精确命中
    assert map_time(7000, points) == 10500
    assert map_time(4000, points) == 6250    # 2000 + 3000 * 17/12


def test_map_extrapolates_with_nearest_segment_slope():
    points = [_pt(1000, 2000), _pt(7000, 10500)]  # 斜率 17/12
    # 首区段外推：2000 - 1000 * 17/12 = 583.33 → 583
    assert map_time(0, points) == 583
    # 末区段外推：10500 + 1000 * 17/12 = 11916.67 → 11917
    assert map_time(8000, points) == 11917


def test_map_uses_inner_segments():
    # 三段：斜率分别为 1、2、0.5
    points = [_pt(0, 0), _pt(1000, 1000), _pt(2000, 3000), _pt(4000, 4000)]
    assert map_time(500, points) == 500
    assert map_time(1500, points) == 2000
    assert map_time(3000, points) == 3500
    assert map_time(2000, points) == 3000  # 校准点精确命中


def test_map_rounds_to_ms():
    points = [_pt(0, 0), _pt(3, 1)]  # 斜率 1/3
    assert map_time(1, points) == 0          # 0.333 → 0
    assert map_time(2, points) == 1          # 0.667 → 1
    half = [_pt(0, 0), _pt(2, 1)]            # 斜率 1/2
    assert map_time(1, half) == 1            # 0.5 → 1（四舍五入）


# ---------- 重定时校验（拒绝导出场景） ----------

SRT_BASIC = "\n".join(
    [
        "1",
        "00:00:01,000 --> 00:00:02,500",
        "你好，世界！",
        "",
        "2",
        "00:00:03,000 --> 00:00:06,000",
        "第二句字幕",
        "",
        "3",
        "00:00:07,000 --> 00:00:09,000",
        "第三句字幕",
        "",
    ]
)


def test_retime_basic_rows():
    items = parse_srt(SRT_BASIC)
    rows = retime_subtitles(items, [_pt(1000, 2000), _pt(7000, 10500)])
    assert [r.new_index for r in rows] == [1, 2, 3]
    assert rows[0].new_start_tc == "00:00:02,000"
    assert rows[0].new_end_tc == "00:00:04,125"   # 2000 + 1500*17/12
    assert rows[2].new_start_tc == "00:00:10,500"
    # 末区段外推：10500 + 2000*17/12 = 13333.33 → 13333
    assert rows[2].new_end_tc == "00:00:13,333"
    assert rows[0].old_start_tc == "00:00:01,000"
    assert rows[0].text == "你好，世界！"


def test_retime_negative_time_rejected():
    items = parse_srt(SRT_BASIC)
    # 首区段斜率 0.2，块 1 入点 1000ms 外推为 500 - 4000*0.2 = -300ms
    with pytest.raises(ValueError, match="第 1 块.*负时间"):
        retime_subtitles(items, [_pt(5000, 500), _pt(10000, 1500)])


def test_retime_in_not_before_out_rejected():
    # 斜率 1/3 把 1ms 时长的块压成入点 == 出点
    srt = "1\n00:00:00,000 --> 00:00:00,001\n一闪而过\n"
    items = parse_srt(srt)
    with pytest.raises(ValueError, match="第 1 块.*入点不早于出点"):
        retime_subtitles(items, [_pt(0, 0), _pt(3, 1)])


def test_retime_out_of_order_starts_rejected():
    srt = "\n".join(
        [
            "1",
            "00:00:05,000 --> 00:00:06,000",
            "先出现的晚字幕",
            "",
            "2",
            "00:00:02,000 --> 00:00:03,000",
            "后出现的早字幕",
            "",
        ]
    )
    items = parse_srt(srt)
    with pytest.raises(ValueError, match="第 2 块.*顺序倒退"):
        retime_subtitles(items, [_pt(0, 1000)])


def test_build_srt_renumbers_and_keeps_text():
    srt = "\n".join(
        [
            "5",
            "00:00:01,000 --> 00:00:02,000",
            "第一行",
            "第二行",
            "",
            "9",
            "00:00:03,000 --> 00:00:04,000",
            "另一块",
            "",
        ]
    )
    items = parse_srt(srt)
    rows = retime_subtitles(items, [_pt(0, 0)])
    out = build_srt(rows)
    blocks = out.strip().split("\n\n")
    assert len(blocks) == 2
    # 重新编号为 1、2，正文多行原样保留
    assert blocks[0].splitlines() == [
        "1", "00:00:01,000 --> 00:00:02,000", "第一行", "第二行",
    ]
    assert blocks[1].splitlines() == [
        "2", "00:00:03,000 --> 00:00:04,000", "另一块",
    ]


# ---------- 接口 ----------

POINTS = [
    {"source": "00:00:01,000", "target": "00:00:02,000"},
    {"source": "00:00:07,000", "target": "00:00:10,500"},
]


def test_api_retime_preview():
    resp = client.post("/api/retime", json={"srt": SRT_BASIC, "points": POINTS})
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"] == {"total": 3, "points": 2}
    rows = data["rows"]
    assert [r["new_index"] for r in rows] == [1, 2, 3]
    assert rows[0]["old_start_tc"] == "00:00:01,000"
    assert rows[0]["new_start_tc"] == "00:00:02,000"
    assert rows[0]["new_end_tc"] == "00:00:04,125"
    assert rows[0]["text"] == "你好，世界！"


def test_api_retime_single_point_shift():
    resp = client.post(
        "/api/retime",
        json={
            "srt": SRT_BASIC,
            "points": [{"source": "00:00:01,000", "target": "00:00:02,500"}],
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert rows[0]["new_start_tc"] == "00:00:02,500"  # 整体后移 1500ms
    assert rows[1]["new_start_tc"] == "00:00:04,500"


def test_api_retime_srt_download():
    resp = client.post("/api/retime.srt", json={"srt": SRT_BASIC, "points": POINTS})
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    blocks = text.strip().split("\n\n")
    assert len(blocks) == 3
    assert blocks[0].splitlines()[:2] == ["1", "00:00:02,000 --> 00:00:04,125"]
    assert blocks[2].splitlines()[:2] == ["3", "00:00:10,500 --> 00:00:13,333"]
    assert "第二句字幕" in text


def test_api_retime_points_not_increasing_400():
    resp = client.post(
        "/api/retime",
        json={
            "srt": SRT_BASIC,
            "points": [
                {"source": "00:00:05,000", "target": "00:00:06,000"},
                {"source": "00:00:02,000", "target": "00:00:07,000"},
            ],
        },
    )
    assert resp.status_code == 400
    assert "严格递增" in resp.json()["detail"]
    # 目标时间列不递增同样拒绝
    resp = client.post(
        "/api/retime",
        json={
            "srt": SRT_BASIC,
            "points": [
                {"source": "00:00:01,000", "target": "00:00:08,000"},
                {"source": "00:00:07,000", "target": "00:00:06,000"},
            ],
        },
    )
    assert resp.status_code == 400
    assert "严格递增" in resp.json()["detail"]


def test_api_retime_bad_timecode_400():
    resp = client.post(
        "/api/retime",
        json={"srt": SRT_BASIC, "points": [{"source": "abc", "target": "00:00:01,000"}]},
    )
    assert resp.status_code == 400
    assert "第 1 个校准点" in resp.json()["detail"]


def test_api_retime_missing_points_400():
    resp = client.post("/api/retime", json={"srt": SRT_BASIC})
    assert resp.status_code == 400
    assert "校准点" in resp.json()["detail"]


def test_api_retime_invalid_result_400_with_block_number():
    # 映射后产生负时间：拒绝导出并指出块号
    resp = client.post(
        "/api/retime.srt",
        json={
            "srt": SRT_BASIC,
            "points": [
                {"source": "00:00:05,000", "target": "00:00:00,500"},
                {"source": "00:00:10,000", "target": "00:00:01,500"},
            ],
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "第 1 块" in detail and "负时间" in detail


def test_api_retime_empty_srt_400():
    resp = client.post(
        "/api/retime",
        json={"srt": "", "points": [{"source": "00:00:01,000", "target": "00:00:02,000"}]},
    )
    assert resp.status_code == 400
