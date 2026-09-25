"""字幕校稿 Web 应用：FastAPI 后端。"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alignment import GAP_COST, align, align_with_anchors, normalize_anchors
from .parsers import parse_script, parse_srt
from .relayout import build_relayout_srt, relayout_subtitles
from .retime import CalibPoint, build_srt, parse_timecode, retime_subtitles
from .textnorm import normalize_text

MAX_ITEMS = 2000          # 每侧条目数上限，防止 DP 规模过大导致请求过久
MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_POINTS = 500          # 重定时校准点数量上限
MIN_LINE_WIDTH = 12       # 断行排版每行最大显示宽度下限
MAX_LINE_WIDTH = 60       # 断行排版每行最大显示宽度上限

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="字幕校稿对齐", version="1.0.0")


class Anchor(BaseModel):
    script: int = Field(description="对白稿行序位（解析后非空行，从 1 开始）")
    srt: int = Field(description="SRT 块序位（解析后字幕块，从 1 开始）")


class AlignRequest(BaseModel):
    script: str = Field(default="", description="逐行对白稿")
    srt: str = Field(default="", description="SRT 字幕")
    gap_cost: float = Field(default=GAP_COST, ge=0.0, le=1.0)
    anchors: list[Anchor] | None = Field(
        default=None, description="人工锚点：强制配对的 行:块 序位对"
    )


def _build_rows(
    script_text: str,
    srt_text: str,
    gap_cost: float,
    anchors: list[Anchor] | None = None,
):
    if len(script_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "对白稿超过 2 MB 大小限制")
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")

    try:
        scripts = parse_script(script_text)
    except Exception as exc:  # 纯文本解析基本不会失败，兜底
        raise HTTPException(400, f"对白稿解析失败：{exc}") from exc
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc

    if not scripts and not subs:
        raise HTTPException(400, "两侧内容均为空，请粘贴对白稿与 SRT")
    if len(scripts) > MAX_ITEMS or len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"条目数超过每侧 {MAX_ITEMS} 条的上限，请拆分后再对齐"
        )

    norms_s = [normalize_text(it.raw_text) for it in scripts]
    norms_t = [normalize_text(it.raw_text) for it in subs]

    anchor_pairs: list[tuple[int, int]] = []
    if anchors:
        try:
            anchor_pairs = normalize_anchors(
                [(a.script, a.srt) for a in anchors], len(scripts), len(subs)
            )
        except ValueError as exc:
            raise HTTPException(400, f"锚点无效：{exc}") from exc
        ops = align_with_anchors(norms_s, norms_t, anchor_pairs, gap_cost=gap_cost)
    else:
        ops = align(norms_s, norms_t, gap_cost=gap_cost)

    rows = []
    totals = {"match": 0, "missing_script": 0, "missing_srt": 0}
    match_cost_sum = 0.0
    for op, cost, i, j in ops:
        if op in ("match", "anchor"):
            s, t = scripts[i], subs[j]
            row = {
                "status": "match",
                "cost": round(cost, 6),
                "script_index": s.index,
                "script_text": s.raw_text,
                "srt_index": t.index,
                "srt_text": t.raw_text,
                "start_tc": t.start_tc,
                "end_tc": t.end_tc,
            }
            if anchor_pairs:
                # 仅锚点模式下输出该标记，未提供锚点时保持原有字段不变
                row["anchor"] = op == "anchor"
            totals["match"] += 1
            match_cost_sum += cost
        elif op == "gap_a":
            # 对白稿有条目，SRT 无对应字幕
            s = scripts[i]
            row = {
                "status": "missing_srt",
                "cost": round(cost, 6),
                "script_index": s.index,
                "script_text": s.raw_text,
                "srt_index": None,
                "srt_text": "",
                "start_tc": "",
                "end_tc": "",
            }
            totals["missing_srt"] += 1
        else:
            # SRT 有条目，对白稿缺失
            t = subs[j]
            row = {
                "status": "missing_script",
                "cost": round(cost, 6),
                "script_index": None,
                "script_text": "",
                "srt_index": t.index,
                "srt_text": t.raw_text,
                "start_tc": t.start_tc,
                "end_tc": t.end_tc,
            }
            totals["missing_script"] += 1
        rows.append(row)

    total_cost = sum(r["cost"] for r in rows)
    summary = {
        **totals,
        "total_rows": len(rows),
        "total_cost": round(total_cost, 6),
        "gap_cost": gap_cost,
        "avg_match_cost": round(match_cost_sum / totals["match"], 6)
        if totals["match"]
        else None,
    }
    if anchor_pairs:
        summary["anchors"] = len(anchor_pairs)
    return rows, summary


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/align")
def api_align(req: AlignRequest):
    rows, summary = _build_rows(req.script, req.srt, req.gap_cost, req.anchors)
    return {"summary": summary, "rows": rows}


@app.post("/api/export.csv")
def api_export_csv(req: AlignRequest):
    rows, _ = _build_rows(req.script, req.srt, req.gap_cost, req.anchors)
    use_anchors = bool(req.anchors)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    header = [
        "状态", "代价", "对白稿行号", "对白稿文本",
        "SRT序号", "时间码入点", "时间码出点", "SRT文本",
    ]
    if use_anchors:
        header.append("锚点")
    writer.writerow(header)
    status_label = {
        "match": "对位",
        "missing_script": "缺对白稿",
        "missing_srt": "缺字幕",
    }
    for r in rows:
        values = [
            status_label[r["status"]],
            f"{r['cost']:.4f}",
            r["script_index"] if r["script_index"] is not None else "",
            r["script_text"],
            r["srt_index"] if r["srt_index"] is not None else "",
            r["start_tc"],
            r["end_tc"],
            r["srt_text"],
        ]
        if use_anchors:
            values.append("锚点" if r.get("anchor") else "")
        writer.writerow(values)
    # UTF-8 BOM，保证 Excel 直接打开中文不乱码
    data = "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="alignment.csv"'
        },
    )


class CalibPointIn(BaseModel):
    source: str = Field(description="原时间码，HH:MM:SS,mmm")
    target: str = Field(description="目标时间码，HH:MM:SS,mmm")


class RetimeRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    points: list[CalibPointIn] = Field(
        default_factory=list,
        description="按顺序排列的校准点（原时间→目标时间），两列时间均须严格递增",
    )


def _build_retime_rows(srt_text: str, points_in: list[CalibPointIn]):
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再重定时"
        )
    if not points_in:
        raise HTTPException(400, "至少需要一个校准点")
    if len(points_in) > MAX_POINTS:
        raise HTTPException(400, f"校准点数量超过 {MAX_POINTS} 的上限")

    points: list[CalibPoint] = []
    for i, p in enumerate(points_in, start=1):
        try:
            points.append(
                CalibPoint(parse_timecode(p.source), parse_timecode(p.target))
            )
        except ValueError as exc:
            raise HTTPException(400, f"第 {i} 个校准点：{exc}") from exc
    try:
        return retime_subtitles(subs, points)
    except ValueError as exc:
        raise HTTPException(400, f"重定时结果校验失败：{exc}") from exc


@app.post("/api/retime")
def api_retime(req: RetimeRequest):
    rows = _build_retime_rows(req.srt, req.points)
    return {
        "summary": {"total": len(rows), "points": len(req.points)},
        "rows": [
            {
                "new_index": r.new_index,
                "index": r.index,
                "old_start_tc": r.old_start_tc,
                "old_end_tc": r.old_end_tc,
                "new_start_tc": r.new_start_tc,
                "new_end_tc": r.new_end_tc,
                "text": r.text,
            }
            for r in rows
        ],
    }


@app.post("/api/retime.srt")
def api_retime_srt(req: RetimeRequest):
    rows = _build_retime_rows(req.srt, req.points)
    data = build_srt(rows).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="retimed.srt"'
        },
    )


class RelayoutRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    max_width: int = Field(
        default=30,
        ge=MIN_LINE_WIDTH,
        le=MAX_LINE_WIDTH,
        description="每行最大显示宽度（12~60，东亚宽字符计 2，其余计 1）",
    )


def _build_relayout_rows(srt_text: str, max_width: int):
    if not MIN_LINE_WIDTH <= max_width <= MAX_LINE_WIDTH:
        raise HTTPException(
            400,
            f"每行最大显示宽度须在 {MIN_LINE_WIDTH}~{MAX_LINE_WIDTH} 之间",
        )
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再排版"
        )
    return relayout_subtitles(subs, max_width)


def _relayout_row_json(r) -> dict:
    return {
        "index": r.index,
        "start_tc": r.start_tc,
        "end_tc": r.end_tc,
        "original_text": r.original_text,
        "result_text": r.result_text,
        "line_count": r.line_count,
        "status": r.status,
        "reason": r.reason,
    }


@app.post("/api/relayout")
def api_relayout(req: RelayoutRequest):
    rows = _build_relayout_rows(req.srt, req.max_width)
    counts = {"single": 0, "wrapped": 0, "manual": 0}
    for r in rows:
        counts[r.status] += 1
    return {
        "summary": {
            "total": len(rows),
            "max_width": req.max_width,
            "single": counts["single"],
            "wrapped": counts["wrapped"],
            "manual": counts["manual"],
        },
        "rows": [_relayout_row_json(r) for r in rows],
    }


@app.post("/api/relayout.srt")
def api_relayout_srt(req: RelayoutRequest):
    rows = _build_relayout_rows(req.srt, req.max_width)
    data = build_relayout_srt(rows).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="relayout.srt"'
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
