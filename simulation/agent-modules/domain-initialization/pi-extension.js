import { randomUUID } from 'node:crypto';
import { writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { operationSchemas } from './index.js';
import { DOMAIN_INITIALIZATION_GUIDANCE } from './guidance.js';
import { readLibraryMaterial } from './materials.js';
import { checkLibraryContent } from './privacy.js';
import { requireThat } from '../core/index.js';

const agentOperations = ['create', 'list', 'status', 'guide', 'source', 'readSource', 'propose', 'query', 'research'];
export const domainInitializationToolNames = Object.freeze(['domain_library', 'domain_library_interview', 'domain_library_review']);
export function withDomainInitializationPolicy(options) {
  return { ...options, policy: { ...options.policy,
    allowTools: [...new Set([...(options.policy?.allowTools || []), ...domainInitializationToolNames])] } };
}
const result = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }], details: value });
const humanActor = { actorId: 'domain-library-user', actorInstanceId: 'domain-library-user-interaction', kind: 'human' };
const agentActor = { actorId: 'domain-library-agent', actorInstanceId: 'domain-library-agent-session', kind: 'agent' };

export function domainInitializationExtension({ host, directory, python = 'python' }) {
  return async pi => {
    let activeLibraryId;
    const activate = () => pi.setActiveTools?.([...new Set([...(pi.getActiveTools?.() || []), ...domainInitializationToolNames])]);
    const invoke = (operation, input, actor = agentActor) => host.call('domain-initialization', operation, input,
      { actor, requestId: randomUUID() });
    const display = value => pi.sendMessage({ customType: 'domain-library', display: true, content: JSON.stringify(value, null, 2) });
    const continueGuidance = libraryId => pi.sendMessage({ customType: 'domain-library-next', display: false,
      content: `继续领域库初始化 ${libraryId}。先读取guide和已登记资料，基于用户材料整理有依据的候选，再围绕当前缺口询问最多1–3个问题。` }, { triggerTurn: true });
    const selectLibrary = async context => {
      const { libraries } = await invoke('list', {});
      if (!libraries.length) return undefined;
      const options = libraries.map(item => `${item.title} · ${item.libraryId}`);
      const chosen = await context.ui.select('选择领域库', options);
      return libraries[options.indexOf(chosen)]?.libraryId;
    };
    const selectActive = libraryId => {
      activeLibraryId = libraryId;
      activate();
      pi.appendEntry?.('domain-library-selection', { libraryId });
    };
    const interview = async (libraryId, context) => {
      const status = await invoke('status', { libraryId });
      if (!context?.hasUI) return { status: 'userInputRequired', libraryId, questions: status.nextQuestions };
      if (!status.nextQuestions.length) return { status: 'noPendingQuestions', libraryId };
      const options = status.nextQuestions.map(item => item.prompt);
      const chosen = options.length === 1 ? options[0] : await context.ui.select('本轮补充哪个问题', options);
      const question = status.nextQuestions[options.indexOf(chosen)];
      if (!question) return { status: 'cancelled' };
      const answer = await context.ui.input(question.prompt, '可输入具体经验、资料说明、不知道或暂缓');
      if (answer === undefined || !answer.trim()) return { status: 'cancelled' };
      checkLibraryContent(answer);
      const state = /^(暂缓|稍后|defer)$/i.test(answer.trim()) ? 'deferred'
        : /^(不知道|未知|暂时没有|unknown)$/i.test(answer.trim()) ? 'unknown' : 'answered';
      return invoke('answer', { libraryId, questionKey: question.key, answer, status: state,
        expectedRevision: status.revision, operationId: randomUUID() }, humanActor);
    };
    const review = async (libraryId, context, batch = false) => {
      const status = await invoke('status', { libraryId });
      const proposals = status.proposals.filter(item => ['pending', 'deferred'].includes(item.status));
      if (!context?.hasUI) return { status: 'userReviewRequired', libraryId, proposals,
        command: `/domain-init review ${libraryId}` };
      if (!proposals.length) return { status: 'noPendingProposals', libraryId };
      if (batch) {
        display({ batchReviewSubjects: proposals, revision: status.revision });
        const action = await context.ui.select('批次审阅当前候选及其条件/标签/来源', ['全部采用为项目实践', '全部采用为包设计候选', '全部暂缓', '全部拒绝']);
        if (!action) return { status: 'cancelled' };
        const verdict = action === '全部暂缓' ? 'defer' : action === '全部拒绝' ? 'reject' : 'adopt';
        const intendedUse = action === '全部采用为包设计候选' ? 'packCandidate' : 'projectPractice';
        return invoke('decideMany', { libraryId, expectedRevision: status.revision, operationId: randomUUID(),
          decisions: proposals.map(proposal => ({ proposalId: proposal.id, subjectDigest: proposal.subjectDigest, verdict, intendedUse })) }, humanActor);
      }
      const labels = proposals.map(item => `${item.statement.slice(0, 100)} · ${item.id}`);
      const chosen = await context.ui.select('选择待审知识候选', labels);
      const proposal = proposals[labels.indexOf(chosen)];
      if (!proposal) return { status: 'cancelled' };
      display({ reviewSubject: proposal, note: '采用将记录用途，认识状态继续按证据保存。' });
      const action = await context.ui.select('审阅候选及标签', ['采用为项目实践', '采用为包设计候选', '采用为参考资料', '修改候选', '暂缓', '拒绝']);
      if (!action) return { status: 'cancelled' };
      if (action === '修改候选') {
        const edit = (title, previous) => context.ui.editor ? context.ui.editor(title, previous)
          : context.ui.input(`${title}（留空保留原内容）`, previous).then(value => value === '' ? previous : value);
        const statement = await edit('修订后的完整表述', proposal.statement);
        if (!statement?.trim()) return { status: 'cancelled' };
        const conditions = await edit('适用条件，使用分号分隔', proposal.conditions.join('；'));
        if (conditions === undefined) return { status: 'cancelled' };
        const tags = await edit('标签，使用分号分隔', proposal.tags.join('；'));
        if (tags === undefined) return { status: 'cancelled' };
        const snapshot = await invoke('snapshot', { libraryId }, humanActor);
        const fragments = proposal.sourceRefs.map(reference => snapshot.bundle.records.find(item => item.id === reference.id && item.revision === reference.revision));
        const sourceIds = fragments.map(fragment => fragment.data.sourceRef.id);
        const evidenceRanges = fragments.filter(fragment => fragment.data.location.kind === 'line').map(fragment => {
          const [startLine, endLine] = fragment.data.location.value.split('-').map(Number);
          return { sourceId: fragment.data.sourceRef.id, startLine, endLine };
        });
        return invoke('propose', { libraryId, replaceProposalId: proposal.id, statement, kind: proposal.kind,
          sourceIds, evidenceRanges, conditions: conditions.split(/[;；]/).map(x => x.trim()).filter(Boolean),
          tags: tags.split(/[;；]/).map(x => x.trim()).filter(Boolean), exceptions: proposal.exceptions,
          expectedRevision: status.revision, operationId: randomUUID() }, humanActor);
      }
      const intendedUse = action === '采用为包设计候选' ? 'packCandidate' : action === '采用为参考资料' ? 'referenceOnly' : 'projectPractice';
      return invoke('decide', { libraryId, proposalId: proposal.id, subjectDigest: proposal.subjectDigest,
        verdict: action === '暂缓' ? 'defer' : action === '拒绝' ? 'reject' : 'adopt', intendedUse,
        expectedRevision: status.revision, operationId: randomUUID() }, humanActor);
    };
    const registerSource = async (libraryId, context) => {
      const type = await context.ui.select('资料类型', ['行业案例', '行业/企业规范', '亲历经验', '工具手册', '其它资料']);
      if (!type) return { status: 'cancelled' };
      const title = await context.ui.input('资料标题');
      if (!title?.trim()) return { status: 'cancelled' };
      const mode = await context.ui.select('怎样注入资料', ['直接填写或粘贴摘录', '选择本机文件']);
      if (!mode) return { status: 'cancelled' };
      let material;
      if (mode === '选择本机文件') {
        const path = await context.ui.input('材料文件路径', 'UTF-8文本、CSV、JSON、YAML、日志或PDF');
        if (!path?.trim()) return { status: 'cancelled' };
        material = await readLibraryMaterial(path, { python });
      } else {
        const content = context.ui.editor ? await context.ui.editor('案例、条款或经验原文', '') : await context.ui.input('案例、条款或经验原文');
        if (!content?.trim()) return { status: 'cancelled' };
        material = { content };
      }
      return invoke('source', { libraryId, title, sourceType: { '行业案例': 'caseReport', '行业/企业规范': 'standard',
        '亲历经验': 'userAccount', '工具手册': 'manual', '其它资料': 'other' }[type], ...material, operationId: randomUUID() }, humanActor);
    };
    pi.registerTool({ name: 'domain_library', label: '领域库初始化',
      description: 'Create or resume a user-guided domain library, register de-identified sources, propose sourced knowledge/tags, query adopted knowledge or prepare a scholar-style research brief. User adoption is handled by the interactive review tool.',
      parameters: { type: 'object', additionalProperties: false,
        properties: { ...Object.assign({}, ...agentOperations.map(name => operationSchemas[name].properties)), action: { enum: agentOperations } }, required: ['action'] },
      execute: async (toolCallId, args) => {
        const { action, ...input } = args;
        if (['create', 'source', 'propose'].includes(action)) input.operationId ||= toolCallId;
        const output = await invoke(action, input);
        if (output.libraryId) selectActive(output.libraryId);
        return result(output);
      } });
    for (const [name, operation, description] of [
      ['domain_library_interview', interview, 'Ask the user one current domain-library question through the host UI and save their direct answer.'],
      ['domain_library_review', review, 'Show the user a sourced candidate and tags through the host UI, and record their scoped adoption, edit, defer or reject decision.']
    ]) pi.registerTool({ name, label: name === 'domain_library_interview' ? '领域经验访谈' : '审阅领域知识', description,
      parameters: name === 'domain_library_review' ? { ...operationSchemas.status,
        properties: { ...operationSchemas.status.properties, batch: { type: 'boolean' } } } : operationSchemas.status,
      execute: async (_, args, _signal, _onUpdate, context) => result(await operation(args.libraryId, context, args.batch)) });

    pi.on('session_start', async (_, context) => {
      const entries = context.sessionManager?.getBranch?.() || [];
      const selection = entries.filter(item => item.type === 'custom' && item.customType === 'domain-library-selection').at(-1);
      activeLibraryId = selection?.data?.libraryId;
    });
    pi.on('before_agent_start', async event => {
      const prompt = event.messageType === 'user' ? event.prompt : event.message?.content || '';
      if (!activeLibraryId && !/领域库|领域包|domain.?init|初始化知识/i.test(String(prompt))) return;
      activate();
      return { systemPrompt: `${event.systemPrompt}\n${DOMAIN_INITIALIZATION_GUIDANCE}\n当前领域库引用 ${activeLibraryId || '尚未选定，可先list或create。'}` };
    });
    pi.registerCommand('domain-init', { description: '初始化领域库、注入行业案例/规范、访谈、审阅和恢复',
      handler: async (args, context) => {
        requireThat(context.hasUI, 'USER_INPUT_REQUIRED', 'Open the interactive Pi session to use this command');
        const [action, requestedId] = args.trim().split(/\s+/);
        try {
          if (action === 'new') {
            const title = await context.ui.input('领域库名称', '如结构动力学、蛋白质小分子对接');
            if (!title?.trim()) return;
            const objective = await context.ui.input('先支持什么任务，结果用于什么判断');
            if (!objective?.trim()) return;
            const created = await invoke('create', { title, objective, operationId: randomUUID() }, humanActor);
            selectActive(created.libraryId);
            display(created);
            continueGuidance(created.libraryId);
            return;
          }
          if (action === 'list') { display(await invoke('list', {})); return; }
          const libraryId = requestedId || activeLibraryId || await selectLibrary(context);
          if (!libraryId) { context.ui.notify('使用 /domain-init new 创建领域库。', 'info'); return; }
          selectActive(libraryId);
          let operation = action;
          if (!operation) {
            const choice = await context.ui.select('领域库初始化', ['继续访谈', '注入资料', '审阅候选', '查看状态', '启动领域调研', '导出快照', '暂停', '恢复']);
            operation = { '继续访谈': 'answer', '注入资料': 'source', '审阅候选': 'review', '查看状态': 'status',
              '启动领域调研': 'research', '导出快照': 'export', '暂停': 'pause', '恢复': 'resume' }[choice];
            if (!operation) return;
          }
          if (operation === 'answer') display(await interview(libraryId, context));
          else if (operation === 'source') {
            const output = await registerSource(libraryId, context);
            display(output);
            if (output.sourceId) continueGuidance(libraryId);
          }
          else if (operation === 'review') display(await review(libraryId, context));
          else if (operation === 'review-all') display(await review(libraryId, context, true));
          else if (operation === 'research') {
            const brief = await invoke('research', { libraryId });
            pi.sendMessage({ customType: 'domain-library-research', display: true,
              content: `请按当前领域库的缺口开展资料研究并提交有来源的候选。\n${JSON.stringify(brief)}` }, { triggerTurn: true });
          } else if (operation === 'export') {
            const snapshot = await invoke('snapshot', { libraryId }, humanActor);
            const exportDirectory = join(directory, 'exports');
            await mkdir(exportDirectory, { recursive: true });
            const file = join(exportDirectory, `${libraryId}-${snapshot.revision}-${randomUUID()}.json`);
            await writeFile(file, JSON.stringify(snapshot, null, 2), { flag: 'wx', mode: 0o600 });
            display({ libraryId, snapshotDigest: snapshot.snapshotDigest, exportPath: file });
          } else if (['pause', 'resume'].includes(operation)) display(await invoke('pause', { libraryId, paused: operation === 'pause' }, humanActor));
          else if (['status', 'guide'].includes(operation)) display(await invoke('status', { libraryId }));
          else context.ui.notify('可用 new、list、status、answer、source、review、review-all、research、export、pause、resume。', 'info');
        } catch (error) {
          const explanation = { PERSONAL_DATA_RESTRICTED: '请提供去除联系方式与凭据的材料片段。',
            REVISION_CONFLICT: '资料已变化，请刷新后重新检查。', EXTRACTION_UNAVAILABLE: '请核对提取工具，或直接提供可读摘录。',
            DECISION_SCOPE_MISMATCH: '候选已变化，请重新查看原文、条件和用途。' }[error.code] || '请核对当前状态与资料。';
          context.ui.notify(`领域库操作未完成（${error.code || 'OPERATION_FAILED'}）。${explanation}`, 'error');
        }
      } });
  };
}
