"""SRT 断行排版：宽度/断点规则单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.relayout import (
    build_relayout_srt,
    flatten_body,
    layout_block,
    relayout_subtitles,
)

client = TestClient(app)


def _srt(body_by_index):
    blocks = []
    for i, body in enumerate(body_by_index, start=1):
        blocks.append(
            f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n{body}"
        )
    return "\n\n".join(blocks) + "\n"


# ---------- 宽度与字簇 ----------

def test_single_line_when_fits():
    status, lines, reason = layout_block("你好世界", 20)
    assert status == "single"
    assert lines == ("你好世界",)
    assert reason == ""


def test_wide_chars_count_two():
    # 6 个汉字宽度 12，恰能放下；上限 11 时必须两行
    assert layout_block("一二三四五六", 12)[0] == "single"
    status, lines, _ = layout_block("一二三四五六", 11)
    assert status == "wrapped"
    assert lines == ("一二三四五", "六")


def test_narrow_chars_count_one():
    text = "abcdefghij"  # 宽度 10
    assert layout_block(text, 10)[0] == "single"
    status, lines, _ = layout_block(text, 9)
    assert status == "wrapped"
    assert "".join(lines) == text


def test_combining_chars_attach_to_previous():
    # 基字符 e + 组合重音符 U+0301（类别 Mn，宽度 0），组合符不与基字拆开
    text = "e\u0301\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b"
    assert text[1] == "\u0301"  # 确认是分解形式：e 之后紧跟组合符
    status, lines, _ = layout_block(text, 14)
    assert status == "wrapped"
    # 组合符必须与基字 e 在同一行（e + U+0301 完整出现于第一行末尾）
    assert lines[0].endswith("e\u0301")
    # 还原后文字不变
    assert "".join(lines) == text
    # 组合符不能单独出现在任何行首
    assert all(not ln.startswith("\u0301") for ln in lines)


def test_latin_word_not_split():
    status, lines, reason = layout_block(
        "abcdefghijklmnopqrstuvwxyz 后面还有汉字内容", 20
    )
    assert status == "manual"
    assert reason
    # 宽度足够时，断点全部位于单词之间的空格处，单词完整不拆
    status2, lines2, _ = layout_block(
        "hello world subtitle breaking test case", 20
    )
    assert status2 == "wrapped"
    words = " ".join(lines2).split()
    assert words == "hello world subtitle breaking test case".split()


def test_apostrophe_word_kept_intact():
    status, lines, _ = layout_block(
        "don't worry be happy today my friend yeah", 20
    )
    assert status == "wrapped"
    # don't 必须整体出现在同一行
    assert any("don't" in ln for ln in lines)
    # 不存在把 don't 拆开的行
    assert not any(
        ln.rstrip().endswith("don") or ln.lstrip().startswith("'t")
        for ln in lines
    )
    # 较窄宽度下，撇号词仍作为整体
    _, lines2, _ = layout_block("don't worry friends", 12)
    assert lines2[0] == "don't worry"


def test_digit_run_not_split():
    status, lines, _ = layout_block("call 1234567890 now please", 16)
    assert status == "wrapped"
    assert any("1234567890" in ln for ln in lines)


# ---------- 断点枚举与选优 ----------

def test_sentence_end_punctuation_priority():
    # 逗号断点两行 8/10（差 2），中点断点 10/8（差 2）；
    # 标点级别优先，应断在逗号后
    status, lines, _ = layout_block("甲乙丙，丁戊己庚辛壬癸", 16)
    assert status == "wrapped"
    assert lines == ("甲乙丙，", "丁戊己庚辛壬癸")


def test_sentence_end_beats_clause_and_balance():
    # 句末断点可行（两行均不超宽）时，即使另一断点更均衡也选句末
    status, lines, _ = layout_block("短句甲。后续内容有八个字", 20)
    assert status == "wrapped"
    assert lines[0].endswith("。")


def test_closing_quote_inherits_punctuation_level():
    status, lines, _ = layout_block("台词。”后续文字", 12)
    assert status == "wrapped"
    assert lines[0] == "台词。”"


def test_balance_without_punctuation():
    # 无标点时取宽度差最小的断点
    status, lines, _ = layout_block("甲乙丙丁戊己庚辛壬癸", 16)
    assert lines == ("甲乙丙丁戊", "己庚辛壬癸")


def test_whitespace_is_legal_break():
    # 空白处允许断行，断行后断点处空白丢弃、文字内容不变
    status, lines, _ = layout_block("hello world subtitle test", 20)
    assert status == "wrapped"
    assert lines[0] == "hello world subtitle"
    assert "".join(lines) == "helloworldsubtitletest"


def test_balance_beats_keeping_space_when_no_punctuation():
    # 无标点时不因空白断点牺牲均衡：在 CJK 与西文混排处取宽度差最小
    status, lines, _ = layout_block("ab 丙丁戊己庚辛", 12)
    assert status == "wrapped"
    # 均衡断点（8/8，差 0）优于空白断点（ab|丙丁…，4/8，差 4）
    assert lines == ("ab 丙丁", "戊己庚辛")


def test_not_greedy_fixed_count():
    # 不是固定字数贪心：宽字符按 2 计，10 个拉丁字母与 5 个汉字同宽，
    # 断行位置随字符宽度变化而非按固定个数
    _, latin_lines, _ = layout_block("abcdefghijklmnopqrst", 10)
    _, cjk_lines, _ = layout_block("甲乙丙丁戊己庚辛壬癸", 10)
    assert latin_lines[0] == "abcdefghij"   # 10 个窄字符
    assert cjk_lines[0] == "甲乙丙丁戊"      # 5 个宽字符


def test_no_feasible_break_marks_manual():
    # 单个超长不可拆单词
    status, lines, reason = layout_block("abcdefghijklmnopqrstuvwxyz", 12)
    assert status == "manual"
    assert lines == ("abcdefghijklmnopqrstuvwxyz",)
    assert "人工" in reason


def test_two_lines_max():
    status, lines, _ = layout_block(
        "甲乙丙丁戊己庚辛壬癸子丑", 12
    )
    # 两行都必须在限宽内；不会产生第三行
    assert status == "wrapped"
    assert len(lines) == 2
    assert all(
        sum(2 if ord(c) > 0x2E80 else 1 for c in ln) <= 12 for ln in lines
    )
    # 超出两行可容纳范围时不强行排成多行，交给人工
    status2, _, _ = layout_block(
        "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥", 12
    )
    assert status2 == "manual"


# ---------- 原块多行还原 ----------

def test_flatten_body():
    assert flatten_body(("hello", "world")) == "hello world"
    assert flatten_body(("你好", "世界")) == "你好世界"
    assert flatten_body(("hello123", "世界")) == "hello123世界"
    assert flatten_body(("唯一行",)) == "唯一行"


# ---------- 逐块排版与导出 ----------

def test_relayout_preserves_timecodes_and_index():
    srt = _srt([
        "你好，世界！今天我们来聊断行排版。",
        "short",
    ])
    rows = relayout_subtitles(parse_srt(srt), 20)
    assert len(rows) == 2
    r1, r2 = rows
    assert r1.start_tc == "00:00:01,000"
    assert r1.end_tc == "00:00:01,500"
    assert r1.status == "wrapped"
    assert r2.status == "single"
    out = build_relayout_srt(rows)
    assert "00:00:01,000 --> 00:00:01,500" in out
    assert "00:00:02,000 --> 00:00:02,500" in out
    # 序号保留
    assert out.splitlines()[0] == "1"


def test_manual_block_export_keeps_original_body():
    srt = _srt([
        "abcdefghijklmnopqrstuvwxyz",
        "这是第一行\n这是第二行",
    ])
    rows = relayout_subtitles(parse_srt(srt), 12)
    assert rows[0].status == "manual"
    out = build_relayout_srt(rows)
    # 无合法排法的超长单词原样保留
    assert "abcdefghijklmnopqrstuvwxyz" in out
    # 可排的多行块：文字内容不变（仅换行可能变化）
    assert "这是第一行" in out and "这是第二行" in out


def test_text_content_unchanged_after_relayout():
    bodies = [
        "Hello world, this is a subtitle line.",
        "你好，世界！今天天气真不错呢。",
    ]
    srt = _srt(bodies)
    rows = relayout_subtitles(parse_srt(srt), 22)
    for row, body in zip(rows, bodies):
        assert row.status != "manual"
        assert "".join(row.lines) == body


# ---------- 接口 ----------

def test_api_relayout_preview():
    srt = _srt([
        "你好，世界！今天我们来聊断行排版。",
        "abcdefghijklmnopqrstuvwxyz",
    ])
    resp = client.post("/api/relayout", json={"srt": srt, "max_width": 20})
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total"] == 2
    assert data["summary"]["wrapped"] == 1
    assert data["summary"]["manual"] == 1
    rows = data["rows"]
    assert rows[0]["status"] == "wrapped"
    assert rows[0]["line_count"] == 2
    assert rows[1]["status"] == "manual"
    assert rows[1]["reason"]
    assert rows[1]["result_text"] == "abcdefghijklmnopqrstuvwxyz"
    # 预览含原文与结果
    assert "original_text" in rows[0] and "result_text" in rows[0]


def test_api_relayout_download_srt():
    srt = _srt(["你好，世界！今天我们来聊断行排版。"])
    resp = client.post("/api/relayout.srt", json={"srt": srt, "max_width": 20})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.content.decode("utf-8")
    assert "00:00:01,000 --> 00:00:01,500" in text
    assert "你好，世界！" in text


def test_api_relayout_width_bounds():
    srt = _srt(["测试"])
    for bad in (11, 61, 0):
        resp = client.post("/api/relayout", json={"srt": srt, "max_width": bad})
        assert resp.status_code in (400, 422)


def test_api_relayout_empty_and_bad_srt():
    resp = client.post("/api/relayout", json={"srt": "", "max_width": 30})
    assert resp.status_code == 400
    resp = client.post(
        "/api/relayout", json={"srt": "没有时间码", "max_width": 30}
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]
