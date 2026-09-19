
const $ = (selector) => document.querySelector(selector);
const input = $("#message");
const send = $("#send");
const messageList = $("#messages");
const dialog = $("#settings-dialog");
const rows = new Map();
let settings = null;
let messages = [];
let busy = false;
let remoteBusy = false;
let ready = false;
let saveError = false;
let activeReply = null;
let stopping = false;

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "本地请求失败，请重新连接。";
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response;
}

function alertUser(text = "") {
  $("#page-alert").textContent = text;
  $("#page-alert").hidden = !text;
}

function controls() {
  input.disabled = !ready || !settings?.configured || busy || remoteBusy || saveError;
  send.textContent = busy ? (stopping ? "正在停止…" : "停止") : "发送 ↑";
  send.disabled = busy ? (!activeReply || stopping) : (input.disabled || !input.value.trim());
  $("#settings-open").disabled = !ready || busy || remoteBusy;
  $("#save-retry").hidden = !saveError;
  $("#save-retry").disabled = busy || remoteBusy;
  $("#restore").hidden = !remoteBusy && ready;
  $("#model-notice").hidden = Boolean(settings?.configured);
  $("#model-label").textContent = settings?.configured ? settings.model : "DeepSeek · 未配置";
  $("#welcome-title").closest("section").hidden = messages.length > 0;
  document.querySelectorAll(".retry-answer").forEach((button) => {
    button.disabled = !ready || busy || remoteBusy || saveError || !settings?.configured;
  });
}

function isAtBottom() {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 100;
}

function scrollIfFollowing(follow) {
  if (follow) window.scrollTo({top: document.documentElement.scrollHeight, behavior: "instant"});
}

function formatAnswer(container, text) {
  // Never allow model-supplied HTML, external images, forms, or inline event handlers.
  const html = marked.parse(text);
  container.innerHTML = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ["p", "br", "strong", "em", "del", "h1", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "pre", "code", "a", "hr", "table", "thead", "tbody", "tr", "th", "td"],
    ALLOWED_ATTR: ["href", "title", "start"],
  });
  container.querySelectorAll("a").forEach((link) => {
    const href = link.getAttribute("href") || "";
    if (!/^https?:\/\//i.test(href)) link.removeAttribute("href");
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
  container.querySelectorAll("pre").forEach((pre) => {
    const code = pre.querySelector("code");
    if (!code) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary copy-code";
    button.textContent = "复制";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        button.textContent = "已复制";
      } catch { button.textContent = "复制失败，请手动选择"; }
    });
    pre.prepend(button);
  });
}

function drawMessage(message) {
  let row = rows.get(message.id);
  if (!row) {
    const article = document.createElement("article");
    article.className = "message";
    const role = document.createElement("p");
    role.className = "message-role";
    role.textContent = message.role === "user" ? "你" : "AI";
    const content = document.createElement("div");
    content.className = "message-content";
    const status = document.createElement("div");
    status.className = "message-status";
    article.append(role, content, status);
    if (message.role === "assistant" && message.context_id) {
      const materials = document.createElement("button");
      materials.type = "button";
      materials.className = "secondary";
      materials.textContent = "本次教学材料";
      materials.addEventListener("click", () => showTeaching(message.id));
      article.append(materials);
    }
    messageList.append(article);
    row = {article, content, status};
    rows.set(message.id, row);
  }
  const plain = message.role === "user" || message.status === "streaming";
  row.content.classList.toggle("plain", plain);
  if (plain) row.content.textContent = message.content;
  else formatAnswer(row.content, message.content);
  const labels = {streaming: message.content ? "正在回答…" : "正在等待 DeepSeek…", stopped: "已停止 · 回答未完成", interrupted: "程序中断 · 回答未完成"};
  row.status.textContent = message.error || labels[message.status] || "";
}

