"use strict";

// SRT 时间避让：粘贴 SRT + 设置最小块间隔/单块最大平移量，
// 预览原/新时间码与各块平移量，可行时下载修整后的 SRT
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("avSrt"),
    gap: $("avGap"),
    shift: $("avShift"),
    runBtn: $("avRunBtn"),
    clearBtn: $("avClearBtn"),
    status: $("avStatus"),
    summary: $("avSummary"),
    resultWrap: $("avResultWrap"),
    body: $("avBody"),
    downloadBtn: $("avDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "你好，世界！",
    "",
    "2",
    "00:00:02,500 --> 00:00:04,000",
    "这一块和上一块重叠了",
    "",
    "3",
    "00:00:04,100 --> 00:00:06,000",
    "间隔太短的也算冲突",
    "",
    "4",
    "00:00:10,000 --> 00:00:12,000",
    "正常的字幕块不需要动",
    "",
  ].join("\n");

  $("avSample").addEventListener("click", () => {
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
    const gap = parseInt(els.gap.value, 10);
    const shift = parseInt(els.shift.value, 10);
    if (!Number.isInteger(gap) || gap < 0 || gap > 60000) {
      throw new Error("最小块间隔须为 0~60000 之间的整数毫秒");
    }
    if (!Number.isInteger(shift) || shift < 0 || shift > 3600000) {
      throw new Error("单块最大平移量须为 0~3600000 之间的整数毫秒");
    }
    return { min_gap_ms: gap, max_shift_ms: shift };
  }

  function shiftCell(ms) {
    if (ms > 0) return `<span class="shift-pos">+${ms} ms</span>`;
    if (ms < 0) return `<span class="shift-neg">-${-ms} ms</span>`;
    return '<span class="shift-zero">0</span>';
  }

  function render(data) {
    const s = data.summary;
    $("avCnt").textContent = s.total;
    $("avMoved").textContent = s.moved;
    $("avMaxShift").textContent = s.max_abs_shift_ms;
    $("avGapShown").textContent = s.min_gap_ms;
    $("avLimit").textContent = s.max_shift_ms;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      if (r.shift_ms !== 0) tr.className = "row-shifted";
      tr.innerHTML = `
        <td class="cell-idx">${r.new_index}</td>
        <td class="cell-idx">${r.index}</td>
        <td class="cell-tc">${escapeHtml(r.old_start_tc)} → ${escapeHtml(r.old_end_tc)}</td>
        <td class="cell-tc cell-tc-new">${escapeHtml(r.new_start_tc)} → ${escapeHtml(r.new_end_tc)}</td>
        <td class="cell-shift">${shiftCell(r.shift_ms)}</td>
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
      const resp = await fetch("/api/avoid", {
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
        s.moved
          ? `完成：${s.total} 个字幕块，${s.moved} 块已平移，最大平移 ${s.max_abs_shift_ms} ms`
          : `完成：${s.total} 个字幕块均满足约束，无需平移`,
        false
      );
    } catch (e) {
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
      const resp = await fetch("/api/avoid.srt", {
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
      a.download = "avoid.srt";
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
