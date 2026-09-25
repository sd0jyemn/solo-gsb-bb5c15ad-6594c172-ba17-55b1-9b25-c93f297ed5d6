"use strict";

// SRT 断行排版：粘贴 SRT + 设置每行最大显示宽度，预览原文/结果/状态并下载 SRT
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("rlSrt"),
    width: $("rlWidth"),
    runBtn: $("rlRunBtn"),
    clearBtn: $("rlClearBtn"),
    status: $("rlStatus"),
    summary: $("rlSummary"),
    resultWrap: $("rlResultWrap"),
    body: $("rlBody"),
    downloadBtn: $("rlDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "你好，世界！今天我们来聊聊字幕断行排版。",
    "",
    "2",
    "00:00:04,000 --> 00:00:06,500",
    "Subtitle line breaking keeps Latin words",
    "together and balances the two lines.",
    "",
    "3",
    "00:00:07,000 --> 00:00:09,000",
    "这一整段没有任何可以断开的地方也没有标点真的非常非常长啊",
    "",
    "4",
    "00:00:10,000 --> 00:00:12,000",
    "é 组合字符附着在前字之后，不可以被拆开，中日韩宽字符计二。",
    "",
  ].join("\n");

  $("rlSample").addEventListener("click", () => {
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

  function multilineCell(text) {
    return escapeHtml(text).replace(/\n/g, '<span class="br-marker">↵</span><br>');
  }

  const STATUS_META = {
    single: { label: "单行", badge: "badge-single", row: "row-single" },
    wrapped: { label: "两行", badge: "badge-wrapped", row: "row-wrapped" },
    manual: { label: "需人工处理", badge: "badge-manual", row: "row-manual" },
  };

  function render(data) {
    const s = data.summary;
    $("rlCntSingle").textContent = s.single;
    $("rlCntWrapped").textContent = s.wrapped;
    $("rlCntManual").textContent = s.manual;
    $("rlCnt").textContent = s.total;
    $("rlMaxWidth").textContent = s.max_width;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const meta = STATUS_META[r.status];
      const tr = document.createElement("tr");
      tr.className = meta.row;
      const reason = r.reason
        ? `<div class="cell-reason">${escapeHtml(r.reason)}</div>`
        : "";
      tr.innerHTML = `
        <td><span class="badge ${meta.badge}">${meta.label}</span>${reason}</td>
        <td class="cell-idx">${r.index}</td>
        <td class="cell-tc">${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}</td>
        <td class="cell-text cell-multiline">${multilineCell(r.original_text)}</td>
        <td class="cell-text cell-multiline">${multilineCell(r.result_text)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  function readWidth() {
    const w = parseInt(els.width.value, 10);
    if (!Number.isInteger(w) || w < 12 || w > 60) {
      throw new Error("每行最大显示宽度须为 12~60 之间的整数");
    }
    return w;
  }

  let lastRequest = null;

  async function run() {
    let maxWidth;
    try {
      maxWidth = readWidth();
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, max_width: maxWidth };

    els.runBtn.disabled = true;
    setStatus("排版中……", false);
    try {
      const resp = await fetch("/api/relayout", {
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
      const tail = s.manual
        ? `，其中 ${s.manual} 块无合法排法、已保留原文待人工处理`
        : "";
      setStatus(`完成：${s.total} 个字幕块（单行 ${s.single} / 两行 ${s.wrapped}）${tail}`, false);
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
      setStatus("请先执行排版", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/relayout.srt", {
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
      a.download = "relayout.srt";
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
