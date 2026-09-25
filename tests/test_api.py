"""FastAPI 接口层测试。需要 fastapi 的 TestClient（自带 httpx/starlette）。

运行：python -m pytest tests/ -q
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SCRIPT = "\n".join(
    [
        "你好，世界！",
        "今天我们来聊聊字幕校对。",
        "这一句只出现在对白稿里。",
        "编辑距离会忽略标点和空格。",
        "再见！",
        "再见！",
    ]
)

SRT = "\n".join(
    [
        "1",
        "00:00:01,000 --> 00:00:02,500",
        "你好世界",
        "",
        "2",
        "00:00:03,000 --> 00:00:06,000",
        "今天我们来聊聊字幕校对",
        "",
        "3",
        "00:00:07,000 --> 00:00:10,000",
        "编辑距离会忽略标点和空格",
        "",
        "4",
        "00:00:11,200 --> 00:00:12,800",
        "这是一条多余的字幕",
        "",
        "5",
        "00:00:13,000 --> 00:00:14,000",
        "再见",
        "",
        "6",
        "00:00:14,500 --> 00:00:15,500",
        "再见",
        "",
    ]
)


def _align():
    resp = client.post("/api/align", json={"script": SCRIPT, "srt": SRT})
    assert resp.status_code == 200
    return resp.json()


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_index_and_static():
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_align_counts_and_order():
    data = _align()
    s = data["summary"]
    assert s["match"] == 5
    assert s["missing_script"] == 1
    assert s["missing_srt"] == 1
    assert s["total_rows"] == 7
    assert s["total_cost"] == 0.9

    statuses = [r["status"] for r in data["rows"]]
    # 缺项应落在正确的中间位置，整体顺序单调
    assert statuses.index("missing_srt") == 2
    assert statuses.index("missing_script") == 4

    # 两条“再见”按顺序分别对上 SRT 5、6
    bye = [r for r in data["rows"] if r["srt_text"] == "再见"]
    assert [r["script_index"] for r in bye] == [5, 6]
    assert [r["srt_index"] for r in bye] == [5, 6]

    # 时间码保留
    first = data["rows"][0]
    assert first["start_tc"] == "00:00:01,000"
    assert first["end_tc"] == "00:00:02,500"


def test_each_item_matched_at_most_once():
    data = _align()
    si = [r["script_index"] for r in data["rows"] if r["status"] == "match"]
    ti = [r["srt_index"] for r in data["rows"] if r["status"] == "match"]
    assert len(si) == len(set(si))
    assert len(ti) == len(set(ti))


def test_punctuation_ignored():
    resp = client.post(
        "/api/align",
        json={"script": "你好，世界！\n", "srt": "1\n00:00:01,000 --> 00:00:02,000\n你好世界\n"},
    )
    row = resp.json()["rows"][0]
    assert row["status"] == "match"
    assert row["cost"] == 0.0


def test_custom_gap_cost():
    resp = client.post(
        "/api/align",
        json={"script": "甲甲甲\n", "srt": "1\n00:00:01,000 --> 00:00:02,000\n乙乙乙\n", "gap_cost": 0.2},
    )
    data = resp.json()
    # 匹配代价 1.0 > 2*0.2=0.4，应各跳过
    assert {r["status"] for r in data["rows"]} == {"missing_script", "missing_srt"}


def test_bad_srt_returns_400():
    resp = client.post("/api/align", json={"script": "你好", "srt": "没有时间码"})
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]


def test_empty_both_returns_400():
    resp = client.post("/api/align", json={"script": "  \n", "srt": ""})
    assert resp.status_code == 400


def test_csv_export():
    resp = client.post("/api/export.csv", json={"script": SCRIPT, "srt": SRT})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    raw = resp.content
    assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    assert "状态,代价,对白稿行号" in text
    assert "对位" in text and "缺字幕" in text and "缺对白稿" in text
    assert "00:00:01,000" in text


# ---------- 人工锚点 ----------

ANCHOR_SCRIPT = "aaaaaaaaaa\n第二句\n第三句"
ANCHOR_SRT = "\n".join(
    [
        "1",
        "00:00:01,000 --> 00:00:02,000",
        "bbbbbbbbbb",
        "",
        "2",
        "00:00:03,000 --> 00:00:04,000",
        "第二句",
        "",
        "3",
        "00:00:05,000 --> 00:00:06,000",
        "第三句",
        "",
    ]
)


def _align_with_anchors(anchors):
    return client.post(
        "/api/align",
        json={"script": ANCHOR_SCRIPT, "srt": ANCHOR_SRT, "anchors": anchors},
    )


def test_anchor_forces_match_despite_distance():
    # 第一条两侧文本完全不同（距离 1.0 > 2*gap），锚点仍强制对位
    resp = _align_with_anchors([{"script": 1, "srt": 1}])
    assert resp.status_code == 200
    data = resp.json()
    first = data["rows"][0]
    assert first["status"] == "match"
    assert first["anchor"] is True
    assert first["cost"] == 1.0
    assert first["script_index"] == 1 and first["srt_index"] == 1
    # 其余行照常对齐，非锚点行 anchor 为 False，且索引为全局序位
    rest = data["rows"][1:]
    assert all(r["anchor"] is False for r in rest)
    assert {r["status"] for r in rest} == {"match"}
    assert [r["script_index"] for r in rest] == [2, 3]
    assert [r["srt_index"] for r in rest] == [2, 3]
    assert data["summary"]["anchors"] == 1


def test_anchor_segments_keep_gaps():
    # 锚点只强制自身配对，其余条目仍可出现双侧缺项
    script = "稿里独有\n锚点句\n第四句"
    srt = "\n".join(
        [
            "1",
            "00:00:01,000 --> 00:00:02,000",
            "差异很大",
            "",
            "2",
            "00:00:03,000 --> 00:00:04,000",
            "字幕独有",
            "",
            "3",
            "00:00:05,000 --> 00:00:06,000",
            "第四句",
            "",
        ]
    )
    resp = client.post(
        "/api/align",
        json={"script": script, "srt": srt, "anchors": [{"script": 2, "srt": 1}]},
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["status"] for r in rows] == [
        "missing_srt", "match", "missing_script", "match",
    ]
    anchor_row = rows[1]
    assert anchor_row["anchor"] is True
    assert anchor_row["script_index"] == 2 and anchor_row["srt_index"] == 1
    assert anchor_row["cost"] == 1.0  # 差异很大也不跳过


def test_anchor_out_of_range_returns_400():
    for anchors in (
        [{"script": 0, "srt": 1}],
        [{"script": 1, "srt": 0}],
        [{"script": 4, "srt": 1}],   # 对白稿只有 3 行
        [{"script": 1, "srt": 4}],   # SRT 只有 3 块
    ):
        resp = _align_with_anchors(anchors)
        assert resp.status_code == 400
        assert "锚点" in resp.json()["detail"]
        assert "超出范围" in resp.json()["detail"]


def test_anchor_duplicate_returns_400():
    resp = _align_with_anchors(
        [{"script": 1, "srt": 1}, {"script": 1, "srt": 1}]
    )
    assert resp.status_code == 400
    assert "重复" in resp.json()["detail"]


def test_anchor_crossing_returns_400():
    resp = _align_with_anchors(
        [{"script": 1, "srt": 2}, {"script": 2, "srt": 1}]
    )
    assert resp.status_code == 400
    assert "交叉" in resp.json()["detail"]
    # 两列都必须严格递增：仅一列递增也算交叉
    resp = _align_with_anchors(
        [{"script": 1, "srt": 1}, {"script": 1, "srt": 2}]
    )
    assert resp.status_code == 400


def test_anchor_csv_marks_anchor_rows():
    resp = client.post(
        "/api/export.csv",
        json={
            "script": ANCHOR_SCRIPT,
            "srt": ANCHOR_SRT,
            "anchors": [{"script": 1, "srt": 1}],
        },
    )
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header.endswith(",锚点")
    # 锚点行末列标记「锚点」，其余行为空
    lines = text.splitlines()[1:]
    assert lines[0].endswith(",锚点")
    assert all(not ln.endswith(",锚点") for ln in lines[1:])


def test_no_anchor_keeps_original_shape():
    # 未提供锚点时：行无 anchor 字段、summary 无 anchors、CSV 无锚点列
    data = _align()
    assert all("anchor" not in r for r in data["rows"])
    assert "anchors" not in data["summary"]
    resp = client.post("/api/export.csv", json={"script": SCRIPT, "srt": SRT})
    header = resp.content.decode("utf-8-sig").splitlines()[0]
    assert "锚点" not in header