function updateRetryButtons() {
  document.querySelectorAll(".retry-answer").forEach((button) => button.remove());
  const last = messages.at(-1);
  if (!last || !["error", "stopped", "interrupted"].includes(last.status)) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary retry-answer";
  button.textContent = "重试回答";
  button.addEventListener("click", () => startChat(last.id));
  rows.get(last.id).article.append(button);
}

function upsertMessage(message) {
  const index = messages.findIndex((item) => item.id === message.id);
  if (index < 0) messages.push(message);
  else messages[index] = message;
  drawMessage(message);
}

async function restoreConversation() {
  const data = await (await api("/api/conversation")).json();
  messages = data.conversation.messages;
  remoteBusy = data.active;
  saveError = data.save_error;
  rows.clear();
  messageList.replaceChildren();
  messages.forEach(drawMessage);
  updateRetryButtons();
  $("#composer-note").textContent = saveError
    ? "有记录尚未写入文件，请保留此页面并重试保存。"
    : remoteBusy ? "有回答正在生成，请稍后重新读取聊天。"
    : messages.length ? "聊天已保存在本机 · Enter 发送，Shift+Enter 换行" : "发送后自动保存聊天 · Enter 发送，Shift+Enter 换行";
}

async function initialize() {
  ready = false;
  controls();
  $("#connection-label").textContent = "正在连接本地服务";
  $("#retry").hidden = true;
  try {
    settings = await (await api("/api/settings")).json();
    await restoreConversation();
    await refreshTeachingNotice();
    ready = true;
    $(".connection").dataset.state = "ready";
    $("#connection-label").textContent = "本地服务已连接";
    alertUser();
    scrollIfFollowing(true);
  } catch {
    $(".connection").dataset.state = "error";
    $("#connection-label").textContent = "无法读取本地资料";
    $("#retry").hidden = false;
    alertUser("无法读取设置或聊天记录，请确认本地程序仍在运行，然后重新连接。");
  }
  controls();
}

function openSettings() {
  $("#api-key").value = "";
  $("#key-note").textContent = settings.configured ? "已保存密钥，留空则保留原密钥。保存配置不代表已验证连通性。" : "密钥仅发送给本机服务和 DeepSeek。保存配置不代表已验证连通性。";
  $("#model-select").replaceChildren(...settings.models.map((name) => new Option(name, name)));
  $("#model-select").value = settings.model;
  $("#settings-status").textContent = "";
  dialog.showModal();
}

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#settings-save").disabled = true;
  try {
    settings = await (await api("/api/settings", {api_key: $("#api-key").value, model: $("#model-select").value})).json();
    $("#api-key").value = "";
    dialog.close();
    alertUser();
    controls();
    input.focus();
  } catch (error) { $("#settings-status").textContent = error.message; }
  finally { $("#settings-save").disabled = false; }
});
dialog.addEventListener("close", () => { $("#api-key").value = ""; });
$("#settings-open").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", () => dialog.close());

async function refreshTeachingNotice() {
  try {
    const data = await (await api("/api/teaching")).json();
    $("#teaching-notice").textContent = data.mode === "linked"
      ? `已接入原项目的 ${data.materials.length} 份材料。发送时将连同聊天交给 DeepSeek；断点目前只读取，不自动更新。`
      : "尚未连接原项目，当前使用基础聊天提示。可在“教学材料”中查看。";
  } catch (error) { $("#teaching-notice").textContent = error.message; }
}

