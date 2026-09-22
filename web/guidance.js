// Separate discussion and explicit, versioned material proposals.
const guidance = (() => {
  const panel = $('#guidance-dialog');
  const field = $('#guidance-input');
  const status = $('#guidance-status');
  let data = null;
  let working = false;
  let attachment = null;
  let dismissed = null;
  let controller = null;
  function controls() {
    const locked = working || data?.active || data?.save_error || !data;
    field.disabled = locked;
    $('#guidance-send').disabled = locked || !field.value.trim();
    $('#guidance-propose').disabled = locked || !data.included.length;
    $('#guidance-stop').hidden = !working && !data?.active;
    $('#guidance-save').hidden = !data?.save_error;
    $('#guidance-save').disabled = working || data?.active;
    $('#guidance-reload').disabled = working;
    $('#guidance-close').disabled = working;
    $('#guidance-remove-attachment').disabled = locked;
    const last = data?.conversation.messages.at(-1);
    $('#guidance-retry').hidden = !last || !['error', 'interrupted', 'stopped'].includes(last.status);
    $('#guidance-retry').disabled = locked;
    const p = data?.proposal;
    const applied = p && data?.last_change && (data.last_change.id === p.id || data.last_change.undo_of === p.id);
    const visible = p && dismissed !== p.id;
    $('#guidance-apply').hidden = !visible || !p.changes.length || applied;
    $('#guidance-apply').disabled = locked || p?.revision !== data?.revision;
    $('#guidance-dismiss').hidden = !visible || applied;
    $('#guidance-dismiss').disabled = working;
    $('#guidance-undo').hidden = !data?.last_change || Boolean(data.last_change.undo_of);
    $('#guidance-undo').disabled = locked;
  }
  function pre(text) { const el = document.createElement('pre'); el.textContent = text; return el; }
  function draw() {
    const list = $('#guidance-messages');
    list.replaceChildren();
    for (const m of data.conversation.messages) {
      const article = document.createElement('article');
      article.className = 'message';
      const label = document.createElement('strong');
      label.textContent = m.role === 'user' ? '你' : '学习指导';
      article.append(label, pre(m.content));
      if (m.status !== 'complete') article.append(pre(m.error || ({streaming:'正在生成…',stopped:'已停止，回答未完成',interrupted:'上次中断，回答未完成',error:'回答失败'}[m.status])));
      list.append(article);
    }
    $('#guidance-context-note').textContent = data.context_note;
    $('#guidance-included').replaceChildren(...data.conversation.messages.filter(m => data.included.includes(m.id)).map(m => pre(`${m.role === 'user' ? '你' : '指导'} · ${m.id}\n${m.content}`)));
    $('#guidance-attachment').hidden = !attachment;
    $('#guidance-attachment-text').textContent = attachment?.text || '';
    const area = $('#guidance-proposal');
    area.replaceChildren();
    const p = data.proposal;
    area.hidden = !p || dismissed === p.id;
    if (p && !area.hidden) {
      const title = document.createElement('h3'); title.textContent = '待核对的修改建议';
      area.append(title, pre(p.summary));
      const last = data.last_change;
      if (last?.id === p.id || last?.undo_of === p.id) area.append(pre(last.undo_of ? '该次修改已撤销。' : '该次修改已应用，下一轮教学生效。'));
      else if (p.revision !== data.revision) area.append(pre('讨论已更新，请重新整理后再应用。'));
      for (const change of p.changes) {
        const h = document.createElement('h4'); h.textContent = ({profile:'个人背景',teaching:'教学约定',goals:'学习目标'})[change.material];
        area.append(h, pre(change.reason), pre(change.diff));
        const evidence = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = '查看依据与完整候选正文';
        evidence.append(summary);
        for (const id of change.evidence) evidence.append(pre(p.discussion.find(m => m.id === id)?.content || id));
        evidence.append(pre(change.content)); area.append(evidence);
      }
      const scope = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = '查看生成这份提案时的讨论';
      scope.append(summary, ...p.discussion.map(m => pre(`${m.role === 'user' ? '你' : '指导'}\n${m.content}`))); area.append(scope);
    }
    controls();
  }
  async function reload() {
    data = await (await api('/api/guidance')).json(); draw();
  }
  async function open(conversation, answer) {
    if (working) return;
    if (!panel.open) panel.showModal();
    status.textContent = '正在读取…';
    try {
      if (answer) {
        attachment = await (await api(`/api/guidance/feedback/${conversation}/${answer}`)).json();
        $('#guidance-attachment').open = true;
      }
      await reload(); status.textContent = data.active ? '另一个窗口正在生成，可停止后重新读取。' : '交流不会自动修改材料。';
      field.focus();
    } catch (error) { status.textContent = error.message; }
  }
  async function send(retry = false) {
    if (working || !data) return;
    working = true; controller = new AbortController(); controls();
    let completed = false;
    const text = field.value;
    let reader;
    try {
      const payload = retry ? {revision:data.revision, retry_id:data.conversation.messages.at(-1).id} : {revision:data.revision, message:text, ...(attachment ? {conversation_id:attachment.conversation_id, answer_id:attachment.answer_id} : {})};
      const response = await api('/api/guidance/chat', payload, controller.signal);
      reader = response.body.getReader();
      const decoder = new TextDecoder(); let buffer = ''; let active;
      const receive = event => {
        if (event.type === 'start') {
          if (!retry) { data.conversation.messages.push(event.user); field.value = ''; attachment = null; }
          active = event.message; data.conversation.messages.push(active); status.textContent = '问题已保存，正在回答…';
        } else if (event.type === 'delta') active.content += event.text;
        else if (event.type === 'done') { Object.assign(active,event.message); data.save_error = !event.saved; completed = true; status.textContent = event.saved ? '指导对话已保存，教学材料尚未修改。' : '回答尚未保存，请勿关闭程序，先重试保存。'; }
        draw();
      };
      while (true) {
        const {value,done} = await reader.read(); buffer += done ? decoder.decode() : decoder.decode(value,{stream:true});
        let split; while ((split = buffer.indexOf('\n')) >= 0) { const line = buffer.slice(0,split); buffer = buffer.slice(split+1); if (line.trim()) receive(JSON.parse(line)); }
        if (done) break;
      }
      if (!completed) throw new Error('连接中断，请重新读取核对记录。');
    } catch (error) { status.textContent = error.name === 'AbortError' ? '已请求停止，请核对保存记录。' : error.message; }
    finally {
      if (reader) { if (!completed) await reader.cancel().catch(()=>{}); reader.releaseLock(); }
      working = false; controller = null;
      try { await reload(); } catch (error) { status.textContent = `无法重新读取：${error.message} 请保留页面。`; }
      controls();
    }
  }
  async function action(path, body, message) {
    if (working) return;
    working = true; controls(); status.textContent = '正在处理…';
    try {
      await api(path,body); await reload(); status.textContent = message;
      if (path.endsWith('/apply') || path.endsWith('/undo')) await refreshTeachingNotice();
    } catch (error) { status.textContent = error.message; }
    finally { working = false; controls(); }
  }
  $('#guidance-open').addEventListener('click',()=>open());
  $('#guidance-close').addEventListener('click',()=>panel.close());
  panel.addEventListener('cancel',event=>{ if (working) event.preventDefault(); });
  field.addEventListener('input',controls);
  field.addEventListener('keydown',event=>{ if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!$('#guidance-send').disabled) send(); } });
  $('#guidance-form').addEventListener('submit',event=>{event.preventDefault(); send();});
  $('#guidance-retry').addEventListener('click',()=>send(true));
  $('#guidance-remove-attachment').addEventListener('click',()=>{attachment=null;draw();});
  $('#guidance-reload').addEventListener('click',async()=>{try {await reload(); status.textContent='已重新读取。';} catch(error) {status.textContent=error.message;}});
  $('#guidance-stop').addEventListener('click',async()=>{try {await api('/api/guidance/stop',{}); controller?.abort(); if (!working) await reload();} catch(error) {status.textContent=error.message;}});
  $('#guidance-save').addEventListener('click',()=>action('/api/guidance/save',{},'指导记录已保存。'));
  $('#guidance-propose').addEventListener('click',()=>{dismissed=null;action('/api/guidance/propose',{revision:data.revision},'整理完成，请核对提案；材料尚未修改。');});
  $('#guidance-apply').addEventListener('click',()=>action('/api/guidance/apply',{id:data.proposal.id},'修改已应用，下一轮教学生效，历史快照不变。'));
  $('#guidance-dismiss').addEventListener('click',()=>{dismissed=data.proposal.id;draw();field.focus();status.textContent='未应用任何修改，可以继续讨论。';});
  $('#guidance-undo').addEventListener('click',()=>action('/api/guidance/undo',{id:data.last_change.id},'最近一次应用已撤销，下一轮教学读取恢复后的材料。'));
  window.addEventListener('beforeunload',event=>{if(field.value.trim() || working){event.preventDefault();event.returnValue='';}});
  return {open};
})();
