"use strict";

// SRT 时间轴重定时 + 页签切换（对齐 / 重定时 / 断行排版三个页签共用）
(() => {
  const $ = (id) => document.getElementById(id);

  const tabButtons = Array.from(document.querySelectorAll(".tab[data-tab]"));
  const views = {
    align: $("viewAlign"),
    retime: $("viewRetime"),
    relayout: $("viewRelayout"),
  };

  function switchTab(name) {
    for (const btn of tabButtons) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    }
    for (const [key, view] of Object.entries(views)) {
      view.classList.toggle("hidden", key !== name);
    }
  }
  for (const btn of tabButtons) {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  }

  const els = {
    srt: $("rtSrt"),
    points: $("rtPoints"),
    runBtn: $("rtRunBtn"),
    clearBtn: $("rtClearBtn"),
    status: $("rtStatus"),
    summary: $("rtSummary"),
    resultWrap: $("rtResultWrap"),
    body: $("rtBody"),
    downloadBtn: $("rtDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:02,500",
    "你好，世界！",
    "",
    "2",
    "00:00:03,000 --> 00:00:06,000",
    "今天我们来聊聊字幕重定时。",
    "",
    "3",
    "00:00:07,000 --> 00:00:09,000",
    "校准点之间做分段线性映射，",
    "区间外沿用最近区段斜率外推。",
    "",
  ].join("\n");

  const SAMPLE_POINTS = [
    "00:00:01,000 --> 00:00:02,000",
    "00:00:07,000 --> 00:00:10,500",
  ].join("\n");

  $("rtSample").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
    els.points.value = SAMPLE_POINTS;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    els.points.value = "";
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

  // 解析校准点行：每行「原时间 --> 目标时间」，也接受空白分隔
  function parsePointLines(text) {
    const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (!lines.length) throw new Error("请至少填写一个校准点");
    return lines.map((ln, i) => {
      const parts = ln.split(/\s*(?:-->|->|→)\s*|\s+/).filter(Boolean);
      if (parts.length !== 2) {
        throw new Error(
          `第 ${i + 1} 行校准点格式应为「原时间 --> 目标时间」，如 ` +
          "00:00:01,000 --> 00:00:02,000"
        );
      }
      return { source: parts[0], target: parts[1] };
    });
  }

  function render(data) {
    $("rtCnt").textContent = data.summary.total;
    $("rtPts").textContent = data.summary.points;
    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      tr.className = "row-match";
      tr.innerHTML = `
        <td class="cell-idx">${r.new_index}</td>
        <td class="cell-idx">${r.index}</td>
        <td class="cell-tc">${escapeHtml(r.old_start_tc)} → ${escapeHtml(r.old_end_tc)}</td>
        <td class="cell-tc cell-tc-new">${escapeHtml(r.new_start_tc)} → ${escapeHtml(r.new_end_tc)}</td>
        <td class="cell-text">${escapeHtml(r.text)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    let points;
    try {
      points = parsePointLines(els.points.value);
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, points };

    els.runBtn.disabled = true;
    setStatus("重定时中……", false);
    try {
      const resp = await fetch("/api/retime", {
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
      setStatus(`完成：${data.summary.total} 个字幕块已重定时`, false);
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
      setStatus("请先执行重定时", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/retime.srt", {
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
      a.download = "retimed.srt";
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
