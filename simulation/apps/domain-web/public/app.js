const content = document.querySelector('#content');
const libraryList = document.querySelector('#libraries');
const workspace = document.querySelector('#workspace');
const dialog = document.querySelector('#dialog');
const dialogBody = document.querySelector('#dialog-body');
const notice = document.querySelector('#notice');
const tabs = { overview: '概览', sources: '资料', interview: '访谈', review: '候选审阅', knowledge: '知识检索' };
const sourceTypes = { caseReport: '行业案例', standard: '行业规范', userAccount: '亲历经验', manual: '工具手册',
  paper: '论文', log: '运行日志', dataset: '数据集', other: '其他资料' };
const knowledgeTypes = { concept: '概念', claim: '命题', method: '方法', practice: '实践', observation: '观察', resource: '资源' };
const states = { pending: '待审', adopted: '已采用', deferred: '暂缓', rejected: '已拒绝',
  answered: '已回答', unknown: '信息待补', open: '待补', resolved: '已解决' };
const uses = { projectPractice: '项目实践', packCandidate: '包设计候选', referenceOnly: '参考资料', activeInScope: '范围内启用' };
const state = { libraries: [], current: null, tab: 'overview', query: null, token: '', modelAvailable: false };
let noticeTimer;
let navigation = 0;
let changing = false;

function escape(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}

const button = (label, action, key = '', css = '', disabled = false) =>
  `<button type="button" class="button ${css}" data-action="${action}" data-key="${escape(key)}"${disabled ? ' disabled' : ''}>${label}</button>`;
const pill = (label, css = '') => `<span class="pill ${css}">${escape(label)}</span>`;
const options = (values, selected) => Object.entries(values).map(([key, label]) =>
  `<option value="${escape(key)}"${key === selected ? ' selected' : ''}>${escape(label)}</option>`).join('');
const empty = message => `<p class="empty">${escape(message)}</p>`;
const lines = value => [...new Set(String(value || '').split(/[\r\n]+/).map(line => line.trim()).filter(Boolean))];
const editable = () => state.current?.status === 'active';
const reviewable = proposal => ['pending', 'deferred'].includes(proposal.status);

function notify(message, error = false) {
  clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.classList.toggle('error', error);
  notice.hidden = false;
  noticeTimer = setTimeout(() => { notice.hidden = true; }, error ? 9000 : 4300);
}

async function loadSession() {
  const response = await fetch('/api/session', { cache: 'no-store' });
  if (!response.ok) throw new Error('无法连接到本机领域库服务。');
  ({ token: state.token, modelAvailable: state.modelAvailable } = await response.json());
}