async function showTeaching(messageId = null) {
  const panel = $("#teaching-dialog");
  const target = $("#teaching-content");
  $("#teaching-title").textContent = messageId ? "本次教学材料" : "下一次回答的教学材料";
  $("#teaching-summary").textContent = "正在读取……";
  target.replaceChildren();
  panel.showModal();
  try {
    const data = await (await api(messageId ? `/api/messages/${messageId}/context` : "/api/teaching")).json();
    $("#teaching-summary").textContent = data.mode === "linked"
      ? `${messageId ? "这是该次请求保存的原文快照，后续修改不会改变它。" : "这是当前原文预览；发送问题时会重新读取。"}来源：${data.source}。共 ${data.materials.length} 份，${data.system_prompt.length.toLocaleString()} 字符（不是 token 数）。材料会发送给 DeepSeek；目前不运行代码，也不更新学习记录。`
      : "本次使用基础聊天提示，没有接入个人学习材料。";
    function addSection(title, text) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = title;
      const body = document.createElement("pre");
      body.textContent = text;
      details.append(summary, body);
      target.append(details);
    }
    data.materials.forEach((item) => addSection(`${item.title} · ${item.path}`, item.content));
    addSection("完整教学上下文（含应用能力说明）", data.system_prompt);
  } catch (error) { $("#teaching-summary").textContent = error.message; }
}

$("#teaching-open").addEventListener("click", () => showTeaching());
$("#teaching-close").addEventListener("click", () => $("#teaching-dialog").close());

async function startChat(retryId = null) {
  if (busy || remoteBusy || saveError || !ready || !settings?.configured) return;
  const text = input.value.trim();
  if (!retryId && !text) return;
  const previousIds = new Set(messages.map((message) => message.id));
  busy = true;
  activeReply = null;
  stopping = false;
  let completed = false;
  let accepted = false;
  alertUser();
  controls();
  try {
    const response = await api("/api/chat", retryId ? {retry_id: retryId} : {message: text});
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    function receive(event) {
      const follow = isAtBottom();
      if (event.type === "start") {
        accepted = true;
        if (!retryId) { input.value = ""; input.style.height = ""; }
        upsertMessage(event.user);
        upsertMessage(event.message);
        activeReply = event.message.id;
        $("#composer-note").textContent = "问题已保存，正在生成回答…";
      } else if (event.type === "delta") {
        const answer = messages.find((item) => item.id === activeReply);
        answer.content += event.text;
        drawMessage(answer);
      } else if (event.type === "done") {
        completed = true;
        upsertMessage(event.message);
        saveError = !event.saved;
        $("#composer-note").textContent = event.saved ? "聊天已保存在本机" : "回答尚未保存，请保留此页面并重试保存。";
        if (!event.saved) alertUser("文件保存失败，回答目前仍在本地程序内存中。请勿关闭程序，先重试保存。");
      }
      controls();
      scrollIfFollowing(follow);
    }
    try {
      while (true) {
        const {value, done} = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
        let newline;
        while ((newline = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) receive(JSON.parse(line));
        }
        if (done) break;
      }
      if (buffer.trim()) receive(JSON.parse(buffer));
      if (!completed) throw new Error("连接中断，回答可能未完成。");
    } finally {
      if (!completed) await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  } catch (error) {
    // Restore server truth before another send; request acceptance may be uncertain.
    try {
      await restoreConversation();
      if (!retryId && !accepted && messages.some((message) => !previousIds.has(message.id) && message.role === "user" && message.content === text)) {
        input.value = "";
      }
    } catch { ready = false; $("#retry").hidden = false; }
    alertUser(error.message || "连接中断，请检查记录后重试。");
  } finally {
    busy = false;
    stopping = false;
    activeReply = null;
    updateRetryButtons();
    controls();
    if (ready && !saveError && !remoteBusy) input.focus();
  }
}

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!busy) return startChat();
  if (!activeReply || stopping) return;
  stopping = true;
  controls();
  try {
    await api("/api/chat/stop", {reply_id: activeReply});
  } catch (error) {
    stopping = false;
    alertUser(error.message);
    controls();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
  controls();
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!send.disabled) $("#composer").requestSubmit();
  }
});
$("#retry").addEventListener("click", initialize);
$("#restore").addEventListener("click", initialize);
$("#save-retry").addEventListener("click", async () => {
  try {
    await api("/api/conversation/save", {});
    await restoreConversation();
    alertUser();
  } catch (error) { alertUser(error.message); }
  controls();
});
initialize();
