"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { csrf: "", capabilities: new Set(), execution: false, task: null, draft: null,
    selected: null, receipt: null, busy: false, permits: [], pendingStart: null, pendingItem: null,
    requestText: "", requestId: () => `web-${crypto.randomUUID()}` };
  const esc = (s) => String(s ?? "");
  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = esc(text); return e; };
  const opt = (select, value, label) => { const o = node("option", "", label); o.value = value; select.append(o); };
  const put = (target, child) => { target.replaceChildren(child); };
  const notice = (message, kind = "") => { const n = $("notice"); n.textContent = message; n.className = kind; };
  const busy = (flag, message = "") => { state.busy = flag; document.querySelectorAll("button").forEach(b => { if (b.id !== "cancelStart" && b.id !== "closeEditor") b.disabled = flag || b.dataset.locked === "true"; }); if (message) notice(message, flag ? "busy" : ""); };
  const taskId = () => $("taskId").value.trim();
  const snap = () => state.task;
  const revision = () => ({ task_id: snap().task_id, expected_revision: snap().revision, expected_state_seq: snap().state_seq });
  const parseRows = (raw, separator = "|") => raw.split(/\r?\n/).map(x => x.trim()).filter(Boolean).map(row => {
    const parts = row.split(separator).map(x => x.trim()); if (parts.length !== 2 || !parts[0] || !/^[a-f0-9]{64}$/.test(parts[1])) throw Error("每行需要一个路径和完整 SHA-256。");
    return { path: parts[0], sha256: parts[1] };
  });
  async function api(path, body) {
    const response = await fetch(path, { method: body ? "POST" : "GET", headers: body ? { "Content-Type": "application/json", "X-Evidence-CSRF": state.csrf } : {}, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    const data = await response.json().catch(() => ({ error: `HTTP_${response.status}` }));
    if (!response.ok || data.error) { const error = new Error(`${data.error || response.status}${data.detail ? `: ${data.detail}` : ""}`); error.code = data.error; error.status = response.status; throw error; }
    return data;
  }
  async function act(path, body, message = "正在处理…") {
    if (state.busy) return;
    busy(true, message);
    try { const result = await api(path, body); if (result.state) acceptState(result.state); else if (result.snapshot?.state) acceptState(result.snapshot.state); else if (path === "/api/command" && result.state) acceptState(result.state); notice("已完成。请查看返回状态和版本。"); return result; }
    catch (error) { notice(error.message + (error.status === 409 ? "。刷新后核对差异，不会覆盖新版本。" : ""), "error"); if (error.status === 409) $("refresh").disabled = false; throw error; }
    finally { busy(false); enableControls(); }
  }
  function enableControls() {
    const has = !!state.task, current = state.task?.current_stage;
    for (const id of ["refresh", "review", "evaluate", "reevaluate", "advance", "complete", "addItem", "replan", "addEvidence", "saveInputs", "submitHuman", "permit", "authorizeHuman", "finish"]) $(id).disabled = !has || state.busy;
    if (has && state.task.intent?.action === "consult") for (const id of ["evaluate", "reevaluate", "advance", "complete", "permit", "authorizeHuman"]) $(id).disabled = true;
    if (has) { $("reviewKind").disabled = state.busy; $("evaluateStage").value = current; }
    for (const [id, capability, needsTask] of [["measure", "measure", true], ["selectRun", "revise", true], ["refreshKnowledge", "refresh_knowledge", true], ["inspectPublication", "inspect_publication", false], ["recoverPublication", "recover_publication", false]]) {
      $(id).disabled = state.busy || (needsTask && !has) || !state.capabilities.has(capability);
      $(id).title = state.capabilities.has(capability) ? "" : "当前服务未公开此动作";
    }
  }
  function acceptState(value) {
    state.task = value; state.receipt = (value.receipts || []).at(-1) || null;
    $("taskId").value = value.task_id; $("version").textContent = `revision ${value.revision} · state ${value.state_seq}`;
    $("executionStatus").textContent = state.execution ? "本地桥已启用；服务端适配器仍须验证" : "执行已关闭：仅审阅，不会启动任务";
    render(); enableControls();
  }
  function lines(list) { return Array.isArray(list) ? list.map(x => typeof x === "string" ? x : JSON.stringify(x)) : []; }
  function simpleList(title, rows, emptyText) {
    const box = node("div"); if (!rows?.length) { box.className = "empty"; box.textContent = emptyText; return box; }
    for (const row of rows) { const item = node("div", "record"); const h = node("h4", "", title(row)); item.append(h); const p = node("p", "", typeof row === "string" ? row : row.reason || row.rationale || row.detail || row.status || JSON.stringify(row)); item.append(p); box.append(item); }
    return box;
  }
  function renderIntent() {
    const box = node("div");
    if (state.draft) {
      const intent = state.draft.intent || {action: "未识别", goals: [], unknowns: []}; const callout = node("div", "sourceQuote", state.draft.request); box.append(callout);
      const action = node("p", "", `请求类型：${intent.action}${intent.action === "consult" ? "（只读咨询，不建立审批）" : ""}`); box.append(action);
      box.append(simpleList(g => g.text, intent.goals || [], "没有识别到目标。"));
      box.append(simpleList(x => typeof x === "string" ? x : x.text, intent.unknowns || [], "未报告未知项。"));
      if (intent.citations) box.append(simpleList(c => `${c.path}: ${c.quote}`, intent.citations, "未提供引文映射。"));
      if (state.draft.discovery) {
        box.append(node("p", state.draft.discovery.complete ? "good" : "warn",
          `开放发现：${state.draft.discovery.status} · 已完成=${state.draft.discovery.complete} · 轮次=${state.draft.discovery.rounds?.length || 0}`));
        box.append(simpleList(x => x, state.draft.discovery.remaining_unknowns, "未报告未解缺口。"));
        box.append(simpleList(x => x, state.draft.discovery.limitations, "未报告局限。"));
      }
      return box;
    }
    if (!state.task) { box.className = "empty"; box.textContent = "目标识别后，这里会展示原话、推断和未知项。"; return box; }
    const i = state.task.intent; box.append(node("p", "", `任务类型：${i.action} · 当前范围：${state.task.plan?.status || "未形成计划"}`));
    for (const goal of i.goals || []) { box.append(node("div", "sourceQuote", goal.text)); box.append(simpleList(c => `${c.path}: ${c.quote}`, goal.citations || goal.source_refs || [], "未提供原话映射。")); }
    box.append(simpleList(x => typeof x === "string" ? x : x.text, i.unknowns, "未报告未知项。"));
    return box;
  }
  function renderRetrieval(bundle) {
    const box = node("div"); if (!bundle) { box.className = "empty"; box.textContent = "尚未检索。"; return box; }
    box.append(node("p", "", `状态：${bundle.status || "unknown"} · 展示候选：${bundle.candidates?.length || 0} · 检索代次：${bundle.knowledge_generation || "未提供"}`));
    box.append(simpleList(x => `${x.source}: ${x.status}`, bundle.health, "没有来源健康记录。"));
    box.append(simpleList(x => `${x.id} · ${x.channel || "来源"} · ${x.applicability?.status || "适用性未知"}`, bundle.candidates, "没有命中候选；仍需按目标提出核查项，不能据此视为通过。"));
    box.append(simpleList(x => `${x.id}: ${x.reason}`, bundle.exclusions, "没有排除候选。"));
    box.append(simpleList(x => x, bundle.limitations, "未报告局限。"));
    return box;
  }
  function renderItems() {
    const target = $("itemList"); target.replaceChildren();
    const items = state.draft?.proposal?.items || state.task?.plan?.items || [];
    $("itemCount").textContent = `${items.length} 项`;
    if (!items.length) { target.className = "empty"; target.textContent = state.draft ? "动态规划未产生核查项。任务不得零项放行。" : "尚未形成核查计划。"; return; }
    target.className = "";
    for (const item of items) {
      const b = node("button", "item"); b.type = "button"; b.setAttribute("aria-pressed", state.selected === item.id ? "true" : "false");
      b.append(node("span", "", item.question));
      b.append(node("span", "meta", `${item.id} · ${item.owner_rule} · ${item.due_stage} · ${item.applicability?.status || "未知"}`));
      const status = item.status || item.result?.verdict || "等待核验"; b.append(node("span", `badge ${/PASS|accepted|applicable/i.test(status) ? "good" : /BLOCK|missing|unknown/i.test(status) ? "warn" : ""}`, status));
      b.addEventListener("click", () => { state.selected = item.id; renderItems(); renderSelected(); }); target.append(b);
    }
  }
  function currentItem() { return (state.task?.plan?.items || state.draft?.proposal?.items || []).find(x => x.id === state.selected); }
  function renderSelected() {
    const box = $("selectedView"); box.replaceChildren(); const item = currentItem();
    if (!item) { box.className = "empty"; box.textContent = "选择一项查看问题、依据和结果。"; return; }
    box.className = "selected"; box.append(node("h3", "", item.question));
    const facts = node("dl", "mapping");
    for (const [a,b] of [["主张", item.claim], ["理由", item.rationale], ["负责规则", item.owner_rule], ["截止阶段", item.due_stage], ["适用性", item.applicability?.status], ["适用理由", item.applicability?.reason], ["来源", (item.origin_refs || []).join(", ")], ["目标", (item.target_refs || []).join(", ")], ["当前结果", (state.task?.rule_results || []).find(x => x.item_id === item.id)?.verdict || item.status]]) { facts.append(node("dt", "", a), node("dd", "", b || "未提供")); }
    box.append(facts);
    const result = (state.task?.rule_results || []).filter(x => x.item_id === item.id);
    for (const row of result) { box.append(node("p", "", `${row.verdict}: ${(row.reasons || []).map(r => r.code || r).join("; ") || "未提供理由"}`)); box.append(simpleList(x => x, row.limitations, "无已记录限制。")); box.append(simpleList(x => x, row.evidence_refs, "未绑定证据。")); }
    const reviewRows = (state.task?.reviews || []).filter(r => (r.covered_items || []).includes(item.id));
    if (reviewRows.length) { const latest = reviewRows.at(-1); box.append(node("p", "", `最近审阅：${latest.status}${latest.stale ? "（过期）" : ""} · ${latest.method || "未知方式"}`)); box.append(simpleList(x => x.reason || x.rationale, latest.conclusions || [], "审阅未返回逐项判断。")); }
  }
  function renderStage() {
    const box = $("stageView"); box.replaceChildren(); if (!state.task) { box.className = "empty"; box.textContent = "尚未建档。"; return; }
    box.className = ""; const stages = ["PLAN", "BUILD", "CONFIG", "EXEC", "ANALYZE"], strip = node("div", "stageStrip");
    for (const s of stages) { const item = node("span", s === state.task.current_stage ? "current" : "", s); if ((state.task.active_receipts || {})[s]) item.title = `凭据 ${(state.task.active_receipts || {})[s]}`; strip.append(item); }
    box.append(strip, node("p", "", `当前阶段：${state.task.current_stage} · 任务状态：${state.task.status} · 行动：${state.task.intent?.action}`));
    box.append(node("p", "", `当前内容版本：revision ${state.task.revision} · 并发序号：${state.task.state_seq} · subject ${state.task.subject_hash}`));
    const receipt = state.receipt; if (receipt) { const card = node("div", `receipt ${receipt.verdict === "PASS" ? "good" : "bad"}`); card.append(node("strong", "", `${receipt.stage} ${receipt.kind}: ${receipt.verdict}`)); card.append(simpleList(x => x, receipt.reasons, "没有拒因；这不等于没有剩余限制。")); card.append(simpleList(x => `${x.id}: ${x.status}`, receipt.coverage?.remaining || [], "无遗留项（以服务端评估为准）。")); box.append(card); }
    box.append(node("p", "muted", "允许执行 ≠ 已启动 ≠ 已结束。只有可信桥回执和产物才证明运行事实。"));
  }
  function render() {
    $("intentView").replaceChildren(renderIntent());
    $("retrievalView").replaceChildren(renderRetrieval(state.draft?.bundle || state.task?.plan?.retrieval));
    $("retrievalBox").open = !!state.draft || !!state.task;
    $("commitBox").hidden = !state.draft || !state.draft.intent || state.draft.intent.action === "consult" || state.draft.discovery?.complete !== true;
    $("goalEditBox").hidden = !state.task;
    if (state.task) $("goalEdit").value = (state.task.intent.goals || []).map(g => g.text).join("\n");
    renderItems(); renderSelected(); renderStage(); renderEvidence(); renderFindings(); renderAuth(); renderHistory(); renderRuns();
    $("inputView").replaceChildren(simpleList(x => `${x.path} · ${x.sha256}`, state.task?.dependencies?.inputs, "当前任务尚未绑定输入清单。"));
    enableControls();
  }
  function renderEvidence() {
    const list = $("evidenceList"); const rows = state.task?.evidence || []; list.replaceChildren();
    if (!rows.length) { list.className = "empty"; list.textContent = "尚无证据。"; return; }
    list.className = ""; for (const e of rows) { const r = node("article", "record"); r.append(node("h4", "", `${e.id} · ${e.source_kind} · ${e.claim_id}`)); r.append(node("p", "", `${e.verification?.status || "未验证"} / ${e.verification?.method || "方式未知"} · ${e.verification?.source_locator || "无定位"}`)); if (e.content) r.append(node("p", "sourceQuote", e.content)); if (e.source_kind === "measurement") r.append(node("p", "", `值 ${e.value ?? "?"} ${e.unit || ""} · run ${e.run_id || "未绑定"} · 输入 ${e.input_hash || "?"} · 产物 ${e.output_hash || "?"}`)); r.append(node("p", "muted", (e.provenance_refs || []).join(", ") || "未提供来源链")); const edit = node("button", "", "追加修正（不覆盖原记录）"); edit.disabled = !state.task; edit.addEventListener("click", () => openEvidence(e)); r.append(edit); list.append(r); }
  }
  function renderFindings() {
    const list = $("findingList"); const rows = state.task?.findings || []; list.replaceChildren();
    if (!rows.length) { list.className = "empty"; list.textContent = "尚无问题报告。未审阅不代表不存在问题。"; return; }
    list.className = ""; for (const f of rows) { const r = node("article", `record ${["infra", "deterministic"].includes(f.type) ? "bad" : f.status === "open" ? "warn" : ""}`); r.append(node("h4", "", `${f.id} · ${f.type} · ${f.severity || "严重程度未定"} · ${f.status}`)); r.append(node("p", "", f.detail || f.reason || f.description || "无详细说明")); r.append(simpleList(x => x, f.evidence_refs || f.source_refs, "无引用材料。")); const select = $("humanTarget"); if (![...select.options].some(o => o.value === `finding:${f.id}`)) opt(select, `finding:${f.id}`, `问题 ${f.id}`); list.append(r); }
  }
  function renderAuth() {
    const rows = state.task?.authorizations || []; $("authorizationList").replaceChildren(simpleList(x => `${x.scope} · ${x.decision} · ${x.actor_id}${x.revoked ? "（已撤回）" : ""}`, rows, "尚无授权记录。"));
    const select = $("humanTarget"); const selected = select.value; select.replaceChildren();
    if (state.task?.plan?.items) for (const i of state.task.plan.items) opt(select, `item:${i.id}`, `核查项 ${i.id} · ${i.question}`);
    for (const f of state.task?.findings || []) opt(select, `finding:${f.id}`, `问题 ${f.id}`);
    for (const a of rows) if (!a.revoked) opt(select, `authorization:${a.id}`, `授权 ${a.id} · ${a.scope}`);
    if ([...select.options].some(o => o.value === selected)) select.value = selected;
  }
  function renderHistory() {
    const s = state.task; $("stateText").textContent = s ? JSON.stringify(s, null, 2) : "";
    const box = $("historyView"); box.replaceChildren(); if (!s) { box.className = "empty"; box.textContent = "尚无记录。"; return; }
    box.className = ""; for (const [name, rows] of [["审阅",s.reviews], ["凭据",s.receipts], ["人工授权",s.authorizations], ["发现的问题",s.findings], ["运行",s.runs]]) { const h = node("h3", "", name); box.append(h); box.append(simpleList(r => `${r.id || "记录"} · ${r.status || r.verdict || r.kind || r.scope || ""}`, rows, "无记录。")); }
  }
  function renderRuns() {
    const s = state.task; $("permitList").replaceChildren(simpleList(r => `${r.id} · ${r.kind} · ${r.adapter?.adapter_id || "适配器未标识"}`, s?.permits, "尚无动作许可。"));
    const list = $("runList"); list.replaceChildren(); if (!s?.runs?.length) { list.className = "empty"; list.textContent = "尚无运行。"; } else { list.className = ""; for (const run of s.runs) { const card = node("article", "record"); card.append(node("h4", "", `${run.id} · ${run.kind} · ${run.status}`)); card.append(node("p", "", `许可 ${run.permit_id} · 输入哈希 ${run.input_hash || "未提供"} · 产物哈希 ${run.output_hash || "未提供"}`)); card.append(simpleList(x => `${x.path} · ${x.sha256}`, run.outputs, "无登记产物。")); if (["STARTING", "START_UNKNOWN", "RUNNING"].includes(run.status)) { const reconcile = node("button", "", "只读对账运行状态"); reconcile.disabled = !state.execution || state.busy; reconcile.addEventListener("click", async () => { try { await reconcileRun(run.id); } catch (error) { handleError(error); } }); card.append(reconcile); } if (["RUNNING", "START_UNKNOWN"].includes(run.status)) { const finish = node("button", "", "登记运行结束及产物哈希"); finish.addEventListener("click", () => { $("finishRun").value = run.id; $("finish").disabled = false; $("finishRun").focus(); }); card.append(finish); } list.append(card); } }
  }
  function updateDraft(result, original) {
    state.draft = result.draft; state.requestText = original; render();
    $("commitBox").hidden = result.consultation || result.complete !== true;
    $("notice").textContent = result.consultation
      ? "只读咨询已完成有界发现；没有创建任务或生产审批。"
      : result.complete === true
        ? "有界发现已结束。仍不代表语义穷尽；核对动态项与局限后，再明确保存任务。"
        : "发现预算已耗尽或仍有缺口；结果保留供查看，但不允许提交成新任务。";
  }
  function handleError(error) { notice(error.message + (error.status === 409 ? "。使用刷新读取最新版本。" : ""), "error"); }
  $("goalForm").addEventListener("submit", async e => { e.preventDefault(); const request = $("goalText").value.trim(); if (!request || state.busy) return; busy(true, "识别目标 → 检索来源 → 规划动态核查项…"); try { const result = await api("/api/discover", { request }); updateDraft(result, request); } catch (err) { handleError(err); } finally { busy(false); enableControls(); } });
  $("goalText").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("goalForm").requestSubmit(); } });
  $("commitDraft").addEventListener("click", async () => { try { const id = $("newId").value.trim(); if (!id) throw Error("需要任务 ID。"); const response = await act("/api/commit", { draft_id: state.draft?.id, task_id: id, request_id: state.requestId() }, "正在通过TaskService建档并保存计划…"); if (response?.state) { state.draft = null; acceptState(response.state); } } catch (e) { handleError(e); } });
  $("openForm").addEventListener("submit", async e => { e.preventDefault(); try { const result = await api("/api/command", { action: "status", task_id: taskId() }); state.draft = null; acceptState(result.state); notice("已读取当前任务。", ""); } catch (err) { handleError(err); } });
  $("newTask").addEventListener("click", () => { state.task = null; state.draft = null; state.selected = null; $("taskId").value = ""; $("version").textContent = "尚未建档"; render(); enableControls(); $("goalText").focus(); });
  $("refresh").addEventListener("click", async () => { try { const result = await api("/api/command", { action: "status", task_id: taskId() }); acceptState(result.state); notice("已刷新，正在显示最新revision/state_seq。", ""); } catch (err) { handleError(err); } });
  document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => { document.querySelectorAll(".tabs button").forEach(x => x.setAttribute("aria-pressed", x === b ? "true" : "false")); document.querySelectorAll(".tab").forEach(x => x.hidden = x.id !== `tab-${b.dataset.tab}`); }));
  $("review").addEventListener("click", async () => { if (!snap()) return; try { const kind = $("reviewKind").value; const args = {action: "review", ...revision(), kind}; if (kind === "validation") { args.spec = executionSpec(); if (args.spec.kind !== "validation") throw Error("先在阶段与运行中填写受限验证探针的精确动作。"); } const result = await act("/api/command", args, "正在请求语义审阅；模型不可用时保持未放行…"); if (kind === "validation" && result?.result?.id) $("execReview").value = result.result.id; } catch (error) { handleError(error); } });
  $("evaluate").addEventListener("click", async () => { if (!snap()) return; try { await act("/api/command", { action: "evaluate", ...revision(), stage: snap().current_stage }, "正在由TaskService重算当前阶段…"); } catch (_) {} });
  $("reevaluate").addEventListener("click", async () => { if (!snap()) return; try { const stage = $("evaluateStage").value; await act("/api/command", { action: "evaluate", ...revision(), stage }, `重评 ${stage}…`); } catch (_) {} });
  $("advance").addEventListener("click", async () => { const stages = ["PLAN","BUILD","CONFIG","EXEC","ANALYZE"]; const idx = stages.indexOf(snap()?.current_stage); const to = stages[idx + 1]; if (!to) return; if (!confirm(`将尝试消费当前 ${snap().current_stage} 凭据并推进到 ${to}。服务端会重新检查许可、凭据与版本。继续？`)) return; try { await act("/api/command", { action: "advance", ...revision(), to_stage: to }, "服务端校验并推进阶段…"); } catch (_) {} });
  $("complete").addEventListener("click", async () => { if (!confirm("服务端将核验ANALYZE结果链后完成任务；这不会改变失败结果。继续？")) return; try { await act("/api/command", { action: "complete", ...revision() }, "服务端核验完成条件…"); } catch (_) {} });
  function itemShape(form) {
    const fd = new FormData(form); const get = (k) => String(fd.get(k) || "").trim();
    const refs = (key) => get(key).split(",").map(x => x.trim()).filter(Boolean);
    const id = get("id") || state.requestId();
    const existing = (state.task?.plan?.items || []).find(x => x.id === state.pendingItem) || {};
    return { ...existing, allow_assumption: form.elements.allow_assumption.checked, id, question: get("question"), claim: get("claim"), rationale: get("rationale"), target_refs: [...form.querySelectorAll("input[name=target_refs]:checked")].map(x => x.value), owner_rule: get("owner_rule"), due_stage: get("due_stage"), origin_refs: refs("origin_refs"), applicability: { status: get("applicability"), reason: get("applicability_reason"), source_refs: refs("applicability_refs") }, claim_id: get("claim_id") || get("claim"), ...(get("claim_kind") ? { claim_kind: get("claim_kind") } : {}), ...(form.elements.allow_assumption.checked ? { allow_assumption: true } : {}), ...(get("required_sources") ? { required_sources: refs("required_sources") } : {}), ...(get("required").length ? { required: form.elements.required.checked } : {}) };
  }
  function openItem(existing = null) {
    $("editorForm").dataset.kind = "item"; state.pendingItem = existing?.id || null; const fields = $("editorFields"); fields.replaceChildren(); $("editorTitle").textContent = existing ? "编辑核查项" : "追加核查项";
    const add = (label, name, value = "", type = "text") => { const l = node("label", "", label); const input = type === "textarea" ? node("textarea") : node("input"); input.name = name; input.value = value; input.required = !["claim_id","origin_refs","applicability_refs","required_sources"].includes(name); if (input.tagName === "TEXTAREA") input.rows = name === "question" ? 3 : 2; fields.append(l,input); return input; };
    add("稳定 ID", "id", existing?.id || ""); add("需要回答的问题", "question", existing?.question || "", "textarea"); add("关联主张", "claim", existing?.claim || "", "textarea"); add("为什么需要核查", "rationale", existing?.rationale || "", "textarea");
    const goalTitle = node("label", "", "对应目标"); fields.append(goalTitle); const goals = state.task?.intent?.goals || state.draft?.intent?.goals || [];
    for (const goal of goals) { const label = node("label", "check", goal.text); const cb = node("input"); cb.type="checkbox"; cb.name="target_refs"; cb.value=goal.id; cb.checked=(existing?.target_refs||[]).includes(goal.id); label.prepend(cb); fields.append(label); }
    const owner = node("label", "", "唯一负责规则"); const selectOwner = node("select"); selectOwner.name="owner_rule"; selectOwner.required=true; for (const v of ["E1","E2","E3","E4","E5"]) opt(selectOwner,v,v); selectOwner.value=existing?.owner_rule||"E1"; fields.append(owner,selectOwner);
    const due = node("label", "", "最晚需要完成的阶段"); const selectDue = node("select"); selectDue.name="due_stage"; for (const v of ["PLAN","BUILD","CONFIG","EXEC","ANALYZE"]) opt(selectDue,v,v); selectDue.value=existing?.due_stage||"PLAN"; fields.append(due,selectDue);
    add("推导来源（逗号分隔）", "origin_refs", (existing?.origin_refs||[]).join(", ")); const app = node("label", "", "适用性"); const selectApp=node("select"); selectApp.name="applicability"; for(const v of ["applicable","unknown","inapplicable"])opt(selectApp,v,v); selectApp.value=existing?.applicability?.status||"unknown"; fields.append(app,selectApp); add("适用性理由", "applicability_reason", existing?.applicability?.reason||"", "textarea"); add("适用性来源（逗号分隔）", "applicability_refs", (existing?.applicability?.source_refs||[]).join(", ")); add("精确主张 ID（E2 需保持一致）", "claim_id", existing?.claim_id||"");
    const assLabel=node("label","check","允许作为设计假设（仍需精确人工授权）"); const ass=node("input"); ass.type="checkbox"; ass.name="allow_assumption"; ass.checked=existing?.allow_assumption===true; assLabel.prepend(ass); fields.append(assLabel);
    $("editorError").textContent=""; $("editor").showModal();
  }
  $("addItem").addEventListener("click", () => openItem());
  $("replan").addEventListener("click", async () => { const items = state.task?.plan?.items; if (!items) return; if (!confirm("将重新检索并重审当前任务核查项。检索来源、未知与排除会产生新结果，是否继续？")) return; try { const result = await act("/api/items", {...revision(), reason:"用户明确要求对当前动态核查计划重新检索、复审，保留本页面呈现的全部条目。", items}, "重新检索并由transport生成发现复审…"); if (result?.state) acceptState(result.state); } catch (_) {} });
  $("editorForm").addEventListener("submit", async e => { e.preventDefault(); if (e.currentTarget.dataset.kind === "evidence") { await saveEvidence(); return; } if (!state.task) { $("editorError").textContent = "先将计划保存为任务，再编辑核查项。"; return; } try { const item=itemShape(e.currentTarget); if (!item.target_refs.length) throw Error("至少关联一个任务目标。"); const items=[...(state.task.plan?.items||[])]; if(state.pendingItem){const i=items.findIndex(x=>x.id===state.pendingItem); if(i<0)throw Error("核查项已变化，请刷新。"); items[i]=item;}else items.push(item); const result=await api("/api/items",{...revision(),reason:`人工提交核查项 ${item.id}；模型必须保留该项并进行新的发现审查。`,items}); if(result.state)acceptState(result.state); $("editor").close(); notice("新计划已保存；此前凭据已由服务端失效，请重新确认和审阅。"); } catch(err) { $("editorError").textContent=err.message; } });
  $("closeEditor").addEventListener("click",()=>$("editor").close());
  function openEvidence(existing=null) {
    if (existing?.source_kind === "measurement") { notice("测量证据必须在阶段与运行页从原始产物重新计算，不能手填修正数值。"); document.querySelector('.tabs button[data-tab="execution"]').click(); $("measurementBox").open = true; $("measureRun").value = existing.run_id || ""; return; }
    $("editorForm").dataset.kind = "evidence";
    const fields=$("editorFields");fields.replaceChildren();$("editorTitle").textContent=existing?"追加修正证据":"追加证据材料";
    const add=(label,name,value="",required=true,area=false)=>{fields.append(node("label","",label));const e=area?node("textarea"):node("input");e.name=name;e.value=value;e.required=required;if(area)e.rows=3;fields.append(e);return e;};
    add("证据 ID（修正请用新 ID）","id",existing?"":state.requestId());fields.append(node("label","","来源类型（测量请使用原始产物重算表单）"));const kind=node("select");kind.name="source_kind";for(const value of ["human","publication","case","experience","estimate"])opt(kind,value,value);kind.value=existing?.source_kind||"human";fields.append(kind);
    add("主张 ID","claim_id",existing?.claim_id||currentItem()?.claim_id||currentItem()?.claim||""); add("本材料具体支持什么","content",existing?.content||"",false,true);add("来源定位（文件/公开引用 ID）","source_locator",existing?.verification?.source_locator||"",true);add("核验方式","verification_method",existing?.verification?.method||"content_review");add("核验材料 SHA-256（非测量可留空）","source_sha256",existing?.source_sha256||"",false);add("来源引用（逗号分隔）","provenance_refs",(existing?.provenance_refs||[]).join(", "),true);add("条件说明（普通文本）","conditions_text",existing?JSON.stringify(existing.conditions||{}):"",false,true);
    if(existing){add("修正所替代的旧记录 ID","supersedes",existing.id);}
    if(existing?.source_kind==="measurement"){for(const [label,name,value] of [["运行 ID","run_id",existing.run_id],["数值","value",existing.value],["指标","metric",existing.metric],["单位","unit",existing.unit],["窗口（JSON）","window",JSON.stringify(existing.window)],["容差","tolerance",existing.tolerance],["输入文件绝对定位","input_locator",existing.verification?.input_locator],["输入 SHA-256","input_hash",existing.input_hash],["产物 SHA-256","output_hash",existing.output_hash]])add(label,name,value??"");}
    $("editorError").textContent="";$("editor").showModal();
  }
  $("addEvidence").addEventListener("click",()=>openEvidence());
  async function saveEvidence(){try{const form=$("editorForm"),fd=new FormData(form),g=k=>String(fd.get(k)||"").trim();let conditions={};if(g("conditions_text")){conditions=JSON.parse(g("conditions_text"));if(!conditions||Array.isArray(conditions)||typeof conditions!=="object")throw Error("条件必须是对象。");}const refs=g("provenance_refs").split(",").map(x=>x.trim()).filter(Boolean);const record={id:g("id"),source_kind:g("source_kind"),claim_id:g("claim_id"),task_id:state.task.task_id,lifecycle:"active",conditions,provenance_refs:refs,verification:{status:"verified",method:g("verification_method"),source_locator:g("source_locator")}};if(g("content"))record.content=g("content");if(g("source_sha256"))record.source_sha256=g("source_sha256");if(g("supersedes"))record.supersedes=g("supersedes");if(record.source_kind==="measurement"){record.run_id=g("run_id");record.value=Number(g("value"));record.metric=g("metric");record.unit=g("unit");record.window=JSON.parse(g("window"));record.tolerance=Number(g("tolerance"));record.input_hash=g("input_hash");record.output_hash=g("output_hash");record.verification.method="recompute";record.verification.input_locator=g("input_locator");}const result=await api("/api/evidence",{...revision(),record});acceptState(result.state);$("editor").close();notice("证据已追加为不可变记录。后续E规则仍会自行核验来源、哈希和绑定。","");}catch(err){$("editorError").textContent=err.message;}}
  $("humanOperation").addEventListener("change",()=>{const selected=$("humanTarget").selectedOptions[0];if(selected&&$("humanOperation").value==="revoke"&&!selected.value.startsWith("authorization:"))notice("撤回操作需要选择既有授权记录。","error");});
  $("humanForm").addEventListener("submit",async e=>{e.preventDefault();if(!state.task)return;try{const operation=$("humanOperation").value, selected=$("humanTarget").value, [kind,id]=selected.split(":");if(operation==="revoke"){if(kind!=="authorization")throw Error("请选择需要撤回的既有授权。");await act("/api/human",{...revision(),operation,authorization_id:id,quote:$("humanQuote").value,actor_id:$("humanActor").value},"记录人工撤回并使受影响许可失效…");return;}if(["accept","dispute","correct","not_applicable"].includes(operation)&&kind!=="finding")throw Error("请选择一个问题报告。");if(operation==="assumption"&&kind!=="item")throw Error("选择核查项后才能请求允许的设计假设授权。");if(operation==="confirm"&&$("humanQuote").value.trim().length<3)throw Error("请填写你本人输入的确认原话。");const body={...revision(),operation,scope:operation==="confirm"?"plan":undefined,quote:$("humanQuote").value,actor_id:$("humanActor").value,finding_id:kind==="finding"?id:undefined,item_id:kind==="item"?id:undefined};await act("/api/human",body,"保存真人决定，不修改事实判断…");}catch(err){handleError(err);}});
  $("goalEditForm").addEventListener("submit",async e=>{e.preventDefault();try{const old=state.task.intent, texts=$("goalEdit").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);if(texts.length!==old.goals.length)throw Error("目前只允许按顺序改写既有目标；增删目标请创建新的任务，以免丢失来源映射。");const goals=old.goals.map((g,i)=>({...g,text:texts[i]}));const intent={...old,goals};await act("/api/command",{action:"revise",...revision(),patch:{intent,decisions:{human_scope_change:{reason:$("goalReason").value,quote:$("goalReason").value,source_refs:["webui-human:"+state.requestId()]}}},change_reason:$("goalReason").value,request_id:state.requestId()},"创建目标新版本并使旧审阅失效…");notice("目标已形成新版本。请重新检索并修订计划、重新确认与审阅。","error");}catch(err){handleError(err);}});
  $("inputForm").addEventListener("submit",async e=>{e.preventDefault();try{const inputs=parseRows($("inputManifest").value);await act("/api/command",{action:"revise",...revision(),patch:{dependencies:{cwd:$("inputCwd").value.trim(),inputs}},request_id:state.requestId()},"保存依赖版本（将使相关凭据过期）…");}catch(err){handleError(err);}});
  function executionSpec(){const command=$("execCommand").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean),inputs=parseRows($("execInputs").value),kind=$("execKind").value,spec={kind,command,cwd:$("execCwd").value.trim(),inputs,limits:{timeout_seconds:Number($("execTimeout").value)},environment:{}};if(kind==="validation"){spec.question_id=$("execQuestion").value.trim();spec.review_id=$("execReview").value.trim();}else if(kind==="production")spec.authorization_id=$("execAuthorization").value.trim();return spec;}
  $("authorizeHuman").addEventListener("click",async()=>{try{if(!$("execQuote").value.trim())throw Error("先输入你对这项精确动作的授权原话。");const kind=$("execKind").value;const authId=state.requestId();$("execAuthorization").value=authId;const spec=executionSpec();const authorization={id:authId,scope:kind,decision:"allow",actor_kind:"human",actor_id:$("execActor").value.trim(),reason:$("execQuote").value.trim(),quote:$("execQuote").value.trim(),event_id:state.requestId(),subject_hash:state.task.subject_hash,source_refs:["webui-human:"+state.requestId()],revoked:false};
      // The exact spec digest is computed by the trusted API when authorize is invoked below.
      const result=await api("/api/human",{...revision(),operation:"execution",scope:kind,authorization_id:authId,spec,actor_id:$("execActor").value.trim(),quote:$("execQuote").value.trim()});if(result.state)acceptState(result.state);notice(`已记录 ${kind} 动作授权。现在可申请一次性许可；授权本身不会启动。`);
    }catch(err){handleError(err);}});
  $("permit").addEventListener("click",async()=>{try{const spec=executionSpec();if(spec.kind==="production"&&!spec.authorization_id)throw Error("先记录对应的真人生产授权。");if(spec.kind==="validation"&&(!spec.question_id||!spec.review_id))throw Error("探针需要问题 ID 和真实成功审阅 ID。");const result=await act("/api/command",{action:"authorize",...revision(),spec},"由TaskService核验凭据、授权、适配器和冻结输入…");if(result.result?.id){$("permitList").prepend(node("p","receipt",`新许可 ${result.result.id} · ${result.result.kind} · ${result.result.spec_hash}`));} }catch(err){handleError(err);}});
  $("execKind").addEventListener("change",()=>{const prod=$("execKind").value==="production";$("execAuthorization").disabled=!prod;$("execQuestion").disabled=$("execKind").value!=="validation";$("execReview").disabled=$("execKind").value!=="validation";});
  function startRun(permit){state.pendingStart={permit,requestId:state.requestId(),authorizationId:state.requestId(),actor:null,quote:null};const spec=permit.spec;$("startPreview").textContent=JSON.stringify({kind:permit.kind,command:spec.command,cwd:spec.cwd,inputs:spec.inputs,limits:spec.limits,permit_id:permit.id,subject_hash:permit.subject_hash},null,2);$("startActor").value=$("execActor").value;$("startQuote").value="";$("startCheck").checked=false;$("startDialog").showModal();}
  function launchButtons(){for(const p of state.task?.permits||[]){if((state.task.runs||[]).some(r=>r.permit_id===p.id))continue;const b=node("button","danger",state.execution?"真人明确选择：实际启动":"启动未启用（默认安全）");b.disabled=!state.execution;b.addEventListener("click",()=>startRun(p));$("permitList").append(b);}}
  $("startForm").addEventListener("submit",async e=>{
    e.preventDefault(); if (!state.pendingStart || state.busy) return;
    if (!$("startCheck").checked || !$("startQuote").value.trim() || !$("startActor").value.trim()) { notice("需勾选并输入操作者与启动指令原话。", "error"); return; }
    const pending=state.pendingStart, p=pending.permit;
    const actor=$("startActor").value.trim(), quote=$("startQuote").value.trim();
    if (pending.actor && (pending.actor!==actor || pending.quote!==quote)) { notice("重试须保留原操作者与指令；取消后重新核对可创建新的授权。", "error"); return; }
    pending.actor=actor; pending.quote=quote; busy(true,"记录真人启动授权并核验单次许可…");
    try {
      // Refresh first so a response lost after authorization/start can be reconciled.
      const latest=await api("/api/command",{action:"status",task_id:snap().task_id}); acceptState(latest.state);
      const prior=snap().runs.find(r=>r.permit_id===p.id);
      if (prior) { $("startDialog").close(); state.pendingStart=null; notice(`该许可已有运行 ${prior.id}：${prior.status}。请核对运行与产物，不会再次启动。`); return; }
      const recorded=snap().authorizations.find(a=>a.id===pending.authorizationId);
      if (!recorded) { const auth=await api("/api/human",{...revision(),operation:"launch",authorization_id:pending.authorizationId,permit_id:p.id,spec:p.spec,actor_id:actor,quote}); acceptState(auth.state); pending.launchEventId=auth.launch_event_id; }
      else pending.launchEventId=recorded.id;
      const result=await api("/api/start",{...revision(),permit_id:p.id,spec:p.spec,launch_event_id:pending.launchEventId,request_id:pending.requestId,quote,actor_id:actor,confirm_start:true});
      acceptState(result.state); $("startDialog").close(); state.pendingStart=null;
      const run=result.result?.run || result.result;
      notice(`桥已返回运行状态 ${run?.status || "未知"}。若为 START_UNKNOWN，先对账，不可重跑。`);
    } catch(err) { handleError(err); } finally { busy(false); enableControls(); }
  });
  $("cancelStart").addEventListener("click",()=>{$("startDialog").close();state.pendingStart=null;});
  $("finishForm").addEventListener("submit",async e=>{e.preventDefault();try{const outputs=$("finishOutputs").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean).map(x=>{const p=x.split("|").map(y=>y.trim());if(p.length!==2||!/^([a-f0-9]{64})$/.test(p[1]))throw Error("产物格式应为绝对路径 | SHA-256");return {path:p[0],sha256:p[1]};});await act("/api/command",{action:"finish",...revision(),run_id:$("finishRun").value.trim(),result:{run_id:$("finishRun").value.trim(),returncode:Number($("finishCode").value),outputs}},"检查真实桥回执、退出码、输出目录与哈希…");}catch(err){handleError(err);}});
  function jsonObject(id) { const value=JSON.parse($(id).value || "{}"); if (!value || Array.isArray(value) || typeof value!=="object") throw Error("配方与条件必须是 JSON 对象。"); return value; }
  async function showAction(args,message) { const response=await act("/api/command",args,message); if(response) $("actionResult").textContent=JSON.stringify(response.result ?? response.state,null,2); return response; }
  async function reconcileRun(runId) { if (!snap()) throw Error("先读取当前任务。"); busy(true,"通过可信本机桥只读对账；不会重新启动…"); try { const result=await api("/api/reconcile",{...revision(),run_id:runId,receipt:{}}); acceptState(result.state); $("actionResult").textContent=JSON.stringify(result.result,null,2); notice(`对账返回运行状态 ${result.result?.status || "未知"}；未发起新启动。`); return result; } finally { busy(false); enableControls(); } }
  $("selectRunForm").addEventListener("submit",async e=>{e.preventDefault();try{const id=$("selectedRun").value.trim();if(!snap()?.runs.some(r=>r.id===id&&r.status==="FINISHED"))throw Error("请选择当前任务的 FINISHED 运行。");await showAction({action:"revise",...revision(),patch:{decisions:{...snap().decisions,selected_run_id:id}},request_id:state.requestId()},"绑定分析运行并使旧审阅失效…");$("measureRun").value=id;}catch(err){handleError(err);}});
  $("measureForm").addEventListener("submit",async e=>{e.preventDefault();try{await showAction({action:"measure",...revision(),run_id:$("measureRun").value.trim(),item_id:$("measureItem").value.trim(),evidence_id:$("measureEvidence").value.trim(),recipe:jsonObject("measureRecipe"),conditions:jsonObject("measureConditions")},"校验原始运行回执、产物哈希并重算测量…");}catch(err){handleError(err);}});
  $("refreshKnowledge").addEventListener("click",async()=>{try{await showAction({action:"refresh_knowledge",...revision(),request_id:state.requestId()},"刷新知识代次并重新检索核查计划…");}catch(err){handleError(err);}});
  $("inspectPublicationForm").addEventListener("submit",async e=>{e.preventDefault();try{await showAction({action:"inspect_publication",event_id:$("publicationEvent").value.trim()||undefined},"检查全局知识发布事务…");}catch(err){handleError(err);}});
  $("recoverPublicationForm").addEventListener("submit",async e=>{e.preventDefault();if(!confirm("将按所选决定恢复该发布事务。已终结事务不能改为相反决定。继续？"))return;try{await showAction({action:"recover_publication",event_id:$("recoverEvent").value.trim(),decision:$("recoverDecision").value,approver:$("recoverApprover").value.trim(),reason:$("recoverReason").value.trim()},"校验发布日志并执行恢复决定…");}catch(err){handleError(err);}});
  function launchButtonsAfterRender(){launchButtons();}
  $("editor").addEventListener("close",()=>{$("editorError").textContent="";});
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&$("startDialog").open)$("startDialog").close();});
  async function init(){try{const session=await api("/api/session");state.csrf=session.csrf;state.capabilities=new Set(session.capabilities);state.execution=session.execution_enabled;$("connection").textContent="已连接到本机证据服务";$("executionStatus").textContent=session.execution_enabled?"本地桥已启用；后端适配器逐次核验":"执行关闭：审阅功能可用，真实启动禁用";$("newId").value=`task-${new Date().toISOString().slice(0,10).replaceAll("-","")}-a`;enableControls();}catch(e){$("connection").textContent="本地服务不可用；请在隔离库启动 webui.server";notice(e.message,"error");}}
  const originalRender=render; render=()=>{originalRender();launchButtonsAfterRender();};
  init();
})();