async function api(operation, input = {}) {
  if (!state.token) await loadSession();
  const token = state.token;
  const send = () => fetch(`/api/${operation}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Domain-Session': state.token }, body: JSON.stringify(input)
  });
  let response = await send();
  if (response.status === 403) {
    if (state.token === token) await loadSession();
    if (state.token !== token) response = await send();
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || '请求未完成。');
  return result;
}

async function refresh(libraryId = state.current?.libraryId, tab = state.tab) {
  const request = ++navigation;
  const { libraries } = await api('list');
  const selected = libraries.find(library => library.libraryId === libraryId) || libraries[0];
  const current = selected ? await api('status', { libraryId: selected.libraryId }) : null;
  if (request !== navigation) return;
  if (state.current?.libraryId !== current?.libraryId || state.current?.revision !== current?.revision) state.query = null;
  state.libraries = libraries;
  state.current = current;
  state.tab = tab;
  if (current) sessionStorage.setItem('domain-library', current.libraryId);
  render();
}

function stage(title, description, label, action, disabled = false) {
  return `<div><h3>${title}</h3><p>${description}</p></div>${button(label, action, '', '', disabled)}`;
}

function questionCard(question) {
  const source = state.current.sources.find(item => item.id === question.sourceId);
  return `<div class="question"><p><strong>${escape(question.prompt)}</strong></p>
    <small>${escape(source ? source.title : question.purpose)}</small>
    ${button('回答', 'answer', question.key, '', !editable())}</div>`;
}

function overview(library) {
  const active = Boolean(library);
  const canEdit = !active || editable();
  const steps = [
    stage('明确任务', '说明要支持谁的工作，以及结果用于什么判断。', active ? '进入访谈' : '新建领域库', active ? 'tab-interview' : 'create'),
    stage('登记资料', '选择代表案例、规范或经验，并保留对应的原文。', '导入资料', active ? 'source' : 'create', !canEdit),
    stage('整理并审阅', '关联原文，补充适用条件和例外，确认标签与采用用途。', '查看候选', active ? 'tab-review' : 'create'),
    stage('检索与导出', '检索已采用的条目，或将领域库快照交给下一阶段。', '检索知识', active ? 'tab-knowledge' : 'create')
  ];
  return `<section class="section split">
    <article class="panel"><h2>建库流程</h2><ol class="steps">
      ${steps.map((step, index) => `<li><span aria-hidden="true">${index + 1}</span>${step}</li>`).join('')}
    </ol></article>
    <article class="panel"><h2>${active ? '下一步访谈' : '开始准备'} </h2>
      ${active
        ? library.nextQuestions.map(questionCard).join('') || '<p class="question subtle">当前没有待答问题。</p>'
        : '<p class="question">用一个代表案例说明当前任务。可以先登记你已有的材料，缺少的内容在访谈时逐步补充。</p>'}
      ${active ? `<div class="question">${button('生成调研简报', 'research')}</div>` : ''}
    </article></section>`;
}

function sources(library) {
  return `<section class="section"><div class="section-head"><div><h2>已登记资料</h2>
    <p class="subtle">选择资料查看原文，再用原文行号为候选标注证据。</p></div>
    ${button('导入资料', 'source', '', 'primary', !editable())}</div>
    <div class="stack">${library.sources.map(source => `<article class="panel row"><div>
      <h3>${escape(source.title)}</h3><p class="subtle">${escape(sourceTypes[source.sourceType])}</p></div>
      <div class="actions">${button('查看原文', 'read', source.id)}${button('整理候选', 'propose', source.id, '', !editable())}</div>
    </article>`).join('') || empty('当前没有资料。导入一个案例、规范或一段亲历经验。')}</div></section>`;
}

function interview(library) {
  return `<section class="section split"><article class="panel"><h2>待补问题</h2>
    ${library.nextQuestions.map(questionCard).join('') || '<p class="question subtle">当前没有待答问题。</p>'}
    <details><summary>已记录的回答（${library.answers.length}）</summary>
      ${library.answers.map(answer => `<div class="question"><div class="row"><strong>${escape(answer.catalogId)} ${escape(states[answer.status])}</strong>
        ${button('更新回答', 'answer', answer.key, '', !editable())}</div><p class="entry-note">${escape(answer.answer)}</p></div>`).join('')}
    </details></article>
    <article class="panel"><div class="section-head"><h2>资料缺口</h2>${button('调研简报', 'research')}</div>
    ${library.gaps.map(gap => `<div class="question"><p>${escape(gap.question)}</p><small>${escape(states[gap.status] || gap.status)}</small></div>`).join('')
      || '<p class="subtle">暂无已记录的缺口。</p>'}</article></section>`;
}

function markers(proposal) {
  const group = (title, values) => values.length
    ? `<div><small>${title}</small><ul class="mini-list">${values.map(value => `<li>${escape(value)}</li>`).join('')}</ul></div>` : '';
  return `<div class="fields-pair">${group('适用条件', proposal.conditions)}${group('例外', proposal.exceptions)}</div>
    <div class="chips tags">${proposal.tags.map(tag => pill(tag)).join('')}</div>`;
}

function review(library) {
  const pending = library.proposals.filter(reviewable);
  const history = library.proposals.filter(item => !reviewable(item));
  return `<section class="section"><div class="section-head"><div><h2>候选审阅</h2>
    <p class="subtle">核对原文、适用条件和标签，再为候选选择采用用途。</p></div>
    <div class="actions">${button('批次审阅', 'batch', '', '', !editable() || !pending.length || pending.length > 50)}
      ${button('整理候选', 'propose', '', 'primary', !editable() || !library.sources.length)}</div></div>
    <div class="stack">${pending.map(item => `<article class="panel row"><div><div class="chips">
      ${pill(states[item.status], 'amber')}${pill(knowledgeTypes[item.kind])}</div><h3>${escape(item.statement)}</h3>
      <p class="subtle">${item.sourceExcerpts.length} 条来源片段</p></div>
      ${button('查看并审阅', 'review', item.id, '', !editable())}</article>`).join('')
      || empty(library.sources.length ? '当前没有待审候选，可以从已登记的原文整理一条。' : '先导入资料，再整理有原文依据的候选。')}</div>
    ${history.length ? `<details class="panel"><summary>审阅历史（${history.length}）</summary>
      ${history.map(item => `<div class="question row"><div>${pill(item.needsReassessment ? '待复核' : states[item.status], item.needsReassessment ? 'amber' : '')}
        <p>${escape(item.statement)}</p></div>
        ${item.needsReassessment ? button('修订候选', 'edit', item.id, '', !editable()) : ''}</div>`).join('')}</details>` : ''}</section>`;
}

function knowledge() {
  const data = state.query;
  return `<section class="section"><div class="section-head"><div><h2>知识检索</h2>
    <p class="subtle">按表述、条件或标签查找。留空可列出全部已采用知识。</p></div></div>
    <form id="query" class="panel toolbar"><label>关键词<input name="query" maxlength="4096" value="${escape(data?.query)}" placeholder="例如：采样"></label>
    <label>标签<input name="tags" value="${escape(data?.tags)}" placeholder="多个标签用逗号分隔"></label>
    <label class="check"><input type="checkbox" name="drafts" ${data?.drafts ? 'checked' : ''}>包含候选与待复核条目</label>
    <button class="button primary" type="submit">检索</button></form>
    ${data ? `<div class="stack"><small>${data.result.total} 条结果</small>${data.result.items.map(item => `<article class="panel">
      <div class="chips">${pill(item.needsReassessment ? '待复核' : item.lifecycle === 'confirmed' ? '已采用' : '候选')}
      ${item.lifecycle === 'confirmed' ? pill(uses[item.useDecision]) : ''}</div>
      <h3>${escape(item.statement)}</h3>${markers(item)}
      ${button('查看原文与条件', 'evidence', item.knowledgeRef.id)}</article>`).join('') || empty('未找到匹配条目。可以缩短关键词或查看待审候选。')}</div>`
      : empty('填写关键词或标签后检索。')}</section>`;
}

function render() {
  const library = state.current;
  libraryList.innerHTML = state.libraries.map(entry => `<button class="library-entry" type="button" data-action="select"
    data-key="${escape(entry.libraryId)}" aria-current="${entry.libraryId === library?.libraryId}">
    <span class="library-dot" aria-hidden="true"></span><span><strong title="${escape(entry.title)}">${escape(entry.title)}</strong>
    <small>${entry.sources} 份资料 · ${entry.pendingProposals} 条待审</small></span></button>`).join('')
    || '<p class="sidebar-empty">还没有领域库</p>';
  document.querySelector('#breadcrumb').textContent = library?.title || '领域库初始化';
  const pages = { overview, sources, interview, review, knowledge };
  content.innerHTML = `<section class="hero"><div>
      ${library ? pill(library.status === 'paused' ? '已暂停' : '整理中') : ''}
      <h1>${escape(library?.title || '领域库初始化')}</h1>
      <p>${escape(library?.objective || '把行业案例、规范和经验整理成带原文依据的领域知识。')}</p></div>
      <div class="actions">${library ? button(library.status === 'paused' ? '恢复整理' : '暂停整理', 'pause', '', 'light')
        + button('导出快照', 'export', '', 'light') : button('新建领域库', 'create', '', 'light')}</div></section>
    ${library ? `<div class="stats">
      ${[[library.sources.length, '已登记资料'], [library.proposals.filter(reviewable).length, '可审候选'], [library.adoptedKnowledge, '已采用知识'],
        [library.gaps.filter(gap => gap.status !== 'resolved').length, '资料缺口']].map(([count, label]) =>
    `<div class="stat"><strong>${count}</strong><span>${label}</span></div>`).join('')}</div>
    <nav class="tabs" aria-label="领域库操作">${Object.entries(tabs).map(([tab, label]) => `<button type="button" data-action="tab-${tab}"
      ${state.tab === tab ? 'aria-current="page"' : ''}>${label}</button>`).join('')}</nav>` : ''}
    ${library ? pages[state.tab](library) : overview(null)}`;
  workspace.setAttribute('aria-busy', 'false');
}

function showDialog(title, html) {
  document.querySelector('#dialog-title').textContent = title;
  dialogBody.innerHTML = html;
  if (!dialog.open) dialog.showModal();
}

function writeContext(library) {
  return { libraryId: library.libraryId, expectedRevision: library.revision, operationId: crypto.randomUUID() };
}

function formDialog(title, fields, submitLabel, submit) {
  showDialog(title, `<form class="form-grid">${fields}<p class="form-error" role="alert" hidden></p>
    <div class="form-actions">${button('取消', 'close')}<button class="button primary" type="submit">${submitLabel}</button></div></form>`);
  const form = dialogBody.querySelector('form');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (changing || form.getAttribute('aria-busy') === 'true') return;
    const errorBox = form.querySelector('.form-error');
    const submitButton = form.querySelector('button[type="submit"]');
    changing = submitButton.disabled = true;
    errorBox.hidden = true;
    try {
      const { libraryId, tab, message } = await submit(new FormData(form));
      dialog.close();
      notify(message);
      try { await refresh(libraryId, tab); } catch (error) { notify(error.message, true); }
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      changing = submitButton.disabled = false;
    }
  });
  return form;
}

function create() {
  formDialog('新建领域库', `<label>名称<input name="title" required maxlength="200" placeholder="例如：结构动力学经验库"></label>
    <label>首个任务目标<textarea name="objective" required maxlength="8000" placeholder="先支持什么任务？结果用于什么判断？"></textarea></label>`,
  '创建领域库', async data => {
    const result = await api('create', { title: data.get('title').trim(), objective: data.get('objective').trim(),
      operationId: crypto.randomUUID() });
    return { libraryId: result.libraryId, tab: 'overview', message: '已创建领域库。' };
  });
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(new Error('无法读取该文件，请重新选择。'));
    reader.readAsDataURL(file);
  });
}

function source(library) {
  formDialog('导入资料', `<div class="fields-pair"><label>资料标题<input name="title" maxlength="200" required></label>
    <label>资料类型<select name="sourceType">${options(sourceTypes, 'caseReport')}</select></label></div>
    <label>选择文件<input name="file" type="file" accept=".md,.txt,.csv,.json,.jsonl,.yaml,.yml,.log,.inp,.pdf"></label>
    <label>或粘贴原文<textarea class="long" name="text" maxlength="262144"></textarea></label>
    <small>文件最多 8 MiB。支持 UTF-8 文本和有文字层的 PDF。请提交已脱敏材料。</small>`, '登记资料',
  async data => {
    const file = data.get('file');
    const selected = Boolean(file?.name);
    const text = data.get('text');
    if (selected === Boolean(text.trim())) throw new Error('请选择一个文件或粘贴一份原文。');
    if (selected && (!file.size || file.size > 8 * 1024 * 1024)) throw new Error('请选择非空且不超过 8 MiB 的文件。');
    const input = { ...writeContext(library), title: data.get('title').trim(), sourceType: data.get('sourceType') };
    const result = selected ? await api('upload', { ...input, name: file.name, base64: await fileBase64(file) })
      : await api('source', { ...input, content: text });
    return { libraryId: library.libraryId, tab: 'sources', message: result.duplicate ? '这份原文已登记。' : '原文已登记，可以开始整理候选。' };
  });
}

function answer(library, key) {
  const question = library.nextQuestions.find(item => item.key === key);
  const previous = library.answers.find(item => item.key === key);
  if (!question && !previous) throw new Error('问题已更新，请刷新。');
  formDialog('记录访谈', `<p>${escape(question?.prompt || `更新 ${previous.catalogId} 的回答`)}</p>
    <label>回答状态<select name="status">${options({ answered: '提供回答', unknown: '不知道', deferred: '暂缓' }, previous?.status)}</select></label>
    <label>回答或备注<textarea name="answer" maxlength="8000">${escape(previous?.answer)}</textarea></label>
    ${previous && library.adoptedKnowledge ? '<small>更正已有回答后，已采用知识会标为待复核。</small>' : ''}`, '保存回答', async data => {
    const status = data.get('status');
    const answer = data.get('answer').trim() || { unknown: '不知道', deferred: '暂缓' }[status];
    if (!answer) throw new Error('请填写回答。');
    await api('answer', { ...writeContext(library), questionKey: key, status, answer });
    return { libraryId: library.libraryId, tab: 'interview', message: '已保存访谈回答。' };
  });
}

async function read(library, sourceId, startLine = 1, page = 0) {
  const excerpt = await api('readSource', { libraryId: library.libraryId, sourceId, startLine, page });
  if (state.current?.libraryId !== library.libraryId) return;
  const visibleLines = excerpt.inlineText.split('\n');
  const lineText = visibleLines.map((line, index) => `${excerpt.displayStartLine + index}  ${line}`).join('\n');
  const pageKey = (line, index) => `${sourceId}:${line}:${index}`;
  const previous = page > 0 ? pageKey(startLine, page - 1)
    : startLine > 1 ? pageKey(Math.max(1, startLine - 100), 0) : null;
  const next = excerpt.hasMorePages ? pageKey(startLine, page + 1)
    : excerpt.hasMoreLines ? pageKey(excerpt.endLine + 1, 0) : null;
  showDialog(excerpt.title, `<div class="actions"><small>第 ${excerpt.displayStartLine}–${excerpt.displayStartLine + visibleLines.length - 1} 行</small>
    ${previous ? button('上一页', 'read-page', previous) : ''}
    ${next ? button('下一页', 'read-page', next) : ''}
    ${button('整理候选', 'propose', sourceId, '', !editable())}</div>
    ${excerpt.sourceMetadata?.extractionCoverage?.limitations?.length ? '<p class="subtle">部分 PDF 页面没有提取到文字。</p>' : ''}
    <div class="preview"><pre>${escape(lineText)}</pre></div>`);
}

function proposalForm(library, sourceId, prior) {
  if (!library.sources.length) throw new Error('请先导入一份资料。');
  const edit = Boolean(prior);
  const anchors = prior?.sourceExcerpts || [];
  const form = formDialog(edit ? '修订候选' : '整理候选', `
    ${edit ? evidence(prior) : `<fieldset><legend>原文依据</legend><label>来源<select name="sourceId" required>
      ${options(Object.fromEntries(library.sources.map(item => [item.id, item.title])), sourceId)}</select></label>
      <div class="fields-pair"><label>起始行<input name="start" type="number" min="1" step="1" value="1" required></label>
      <label>结束行<input name="end" type="number" min="1" step="1" value="1" required></label></div>
      <div class="actions">${button('预览选中片段', 'preview-range')}
        ${state.modelAvailable ? button('用 AI 起草', 'suggest') : ''}</div>
      <div id="range-preview"></div>${state.modelAvailable ? '<small>用 AI 起草时，当前行号选中的原文会发送给配置的模型服务。</small>' : ''}</fieldset>`}
    <label>知识类型<select name="kind">${options(knowledgeTypes, prior?.kind || 'practice')}</select></label>
    <label>知识表述<textarea name="statement" required maxlength="8000">${escape(prior?.statement)}</textarea></label>
    <div class="fields-pair"><label>适用条件（每行一项）<textarea name="conditions">${escape(prior?.conditions.join('\n'))}</textarea></label>
    <label>例外（每行一项）<textarea name="exceptions">${escape(prior?.exceptions.join('\n'))}</textarea></label></div>
    <label>标签（每行一项）<textarea name="tags">${escape(prior?.tags.join('\n'))}</textarea></label>`, '提交待审', async data => {
    const sourceIds = edit ? anchors.map(item => item.sourceId) : [data.get('sourceId')];
    const evidenceRanges = edit ? anchors.filter(item => item.location.kind === 'line').map(item => {
      const [startLine, endLine] = item.location.value.split('-').map(Number);
      return { sourceId: item.sourceId, startLine, endLine };
    }) : [rangeInputs(data)];
    for (const key of ['conditions', 'exceptions', 'tags']) {
      const values = lines(data.get(key));
      if (values.length > 32 || values.some(value => value.length > 200)) throw new Error('条件、例外和标签各最多 32 项，单项最多 200 字符。');
    }
    const conditions = lines(data.get('conditions'));
    await api('propose', { ...writeContext(library), replaceProposalId: prior?.id, kind: data.get('kind'),
      statement: data.get('statement').trim(), conditions: conditions.length ? conditions : undefined,
      exceptions: lines(data.get('exceptions')), tags: lines(data.get('tags')), sourceIds, evidenceRanges });
    return { libraryId: library.libraryId, tab: 'review', message: '知识与标签候选已提交，等待人工审阅。' };
  });
  form.querySelector('fieldset')?.addEventListener('input', () => {
    form.querySelector('#range-preview').textContent = '';
  });
}

function rangeInputs(data) {
  const startLine = Number(data.get('start'));
  const endLine = Number(data.get('end'));
  if (!Number.isSafeInteger(startLine) || !Number.isSafeInteger(endLine) || startLine < 1 || endLine < startLine) {
    throw new Error('请选择有效的原文行号范围。');
  }
  return { sourceId: data.get('sourceId'), startLine, endLine };
}

async function previewOrSuggest(library, action, control) {
  const form = control.closest('form');
  const target = form.querySelector('#range-preview');
  const range = rangeInputs(new FormData(form));
  const fieldset = control.closest('fieldset');
  const submit = form.querySelector('button[type="submit"]');
  form.setAttribute('aria-busy', 'true');
  fieldset.disabled = submit.disabled = true;
  try {
    const input = { libraryId: library.libraryId, ...range };
    if (action === 'suggest') {
      target.textContent = '正在整理草稿…';
      const draft = await api('suggest', input);
      for (const key of ['statement', 'conditions', 'exceptions', 'tags']) {
        form.elements.namedItem(key).value = Array.isArray(draft[key]) ? draft[key].join('\n') : draft[key];
      }
      target.textContent = '草稿已填入。核对并修改后可提交待审。';
    } else {
      const excerpt = await api('readSource', input);
      if (excerpt.endLine !== range.endLine || !excerpt.inlineText.trim()) throw new Error('选中的行号超出原文，或没有可用文字。');
      if (excerpt.truncated) throw new Error('选中的原文超过 16,000 字符，请缩小行号范围。');
      target.innerHTML = `<div class="preview"><small>第 ${excerpt.startLine}–${excerpt.endLine} 行</small>
        <pre>${escape(excerpt.inlineText)}</pre></div>`;
    }
  } catch (error) {
    target.textContent = error.message;
  } finally {
    fieldset.disabled = submit.disabled = false;
    form.removeAttribute('aria-busy');
  }
}

function evidence(proposal) {
  return proposal.sourceExcerpts.map(item => `<div class="preview"><div class="row">
    <small>${escape(item.title)} · ${item.location.kind === 'line' ? '原文行号 ' : ''}${escape(item.location.value)}</small>
    ${item.truncated ? button('补读原文', 'read-page', `${item.sourceId}:${item.location.kind === 'line' ? item.location.value.split('-')[0] : 1}`) : ''}</div><pre>${escape(item.quote)}</pre></div>`).join('');
}

function decide(library, items, batch) {
  if (!items.length) throw new Error('当前没有可审候选。');
  const choices = { projectPractice: '采用为项目实践', packCandidate: '采用为包设计候选',
    ...(!batch ? { referenceOnly: '采用为参考资料' } : {}), defer: '暂缓', reject: '拒绝' };
  formDialog(batch ? `审阅 ${items.length} 条候选` : '审阅候选', `
    ${items.map(item => `<article class="panel"><h3>${escape(item.statement)}</h3>${markers(item)}${evidence(item)}</article>`).join('')}
    ${batch ? '<small>此决定应用于上面列出的每一条候选。</small>' : button('修改候选', 'edit', items[0].id)}
    <label>审阅决定<select name="decision" required><option value="">请选择</option>${options(choices)}</select></label>
    <label>决定依据（选填）<input name="reason" maxlength="8000"></label>`, '记录决定', async data => {
    const choice = data.get('decision');
    const verdict = ['defer', 'reject'].includes(choice) ? choice : 'adopt';
    const decisions = items.map(item => ({ proposalId: item.id, subjectDigest: item.subjectDigest, verdict,
      intendedUse: verdict === 'adopt' ? choice : undefined, reason: data.get('reason').trim() || undefined }));
    await api(batch ? 'decideMany' : 'decide', { ...writeContext(library), ...(batch ? { decisions } : decisions[0]) });
    state.query = null;
    return { libraryId: library.libraryId, tab: 'review', message: '已记录审阅决定。' };
  });
}

async function research(library) {
  const brief = await api('research', { libraryId: library.libraryId });
  const topics = [...new Set([...brief.questions.map(item => item.prompt), ...brief.gaps.filter(item => item.status !== 'resolved').map(item => item.question)])];
  const text = [`继续调研领域库 ${brief.libraryId}`, `目标：${brief.objective}`, '待核查问题', ...topics,
    '研究方法', ...brief.method, brief.nextAction].join('\n');
  showDialog('调研简报', `<p>可以将简报交给 Pi 继续调研，再把查到的资料登记为来源。</p>
    <div class="preview"><pre id="brief">${escape(text)}</pre></div>
    <div class="form-actions">${button('复制简报', 'copy-brief', '', 'primary')}</div>`);
}

async function download(library) {
  const snapshot = await api('snapshot', { libraryId: library.libraryId });
  const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${library.libraryId}-r${snapshot.revision}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notify('领域库快照已交给浏览器下载。');
}

async function onAction(control) {
  const { action, key } = control.dataset;
  const library = state.current;
  if (changing || dialogBody.querySelector('form[aria-busy="true"]')) return;
  if (action === 'close') return dialog.close();
  if (action === 'create') return create();
  if (action === 'refresh') {
    state.query = null;
    await refresh();
    return notify('已刷新。');
  }
  if (action === 'select') return refresh(key, 'overview');
  if (action.startsWith('tab-')) {
    state.tab = action.slice(4);
    render();
    content.querySelector('[aria-current="page"]')?.focus();
    return;
  }
  if (!library) return;
  const proposal = library.proposals.find(item => item.id === key);
  if (action === 'source') return source(library);
  if (action === 'answer') return answer(library, key);
  if (action === 'read') return read(library, key);
  if (action === 'read-page') {
    const [sourceId, line, page = '0'] = key.split(':');
    return read(library, sourceId, Number(line), Number(page));
  }
  if (action === 'propose') return proposalForm(library, key);
  if (action === 'edit' && proposal) return proposalForm(library, '', proposal);
  if (action === 'preview-range' || action === 'suggest') return previewOrSuggest(library, action, control);
  if (action === 'review' && proposal) return decide(library, [proposal], false);
  if (action === 'batch') return decide(library, library.proposals.filter(reviewable), true);
  if (action === 'evidence' && proposal) return showDialog('知识依据', `<h3>${escape(proposal.statement)}</h3>${markers(proposal)}${evidence(proposal)}`);
  if (action === 'research') return research(library);
  if (action === 'copy-brief') {
    await navigator.clipboard.writeText(dialogBody.querySelector('#brief').textContent);
    dialog.close();
    return notify('已复制调研简报。');
  }
  if (action === 'export') return download(library);
  if (action === 'pause') {
    changing = true;
    const paused = library.status !== 'paused';
    try {
      await api('pause', { libraryId: library.libraryId, expectedRevision: library.revision, paused });
      await refresh();
      notify(paused ? '已暂停写入。' : '已恢复整理。');
    } finally { changing = false; }
  }
}

document.addEventListener('click', event => {
  const control = event.target.closest('[data-action]');
  if (control) onAction(control).catch(error => {
    const formError = dialog.open && dialogBody.querySelector('.form-error');
    if (formError) { formError.textContent = error.message; formError.hidden = false; }
    else notify(error.message, true);
  });
});
dialog.addEventListener('cancel', event => {
  if (changing || dialogBody.querySelector('form[aria-busy="true"]')) event.preventDefault();
});
content.addEventListener('submit', async event => {
  if (event.target.id !== 'query') return;
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('[type="submit"]');
  const data = new FormData(form);
  const libraryId = state.current.libraryId;
  const query = data.get('query').trim();
  const tags = data.get('tags').split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
  const drafts = data.has('drafts');
  submit.disabled = true;
  try {
    const result = await api('query', { libraryId, query, tags, includeCandidates: drafts });
    if (form.isConnected && state.current?.libraryId === libraryId) {
      state.query = { query, tags: data.get('tags'), drafts, result };
      render();
      content.querySelector('#query input').focus();
    }
  } catch (error) { notify(error.message, true); }
  finally { submit.disabled = false; }
});

try {
  await loadSession();
  await refresh(sessionStorage.getItem('domain-library'));
} catch (error) {
  workspace.setAttribute('aria-busy', 'false');
  content.innerHTML = empty(error.message);
}
