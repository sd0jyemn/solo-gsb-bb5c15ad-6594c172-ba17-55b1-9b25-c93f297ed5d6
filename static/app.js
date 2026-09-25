"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  script: $("script"),
  srt: $("srt"),
  gap: $("gapCost"),
  anchors: $("anchors"),
  alignBtn: $("alignBtn"),
  clearBtn: $("clearBtn"),
  status: $("status"),
  summary: $("summary"),
  resultWrap: $("resultWrap"),
  body: $("resultBody"),
  downloadBtn: $("downloadBtn"),
};

const SAMPLE_SCRIPT = [
  "你好，世界！",
  "今天我们来聊聊字幕校对。",
  "这一句只出现在对白稿里。",
  "编辑距离会忽略标点和空格。",
  "再见！",
  "再见！",
].join("\n");

const SAMPLE_SRT = [
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
].join("\n");

$("sampleScript").addEventListener("click", () => {
  els.script.value = SAMPLE_SCRIPT;
});
$("sampleSrt").addEventListener("click", () => {
  els.srt.value = SAMPLE_SRT;
});
els.clearBtn.addEventListener("click", () => {
  els.script.value = "";
  els.srt.value = "";
  els.anchors.value = "";
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

const STATUS_META = {
  match: { label: "对位", badge: "badge-match" },
  missing_script: { label: "缺对白稿", badge: "badge-missing_script" },
  missing_srt: { label: "缺字幕", badge: "badge-missing_srt" },
};

function costClass(status, cost) {
  if (status !== "match") return "dim";
  if (cost >= 0.3) return "cost-high";
  if (cost >= 0.1) return "cost-mid";
  return "cost-low";
}

let lastRequest = null;

// 解析「行:块」锚点输入：多对以逗号/分号/空格分隔，冒号支持全角
function parseAnchors(text) {
  const trimmed = text.trim();
  if (!trimmed) return [];
  return trimmed.split(/[\s,;，；、]+/).filter(Boolean).map((tok) => {
    const m = tok.match(/^(\d+)\s*[:：]\s*(\d+)$/);
    if (!m) throw new Error(`锚点「${tok}」格式应为 行:块，如 3:3`);
    return { script: Number(m[1]), srt: Number(m[2]) };
  });
}

function render(data) {
  const s = data.summary;
  $("cntMatch").textContent = s.match;
  $("cntMissingScript").textContent = s.missing_script;
  $("cntMissingSrt").textContent = s.missing_srt;
  $("cntTotal").textContent = s.total_rows;
  $("totalCost").textContent = s.total_cost.toFixed(4);
  $("avgCost").textContent =
    s.avg_match_cost === null ? "-" : s.avg_match_cost.toFixed(4);
  const chipAnchor = $("chipAnchor");
  if (s.anchors) {
    $("cntAnchor").textContent = s.anchors;
    chipAnchor.classList.remove("hidden");
  } else {
    chipAnchor.classList.add("hidden");
  }

  const frag = document.createDocumentFragment();
  for (const r of data.rows) {
    const meta = STATUS_META[r.status];
    const tr = document.createElement("tr");
    tr.className = `row-${r.status}` + (r.anchor ? " row-anchor" : "");
    const costText = r.status === "match"
      ? r.cost.toFixed(3)
      : `(${r.cost.toFixed(2)})`;
    const tc = r.start_tc
      ? `${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}`
      : "";
    const anchorBadge = r.anchor
      ? '<span class="badge badge-anchor">锚点</span>'
      : "";
    tr.innerHTML = `
      <td><span class="badge ${meta.badge}">${meta.label}</span>${anchorBadge}</td>
      <td class="cell-num ${costClass(r.status, r.cost)}">${costText}</td>
      <td class="cell-idx">${r.script_index ?? ""}</td>
      <td class="cell-text">${escapeHtml(r.script_text)}</td>
      <td class="cell-idx">${r.srt_index ?? ""}</td>
      <td class="cell-tc">${tc}</td>
      <td class="cell-text">${escapeHtml(r.srt_text)}</td>`;
    frag.appendChild(tr);
  }
  els.body.replaceChildren(frag);
  els.summary.classList.remove("hidden");
  els.resultWrap.classList.remove("hidden");
}

async function align() {
  const gap = parseFloat(els.gap.value);
  if (!Number.isFinite(gap) || gap < 0 || gap > 1) {
    setStatus("跳过代价需为 0~1 之间的数字", true);
    return;
  }
  let anchors;
  try {
    anchors = parseAnchors(els.anchors.value);
  } catch (e) {
    setStatus(e.message, true);
    return;
  }
  lastRequest = { script: els.script.value, srt: els.srt.value, gap_cost: gap };
  if (anchors.length) lastRequest.anchors = anchors;

  els.alignBtn.disabled = true;
  setStatus("对齐中……", false);
  try {
    const resp = await fetch("/api/align", {
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
    setStatus(
      `完成：${data.summary.total_rows} 行，对位 ${data.summary.match} 条`,
      false
    );
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    els.alignBtn.disabled = false;
  }
}

async function downloadCsv() {
  if (!lastRequest) {
    setStatus("请先执行对齐", true);
    return;
  }
  els.downloadBtn.disabled = true;
  try {
    const resp = await fetch("/api/export.csv", {
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
    a.download = "alignment.csv";
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

els.alignBtn.addEventListener("click", align);
els.downloadBtn.addEventListener("click", downloadCsv);
