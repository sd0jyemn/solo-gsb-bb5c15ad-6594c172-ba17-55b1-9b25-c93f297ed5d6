"use strict";

// SRT 时间避让：粘贴 SRT + 最小块间隔/单块最大平移量，
// 预览原/新时间码与各块平移量，冲突时指出首个冲突块并禁止导出
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("shSrt"),
    gap: $("shGap"),
    maxShift: $("shMax"),
    runBtn: $("shRunBtn"),
    clearBtn: $("shClearBtn"),
    status: $("shStatus"),
    summary: $("shSummary"),
    resultWrap: $("shResultWrap"),
    body: $("shBody"),
    downloadBtn: $("shDownloadBtn"),
  };

  const MAX_GAP_MS = 60000;
  const MAX_SHIFT_MS = 3600000;

  const SAMPLE_SRT = [
    "1",
    "00:00:05,000 --> 00:00:06,000",
    "第一句字幕，与下一句重叠。",
    "",
    "2",
    "00:00:06,100 --> 00:00:07,200",
    "第二句字幕，间隔也太短。",
    "",
    "3",
    "00:00:07,300 --> 00:00:08,500",
    "第三句字幕，同样需要避让。",
    "",
    "4",
    "00:00:20,000 --> 00:00:22,000",
    "最后一句离得很远，不应被移动。",
    "",
  ].join("\n");

  $("shSample").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    els.summary.classList.add("hidden");
    els.resultWrap.classList.add("hidden");
    setStatus("", false);
  });

  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function readParams() {
    const gap = Number(els.gap.value);
    const maxShift = Number(els.maxShift.value);
    if (!Number.isInteger(gap) || gap < 0 || gap > MAX_GAP_MS) {
      throw new Error(`最小块间隔须为 0~${MAX_GAP_MS} 之间的整数毫秒`);
    }
    if (!Number.isInteger(maxShift) || maxShift < 0 || maxShift > MAX_SHIFT_MS) {
      throw new Error(`单块最大平移量须为 0~${MAX_SHIFT_MS} 之间的整数毫秒`);
    }
    return { min_gap_ms: gap, max_shift_ms: maxShift };
  }

  function fmtShift(ms) {
    if (ms > 0) return `+${ms} ms`;
    if (ms < 0) return `-${-ms} ms`;
    return "0";
  }

  function render(data) {
    const s = data.summary;
    $("shCnt").textContent = s.total;
    $("shMoved").textContent = s.shifted;
    $("shMaxApplied").textContent = s.max_applied_shift_ms;
    $("shGapShown").textContent = s.min_gap_ms;
    $("shMaxShown").textContent = s.max_shift_ms;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      tr.className = r.shift_ms !== 0 ? "row-shifted" : "";
      const shiftCls = r.shift_ms !== 0 ? "cell-shift-active" : "dim";
      tr.innerHTML = `
        <td class="cell-idx">${r.index}</td>
        <td class="cell-tc">${escapeHtml(r.old_start_tc)} → ${escapeHtml(r.old_end_tc)}</td>
        <td class="cell-tc cell-tc-new">${escapeHtml(r.new_start_tc)} → ${escapeHtml(r.new_end_tc)}</td>
        <td class="cell-num ${shiftCls}">${fmtShift(r.shift_ms)}</td>
        <td class="cell-text">${escapeHtml(r.text)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    let params;
    try {
      params = readParams();
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, ...params };

    els.runBtn.disabled = true;
    setStatus("避让中……", false);
    try {
      const resp = await fetch("/api/shift", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `请求失败（${resp.status}）` }));
        throw new Error(err.detail || `请求失败（${resp.status}）`);
      }
      const data = await resp.json();
      render(data);
      const s = data.summary;
      setStatus(
        `完成：${s.total} 个字幕块，${s.shifted} 块被平移，` +
        `最大平移 ${s.max_applied_shift_ms} ms`,
        false
      );
    } catch (e) {
      // 约束无法满足：后端已指出首个冲突块，隐藏结果并禁止导出
      lastRequest = null;
      els.summary.classList.add("hidden");
      els.resultWrap.classList.add("hidden");
      setStatus(e.message, true);
    } finally {
      els.runBtn.disabled = false;
    }
  }

  async function downloadSrt() {
    if (!lastRequest) {
      setStatus("请先执行避让", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/shift.srt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "下载失败" }));
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "shifted.srt";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      els.downloadBtn.disabled = false;
    }
  }

  els.runBtn.addEventListener("click", run);
  els.downloadBtn.addEventListener("click", downloadSrt);
})();
