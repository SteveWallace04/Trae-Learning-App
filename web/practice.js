/* C/Java scratchpad; execution is explicit and never triggers a model call. */
const practice = (() => {
  const states = new Map();
  let state = null;
  let loading = false;
  let starting = false;
  let active = null;
  let pollTimer = null;
  let saveTimer = null;
  let escapeTab = false;
  const labels = {complete: "完成", stopped: "已停止", timeout: "超时，已结束", output_limit: "输出超限，已截断并结束", compile_error: "编译失败", runtime_error: "运行出错", error: "启动或清理失败"};
  const el = (id) => document.getElementById(`practice-${id}`);
  const status = (text) => { el("status").textContent = text; };
  const saveStatus = (text) => { el("save-status").textContent = text; };
  const draft = () => state?.drafts[state.drafts.language];

  function panelControls() {
    const unavailable = !ready || switching || loading || starting || !state;
    el("language").disabled = unavailable;
    el("code").disabled = unavailable;
    el("input").disabled = unavailable;
    el("run").disabled = unavailable || starting || !!active || !state?.supported || !state?.tools[state?.drafts.language] || !draft()?.code.trim();
    el("stop").hidden = !active;
    el("stop").disabled = !active;
    el("save").disabled = unavailable || !!state?.saving;
    el("reload").disabled = !ready || switching || loading || starting || !!state?.saving;
    el("attach").disabled = unavailable || !state?.result || input.disabled;
  }

  function showResult() {
    const result = state?.result;
    if (!result) {
      el("output").textContent = "运行后在这里查看编译信息和输出。";
      return;
    }
    const lines = [`${result.language === "c" ? "C" : "Java"} · ${result.started_at} · ${labels[result.status] || result.status}`];
    for (const stage of result.stages) {
      lines.push(`\n${stage.phase === "compile" ? "编译" : "运行"} · ${labels[stage.status]} · 退出码 ${stage.exit_code} · ${stage.elapsed_ms} ms`,
        `命令：${stage.command.join(" ")}`, `标准输出：\n${stage.stdout || "（无）"}`, `标准错误：\n${stage.stderr || "（无）"}`);
    }
    if (result.error) lines.push(result.error);
    el("output").textContent = lines.join("\n");
  }

  function render() {
    if (!state) return;
    el("language").value = state.drafts.language;
    el("code").value = draft().code;
    el("input").value = draft().stdin;
    el("tools").textContent = !state.supported ? "本轮只支持 Windows 本机运行。" : state.tools[state.drafts.language]
      ? "已找到本机工具。编译最多 30 秒，运行最多 10 秒，每阶段输出最多 16 KiB。"
      : state.drafts.language === "c" ? "未找到 gcc。请安装 GCC 并加入 PATH，重启应用后重新读取。" : "未找到完整 JDK。需要 javac 和 java；安装并加入 PATH 后重启应用。";
    saveStatus(state.dirty ? "代码尚未保存。" : "草稿已读取；编辑后自动保存。" );
    showResult();
    panelControls();
  }

  async function load(force = false) {
    const id = conversationId;
    if (!id) return;
    if (!force && state?.id === id) return;
    loading = true;
    state = states.get(id) || null;
    windowControls();
    try {
      const data = await (await api(`/api/practice/${id}`)).json();
      if (conversationId !== id) return;
      if (!state || force || !state.dirty) {
        state = {id, drafts: data.drafts, tools: data.tools, supported: data.supported, dirty: false, edit: 0, saving: null, result: data.result};
        states.set(id, state);
      } else {
        state.tools = data.tools;
        state.supported = data.supported;
        state.result = data.result;
      }
      active = data.active;
      status(active ? "程序正在运行，可以点击停止。" : "");
      render();
      schedulePoll();
    } catch (error) {
      status(`练习区读取失败：${error.message} 可点击“重新读取”。`);
    } finally {
      loading = false;
      windowControls();
    }
  }

  function windowControls() { controls(); }

  function changed() {
    state.dirty = true;
    state.edit += 1;
    saveStatus("正在等待保存……");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => flush().catch(() => {}), 500);
    panelControls();
  }

  async function flush() {
    clearTimeout(saveTimer);
    const target = state;
    if (!target) return;
    if (target.saving) await target.saving;
    if (!target.dirty) return;
    target.saving = (async () => {
      while (target.dirty) {
        const edit = target.edit;
        const payload = {conversation_id: target.id, ...structuredClone(target.drafts)};
        const saved = await (await api("/api/practice/save", payload)).json();
        target.drafts.revision = saved.revision;
        if (edit === target.edit) target.dirty = false;
      }
    })();
    panelControls();
    try {
      await target.saving;
      if (state === target) saveStatus("代码和输入已保存在本机。" );
    } catch (error) {
      if (state === target) saveStatus(`${error.message} 内容仍在页面中；请勿关闭，可点击“保存草稿”重试。`);
      throw error;
    } finally {
      target.saving = null;
      panelControls();
    }
  }

  function open() {
    el("panel").hidden = false;
    $(".app-layout").classList.add("practice-visible");
    el("open").setAttribute("aria-expanded", "true");
    if (window.innerWidth <= 1100) el("panel").scrollIntoView({behavior: "smooth"});
  }

  async function insert(code) {
    if (!ready || switching || loading) return;
    if (code.length > 20000) { alertUser("代码超过 20000 字符，请选取需要练习的部分后手动粘贴。"); return; }
    await load();
    if (!state) return;
    open();
    if (draft().code.trim() && !confirm(`替换当前 ${state.drafts.language === "c" ? "C" : "Java"} 代码？已有代码将被覆盖。`)) return;
    draft().code = code;
    el("code").value = draft().code;
    status("已放入当前语言的练习区，尚未运行。" );
    changed();
    el("code").focus();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (active) pollTimer = setTimeout(poll, 500);
  }

  async function poll() {
    const id = conversationId;
    try {
      const data = await (await api(`/api/practice/${id}`)).json();
      if (id !== conversationId) return;
      active = data.active;
      if (state) { state.result = data.result; showResult(); }
      status(active ? "正在编译或运行……" : data.result ? `${labels[data.result.status]}。结果对应运行时的代码，后续编辑不改变它。` : "当前没有运行任务。" );
      schedulePoll();
    } catch (error) {
      status(`无法确认运行状态：${error.message} 请点击“重新读取”；服务中的任务仍受超时限制。`);
    }
    windowControls();
  }

  el("open").addEventListener("click", async () => { open(); await load(); });
  el("close").addEventListener("click", () => {
    el("panel").hidden = true;
    $(".app-layout").classList.remove("practice-visible");
    el("open").setAttribute("aria-expanded", "false");
  });
  el("language").addEventListener("change", () => {
    state.drafts.language = el("language").value;
    render();
    changed();
  });
  el("code").addEventListener("input", () => { draft().code = el("code").value; changed(); });
  el("input").addEventListener("input", () => { draft().stdin = el("input").value; changed(); });
  el("code").addEventListener("keydown", (event) => {
    if (event.key === "Escape") { escapeTab = true; return; }
    if (event.key === "Tab" && escapeTab) { escapeTab = false; return; }
    escapeTab = false;
    if (event.isComposing || (event.key !== "Tab" && event.key !== "Enter") || event.ctrlKey || event.altKey || event.metaKey) return;
    const editor = el("code");
    const start = editor.selectionStart;
    const line = editor.value.slice(0, start).split("\n").pop();
    const added = event.key === "Tab" ? "    " : "\n" + (line.match(/^\s*/)?.[0] || "");
    if (editor.value.length - (editor.selectionEnd - start) + added.length > 20000) return;
    event.preventDefault();
    editor.setRangeText(added, start, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input"));
  });
  el("save").addEventListener("click", () => flush().catch(() => {}));
  el("reload").addEventListener("click", async () => {
    if (state?.dirty && !confirm("重新读取会丢弃页面中尚未保存的修改。请先复制保留代码。仍要继续吗？")) return;
    clearTimeout(saveTimer);
    await load(true);
  });
  el("run").addEventListener("click", async () => {
    if (el("run").disabled) return;
    starting = true;
    windowControls();
    status("正在保存并启动……");
    try {
      await flush();
      const payload = {conversation_id: state.id, language: state.drafts.language, ...structuredClone(draft())};
      active = await (await api("/api/practice/run", payload)).json();
      state.result = null;
      showResult();
      status("正在编译或运行……");
      schedulePoll();
    } catch (error) {
      status(error.message);
      // Acceptance can be uncertain after a lost response; recover the active run id.
      try {
        const data = await (await api(`/api/practice/${conversationId}`)).json();
        active = data.active;
        state.result = data.result;
        showResult();
        schedulePoll();
      } catch { status(`${error.message} 无法确认运行状态，请重新读取。`); }
    } finally {
      starting = false;
      windowControls();
    }
  });
  el("stop").addEventListener("click", async () => {
    if (!active) return;
    el("stop").disabled = true;
    try {
      await api("/api/practice/stop", {conversation_id: active.conversation_id, run_id: active.id});
      status("正在停止编译和程序……");
      schedulePoll();
    } catch (error) { status(error.message); panelControls(); }
  });
  el("attach").addEventListener("click", () => {
    if (el("attach").disabled) return;
    const result = state.result;
    const longest = Math.max(2, ...(result.code.match(/`+/g) || []).map((s) => s.length));
    const fence = "`".repeat(longest + 1);
    const attachment = `以下是我在本机练习区的一次实际运行记录（不是老师执行的结果，也不代表掌握）：\n运行编号：${result.id}\n时间：${result.started_at}\n语言：${result.language}\n\n代码：\n${fence}${result.language}\n${result.code}\n${fence}\n\n标准输入：\n${result.stdin || "（空）"}\n\n${el("output").textContent}`;
    const next = [input.value.trim(), attachment].filter(Boolean).join("\n\n");
    if (next.length + 8 > 100000) { status("聊天内容超过长度限制，请先缩短已有问题。"); return; }
    input.value = next + "\n\n我的问题：";
    input.dispatchEvent(new Event("input"));
    input.focus();
    input.scrollIntoView({behavior: "smooth", block: "center"});
    status("已放入聊天输入框，可补充问题后发送；尚未调用模型。" );
  });
  window.addEventListener("beforeunload", (event) => {
    if ([...states.values()].some((item) => item.dirty || item.saving)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  return {controls: panelControls, load, flush, insert, isBusy: () => loading || starting || !!active};
})();
initialize();
