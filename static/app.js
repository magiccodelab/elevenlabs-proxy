const $ = (id) => document.getElementById(id);
const logEl = $("log");
const statsEl = $("stats");
const player = $("player");
const dl = $("dl");

const fmt = (n) => n.toLocaleString();
function nowTs() {
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}
function appendLog(level, msg) {
  const cls = { info: "info", ok: "ok", error: "err", warn: "warn" }[level] || "info";
  const div = document.createElement("div");
  const ts = document.createElement("span"); ts.className = "ts"; ts.textContent = `[${nowTs()}] `;
  const span = document.createElement("span"); span.className = cls; span.textContent = msg;
  div.appendChild(ts); div.appendChild(span);
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? `${m}m${String(s).padStart(2, "0")}s` : `${s}s`;
}

async function bootHealth() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    const eg = $("egress");
    const ok = j.egress_actual && j.egress_actual === j.egress_required;
    eg.textContent = `egress ${j.egress_actual || "?"} ${ok ? "✓" : "✗"} (要求 ${j.egress_required})`;
    eg.classList.remove("wait", "ok", "bad");
    eg.classList.add(ok ? "ok" : "bad");
    const tok = j.token || {};
    $("account").textContent =
      `账号 ${j.account || "?"} · impersonate=${j.impersonate} · ` +
      `token=${tok.source || "?"}` +
      (tok.expires_in_seconds != null ? ` (剩 ${fmtDuration(tok.expires_in_seconds)})` : "");
    appendLog(ok ? "ok" : "error",
      `health: egress=${j.egress_actual} (要求 ${j.egress_required}), account=${j.account}, ` +
      `impersonate=${j.impersonate}, token=${tok.source} 剩 ${fmtDuration(tok.expires_in_seconds)}`);
  } catch (e) {
    $("egress").textContent = "health failed";
    $("egress").classList.remove("wait", "ok"); $("egress").classList.add("bad");
    appendLog("error", "health 接口失败: " + e);
  }
}
setInterval(bootHealth, 30_000);

$("clear").onclick = () => { logEl.innerHTML = ""; statsEl.textContent = ""; };

$("go").onclick = async () => {
  const text = $("text").value.trim();
  if (!text) { appendLog("warn", "请输入文本"); return; }

  $("go").disabled = true;
  statsEl.textContent = "…";
  player.removeAttribute("src");
  dl.classList.add("d-none");
  appendLog("info", "提交合成请求 …");

  const body = {
    text,
    voice_id: $("voice").value.trim() || null,
    model_id: $("model").value.trim() || null,
    stability: parseFloat($("stability").value),
  };
  if (Number.isNaN(body.stability)) body.stability = null;

  try {
    const resp = await fetch("/api/tts", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) {
      const t = await resp.text();
      appendLog("error", `HTTP ${resp.status}: ${t.slice(0, 300)}`);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    appendLog("error", "请求异常: " + e);
  } finally {
    $("go").disabled = false;
  }
};

function handleEvent(ev) {
  switch (ev.type) {
    case "log":
      appendLog(ev.level || "info", ev.msg);
      break;
    case "progress":
      statsEl.textContent = `已收 ${fmt(ev.received)} bytes (chunk +${fmt(ev.chunk)})`;
      break;
    case "done":
      appendLog("ok", `完成: ${fmt(ev.size)} bytes / ${ev.elapsed_ms}ms`);
      player.src = ev.url;
      player.play().catch(() => {});
      dl.href = ev.url;
      dl.classList.remove("d-none");
      break;
    case "error":
      appendLog("error", ev.msg);
      break;
  }
}

bootHealth();
