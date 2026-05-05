<script>
  import { inferStream, abortInferStream, agents as agentsApi, tools as toolsApi, sessions as sessionsApi } from '../../lib/api.js'
  import ModelSelector from './ModelSelector.svelte'
  import ToolSelector from './ToolSelector.svelte'
  import PromptTemplateSelector from './PromptTemplateSelector.svelte'
  import PlaceholderInputs from './PlaceholderInputs.svelte'
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import MessageList from './MessageList.svelte'
  import ChatInput from './ChatInput.svelte'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'
  import { sessionRestore, newSessionCreated } from '../../lib/session-state.svelte.js'

  const STORAGE_MODEL_KEY = 'chat_selected_model'
  const STORAGE_TOOLS_KEY = 'chat_selected_tools'

  let messages = $state([])
  let selectedModelId = $state(localStorage.getItem(STORAGE_MODEL_KEY) ?? '')
  let selectedToolIds = $state(JSON.parse(localStorage.getItem(STORAGE_TOOLS_KEY) ?? '[]'))
  let isStreaming = $state(false)
  let errorMsg = $state('')
  let inputText = $state('')
  let abortStream = $state(null)
  let sessionId = $state(null)   // maintained for the lifetime of this chat session

  // 提示词模板面板状态
  let templatePanelOpen = $state(false)
  // 面板内选中的模板 ID（关闭面板后保留选中状态）
  let selectedTemplateId = $state(null)
  // 当前面板内选中的模板内容（用于左侧展示）
  let panelSelectedResult = $state(null)  // null | { type: 'direct', content, template } | { type: 'template', template }
  // PlaceholderInputs 组件引用，用于顶栏按钮读取已填充的值
  let placeholderInputsRef = $state(null)
  // 系统提示词：纯文本或模板引用
  let systemPromptText = $state('')          // 纯文本形式
  let systemPromptTemplate = $state(null)    // { template_id, arguments } 形式，非 null 时优先使用

  // 添加为智能体状态
  let addAgentMode = $state(false)
  let addAgentNickname = $state('')
  let addAgentSaving = $state(false)

  // 智能体选择器状态
  let agentList = $state([])
  let selectedAgentId = $state('')
  let loadingAgents = $state(true)

  function openTemplatePanel() {
    templatePanelOpen = true
  }

  function closeTemplatePanel() {
    templatePanelOpen = false
  }

  function handleTemplatePanelSelect(result) {
    panelSelectedResult = result
  }

  /**
   * 顶栏"作为"按钮统一入口
   */
  function handleHeaderApply(mode) {
    if (!panelSelectedResult) return
    const tpl = panelSelectedResult.template
    const args = placeholderInputsRef ? placeholderInputsRef.getValues() : {}
    if (tpl) {
      if (mode === 'system') {
        handleApplyAsSystemTemplate(tpl.template_id, args)
      } else {
        handleApplyAsUserTemplate(tpl.template_id, args)
      }
    } else {
      const text = panelSelectedResult.content ?? ''
      if (mode === 'system') {
        handleApplyAsSystem(text)
      } else {
        handleApplyAsUserSend(text)
      }
    }
  }

  function handlePlaceholderApply(finalText, mode) {
    if (mode === 'system') {
      handleApplyAsSystem(finalText)
    } else {
      handleApplyAsUserSend(finalText)
    }
  }

  function handleApplyAsSystem(finalText) {
    systemPromptText = finalText
    systemPromptTemplate = null
    closeTemplatePanel()
  }

  function handleApplyAsSystemTemplate(templateId, args) {
    systemPromptTemplate = { template_id: templateId, arguments: args }
    systemPromptText = ''
    closeTemplatePanel()
  }

  function handleApplyAsUserTemplate(templateId, args) {
    closeTemplatePanel()
    handleSendTemplate(templateId, args)
  }

  function handleApplyAsUserSend(finalText) {
    closeTemplatePanel()
    handleSend(finalText)
  }

  function handleSend(text) {
    if (!selectedModelId && !selectedAgentId || isStreaming) return
    errorMsg = ''
    messages = [...messages, { role: 'user', content: text }]
    const apiMessages = []
    // Only send system message on the first request (no session yet)
    // On subsequent requests, the backend will restore it from session history
    if (!sessionId) {
      if (systemPromptTemplate) {
        apiMessages.push({ role: 'system', content: '', prompt_template: systemPromptTemplate.template_id, arguments: systemPromptTemplate.arguments })
      } else if (systemPromptText) {
        apiMessages.push({ role: 'system', content: systemPromptText })
      }
    }
    apiMessages.push({ role: 'user', content: text })
    _doSend(apiMessages)
  }

  function handleSendTemplate(templateId, args) {
    if (!selectedModelId && !selectedAgentId || isStreaming) return
    errorMsg = ''
    messages = [...messages, { role: 'user', content: '', prompt_template: templateId, arguments: args }]
    const apiMessages = []
    // Only send system message on the first request (no session yet)
    if (!sessionId) {
      if (systemPromptTemplate) {
        apiMessages.push({ role: 'system', content: '', prompt_template: systemPromptTemplate.template_id, arguments: systemPromptTemplate.arguments })
      } else if (systemPromptText) {
        apiMessages.push({ role: 'system', content: systemPromptText })
      }
    }
    apiMessages.push({ role: 'user', content: '', prompt_template: templateId, arguments: args })
    _doSend(apiMessages)
  }

  function _doSend(apiMessages) {
    isStreaming = true
    let aIdxRef = { value: messages.length }
    // 创建空assistant消息时带上当前选中的智能体ID
    const assistantMsg = { role: 'assistant', content: '', thinking: null }
    if (selectedAgentId) {
      assistantMsg.assistant_id = selectedAgentId
    }
    messages = [...messages, assistantMsg]
    const reqBody = { model_id: selectedModelId, tool_ids: selectedToolIds, messages: apiMessages, stream: true }
    if (selectedAgentId) {
      reqBody.agent_id = selectedAgentId
    }
    reqBody.session_id = sessionId ?? 'new'
    // 记录本次发送的第一条用户消息，用于新会话的临时标题
    const pendingFirstUserMsg = !sessionId
      ? (apiMessages.find(m => m.role === 'user')?.content || null)
      : null
    abortStream = inferStream(
      reqBody,
      (msg) => onStreamMsg(msg, aIdxRef, pendingFirstUserMsg),
      () => onStreamDone(),
      (err) => onStreamErr(err),
    )
  }

  function handleStop() {
    if (abortStream) {
      // 先通知后端 set cancel_event（覆盖 delegate 子推理期间无 SSE 写入的情况）
      if (sessionId) abortInferStream(sessionId)
      abortStream(); abortStream = null
    }
  }

  function onStreamMsg(msg, aIdxRef, pendingFirstUserMsg) {
    if (msg.session_id && !msg.role) {
      sessionId = msg.session_id
      // 通知 Sidebar 有新会话创建（仅当之前没有 sessionId 时才是新会话）
      if (!newSessionCreated.sessionId) {
        newSessionCreated.sessionId = msg.session_id
        newSessionCreated.firstUserMessage = pendingFirstUserMsg ?? null
      }
      return
    }
    if (msg.role === 'assistant') {
      if (aIdxRef.value === -1) {
        aIdxRef.value = messages.length
        // Inherit agent_nickname from the previous assistant message (if any)
        const prevAgent = [...messages].reverse().find(m => m.role === 'assistant' && m.agent_nickname)
        // 直接展开 msg 的所有字段，同时设置初始值
        messages = [...messages, { role: 'assistant', content: '', thinking: null, agent_nickname: prevAgent?.agent_nickname, ...msg }]
      }
      let u = [...messages]
      const aIdx = aIdxRef.value
      if (!u[aIdx]) return
      // 合并 msg 的所有字段，content 和 thinking 特殊处理（增量追加）
      const newMsg = { ...u[aIdx], ...msg }
      if (msg.content) newMsg.content = (u[aIdx].content || '') + msg.content
      if (msg.thinking) newMsg.thinking = (u[aIdx].thinking || '') + msg.thinking
      u[aIdx] = newMsg
      messages = u
    } else if (msg.role === 'tool') {
      if (msg.streaming === true) {
        // delegate 流式增量帧：找到对应 tool_call_id 的已有工具消息，追加 delta
        const delta = msg.delta || ''
        const tcId = msg.tool_call_id
        const existingIdx = tcId
          ? messages.findLastIndex(m => m.role === 'tool' && m.tool_call_id === tcId)
          : -1
        if (existingIdx >= 0) {
          const arr = [...messages]
          const newMsg = { ...arr[existingIdx], ...msg }
          newMsg.content = (arr[existingIdx].content || '') + delta
          arr[existingIdx] = newMsg
          messages = arr
        } else {
          // 第一帧：创建新的工具消息占位
          messages = [...messages, {
            role: 'tool',
            name: msg.name || '',
            content: delta,
            tool_call_id: tcId,
            streaming: true,
            ...msg
          }]
        }
        // 流式帧不重置 aIdxRef，让 assistant 消息继续累积
      } else if (msg.streaming === false) {
        // delegate 结束帧：标记流式消息框已完成
        const tcId = msg.tool_call_id
        const existingIdx = tcId
          ? messages.findLastIndex(m => m.role === 'tool' && m.tool_call_id === tcId)
          : -1
        if (existingIdx >= 0) {
          const arr = [...messages]
          // 只更新 streaming 状态，不覆盖 content（内容已通过流式增量帧完整推送）
          const newMsg = { ...arr[existingIdx], ...msg, streaming: false }
          // 如果结束帧携带了 content（非空），才更新
          if (msg.content) {
            newMsg.content = msg.content
          }
          arr[existingIdx] = newMsg
          messages = arr
        } else if (msg.content) {
          messages = [...messages, { role: 'tool', ...msg }]
        }
        aIdxRef.value = -1
      } else {
        // 普通工具结果帧（bash、fetch 等）
        messages = [...messages, { role: 'tool', ...msg }]
        aIdxRef.value = -1
      }
    } else if (msg.role === 'system') {
      aIdxRef.value = -1
    } else if (msg.role === 'usage') {
      try {
        const s = JSON.parse(msg.content || '{}')
        const lastAIdx = messages.map((m, i) => m.role === 'assistant' ? i : -1).filter(i => i >= 0).pop()
        if (lastAIdx !== undefined) {
          const arr = [...messages]
          arr[lastAIdx] = { ...arr[lastAIdx], stat: s }
          messages = arr
        }
      } catch (_) {}
    }
  }

  function onStreamDone() {
    isStreaming = false
    abortStream = null
    if (messages.length > 0) {
      const last = messages[messages.length - 1]
      if (last.role === 'assistant' && !last.content && !last.thinking && !last.tool_calls) {
        messages = messages.slice(0, -1)
      }
    }
  }

  function onStreamErr(err) {
    isStreaming = false
    abortStream = null
    errorMsg = err?.message || t('streamError')
  }

  async function handleRevoke(timestamp) {
    if (!sessionId) return
    // 弹出确认对话框
    if (!confirm(t('confirmRevoke'))) {
      return
    }
    // 查找被撤回的用户消息内容
    const revokeIndex = messages.findIndex(m => m.timestamp === timestamp)
    const revokedMessage = revokeIndex >= 0 ? messages[revokeIndex] : null
    // 向后端发送撤回请求
    try {
      await sessionsApi.revoke(sessionId, timestamp)
      // 后端操作成功后，才从前端移除消息并填入输入框
      if (revokeIndex >= 0) {
        messages = messages.slice(0, revokeIndex)
      }
      if (revokedMessage?.content) {
        inputText = revokedMessage.content
      }
    } catch (err) {
      console.error('Failed to revoke message:', err)
    }
  }

  $effect(() => {
    if (sessionRestore.pending) {
      const { sessionId: sid, messages: msgs } = sessionRestore.pending
      sessionRestore.pending = null
      messages = msgs
      sessionId = sid
      errorMsg = ''
      // 检查最后一条assistant消息的assistant_id，设置智能体选择框
      const lastAssistantMsg = [...msgs].reverse().find(m => m.role === 'assistant')
      if (lastAssistantMsg?.assistant_id) {
        selectedAgentId = lastAssistantMsg.assistant_id
        localStorage.setItem('chat_selected_agent', lastAssistantMsg.assistant_id)
      }
    }
  })

  // 加载智能体列表
  async function fetchAgents() {
    loadingAgents = true
    try {
      const data = await agentsApi.list()
      agentList = data.agents ?? []
    } catch {
      agentList = []
    } finally {
      loadingAgents = false
    }
  }

  $effect(() => { fetchAgents() })

  function handleAgentChange(e) {
    selectedAgentId = e.target.value
    if (selectedAgentId) {
      localStorage.setItem('chat_selected_agent', selectedAgentId)
    }
  }

  // 生成添加智能体的默认昵称
  function generateAgentDefaultNickname() {
    const model = selectedModelId || 'unknown'
    const toolCount = selectedToolIds.length
    const toolSummary = toolCount > 0 ? `+${toolCount}tools` : ''
    const tpl = systemPromptTemplate ? systemPromptTemplate.template_id : ''
    return `${model}${toolSummary}${tpl ? '+' + tpl : ''}`
  }

  function openAddAgent() {
    addAgentNickname = generateAgentDefaultNickname()
    addAgentMode = true
  }

  function cancelAddAgent() {
    addAgentMode = false
    addAgentNickname = ''
  }

  async function confirmAddAgent() {
    if (!addAgentNickname.trim() || addAgentSaving) return
    addAgentSaving = true
    try {
      // 获取选中工具的详细信息用于描述
      const toolsData = await toolsApi.list()
      const toolNames = (toolsData.tools ?? [])
        .filter(t => selectedToolIds.includes(t.tool_id))
        .map(t => t.name)

      const payload = {
        model_id: selectedModelId,
        tool_ids: selectedToolIds,
        template_id: systemPromptTemplate?.template_id ?? null,
        template_arguments: systemPromptTemplate?.arguments ?? {},
        system_prompt: systemPromptText,
        nickname: addAgentNickname.trim(),
        myself_view: '',
        description: `Model: ${selectedModelId}, Tools: ${toolNames.join(', ') || 'none'}`,
      }
      await agentsApi.create(payload)
      addAgentMode = false
      addAgentNickname = ''
      // 重新加载智能体列表
      fetchAgents()
    } catch (err) {
      errorMsg = err.message || t('addAsAgentFailed')
    } finally {
      addAgentSaving = false
    }
  }

  function handleAgentNicknameFocus(e) {
    // 当用户点击输入框时，选中全部文本，方便编辑
    e.target.select()
  }
</script>

<div class="chat-page">
  <div class="selection-bar">
    <div class="selector-wrapper" class:disabled={!!selectedAgentId}>
      📦<a href="#/setup?tab=models" class="nav-link">{t('modelLabel')}</a>
      <ModelSelector bind:selectedModelId onchange={(id) => localStorage.setItem(STORAGE_MODEL_KEY, id)} disabled={!!selectedAgentId} />
    </div>
    <div class="selector-wrapper" class:disabled={!!selectedAgentId}>
      🔧<a href="#/setup?tab=tools" class="nav-link">{t('tools')}</a>
      <ToolSelector bind:selectedToolIds onchange={(ids) => localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(ids))} disabled={!!selectedAgentId} />
    </div>
    <div class="agent-selector-spacer"></div>
    <div class="agent-selector">
      🤖<a href="#/setup?tab=agents" class="nav-link">{t('agentSelector')}</a>
      {#if loadingAgents}
        <span class="hint">{t('loading')}</span>
      {:else}
        <select id="agent-select" value={selectedAgentId} onchange={handleAgentChange}>
          <option value="">—</option>
          {#each agentList as a (a.agent_id)}
            <option value={a.agent_id}>{a.nickname}{a.myself_view ? ' (' + a.myself_view + ')' : ''}</option>
          {/each}
        </select>
      {/if}
    </div>
  </div>
  {#if systemPromptTemplate || systemPromptText}
    <div class="system-prompt-bar">
      <span class="sp-label">{t('systemPromptLabel')}</span>
      {#if systemPromptTemplate}
        <span class="sp-text sp-template">{systemPromptTemplate.template_id}</span>
      {:else}
        <span class="sp-text">{systemPromptText.length > 80 ? systemPromptText.slice(0, 80) + '...' : systemPromptText}</span>
      {/if}
      <div class="sp-right-section">
        {#if !addAgentMode}
          <a class="sp-add-agent" onclick={openAddAgent}>{t('addAsAgent')}</a>
        {/if}
        {#if addAgentMode}
          <input
            class="sp-nickname-input"
            type="text"
            value={addAgentNickname}
            oninput={(e) => addAgentNickname = e.target.value}
            onfocus={handleAgentNicknameFocus}
            placeholder={t('addAsAgentNickname')}
            disabled={addAgentSaving}
          />
          <button class="sp-confirm" onclick={confirmAddAgent} disabled={addAgentSaving || !addAgentNickname.trim()}>
            {addAgentSaving ? t('submitting') : t('addAsAgentConfirm')}
          </button>
          <button class="sp-cancel" onclick={cancelAddAgent}>✕</button>
        {/if}
        <button class="sp-clear" onclick={() => { systemPromptText = ''; systemPromptTemplate = null; addAgentMode = false }}>✕</button>
      </div>
    </div>
  {/if}
  {#if errorMsg}
    <div class="error-bar">{errorMsg}</div>
  {/if}

  <div class="message-area">
    <MessageList {messages} {agentList} onRevoke={handleRevoke} />

    {#if templatePanelOpen}
      <div class="template-panel">
        <div class="panel-header">
          <!-- 标题 -->
          📝<a href="#/setup?tab=prompts" class="nav-link">{t('promptTemplatePanelTitle')}</a>
          {#if panelSelectedResult}
            <!-- 占位符标签列表紧跟在标题后面 -->
            {#each extractPlaceholders(panelSelectedResult.template?.content ?? '') as ph}
              <span class="header-placeholder-tag">{ph}</span>
            {/each}
          {/if}
          <!-- 撑满中间空间，右对齐区域 -->
          <div class="header-apply-row">
            {#if panelSelectedResult}
              <a href="#/setup?tab=prompt-edit&templateId={panelSelectedResult.template?.template_id}" class="nav-link">{panelSelectedResult.template?.template_id ?? ''}</a>
              <span class="apply-as-label">{t('applyAs')}</span>
              <button class="btn btn-secondary" onclick={() => handleHeaderApply('system')}>{t('applyAsSystem')}</button>
              <button class="btn btn-primary" onclick={() => handleHeaderApply('user')}>{t('applyAsUserSend')}</button>
            {/if}
          </div>
          <button class="panel-close" onclick={closeTemplatePanel}>✕</button>
        </div>
        <div class="panel-body">
          <!-- 左侧：模板内容预览 + 占位符输入 -->
          <div class="panel-left">
            {#if panelSelectedResult}
              {#if panelSelectedResult.type === 'template'}
                <PlaceholderInputs
                  bind:this={placeholderInputsRef}
                  template={panelSelectedResult.template}
                  onApply={handlePlaceholderApply}
                />
              {:else}
                <div class="direct-preview">
                  <MarkdownRenderer content={panelSelectedResult.content ?? panelSelectedResult.template?.content ?? ''} />
                </div>
              {/if}
            {:else}
              <div class="panel-left-empty">
                <span>{t('selectTemplatePlaceholder')}</span>
              </div>
            {/if}
          </div>
          <!-- 右侧：模板列表 -->
          <div class="panel-right">
            <PromptTemplateSelector
              bind:selectedTemplateId
              onSelect={handleTemplatePanelSelect}
            />
          </div>
        </div>
      </div>
    {/if}
  </div>

  <ChatInput
    disabled={isStreaming || (!selectedModelId && !selectedAgentId)}
    onSend={handleSend}
    onStop={handleStop}
    onOpenTemplatePanel={openTemplatePanel}
    {isStreaming}
    bind:text={inputText}
    onNewSession={() => { messages = []; errorMsg = ''; sessionId = null }}
    hasMessages={messages.length > 0}
  />
</div>

<style>
  .chat-page {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
  }
  .selection-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    padding: 12px;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    align-items: center;
  }
  .selector-wrapper {
    display: flex;
    align-items: center;
    gap: 8px;
    transition: opacity 0.2s;
  }
  .selector-wrapper.disabled {
    opacity: 0.5;
    pointer-events: none;
  }
  .nav-link {
    font-size: 0.85rem;
    font-weight: 600;
    color: inherit;
    text-decoration: none;
    white-space: nowrap;
  }
  .nav-link:hover {
    color: var(--primary);
    text-decoration: underline;
  }
  .agent-selector-spacer { flex: 1; }
  .agent-selector { display: flex; align-items: center; gap: 8px; }
  .agent-selector select {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.9rem;
    min-width: 180px;
  }
  .agent-selector .hint { font-size: 0.8rem; color: var(--text-secondary); }
  .system-prompt-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    font-size: 0.8rem;
  }
  .sp-label { font-weight: 600; color: var(--text-secondary); white-space: nowrap; }
  .sp-text { color: var(--text); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sp-text.sp-template { color: var(--primary); font-family: monospace; font-size: 0.85rem; }
  .sp-clear { background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 0.9rem; padding: 2px 6px; border-radius: 4px; }
  .sp-clear:hover { background: var(--border); }
  .sp-right-section { display: flex; align-items: center; gap: 8px; }
  .sp-add-agent { color: var(--primary); cursor: pointer; text-decoration: underline; font-size: 0.8rem; white-space: nowrap; }
  .sp-add-agent:hover { color: var(--primary-hover); }
  .sp-nickname-input {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.8rem;
    font-family: monospace;
    width: 200px;
  }
  .sp-nickname-input:focus {
    border-color: var(--primary);
    color: var(--text);
    outline: none;
  }
  .sp-confirm {
    background: var(--primary);
    color: #fff;
    border: none;
    padding: 2px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
  }
  .sp-confirm:disabled { opacity: 0.5; cursor: not-allowed; }
  .sp-cancel { background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 0.9rem; padding: 2px 6px; border-radius: 4px; }
  .sp-cancel:hover { background: var(--border); }
  .error-bar { padding: 8px 12px; background: var(--danger); color: #fff; font-size: 0.85rem; }

  .message-area {
    flex: 1;
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .template-panel {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 75%;
    background: var(--bg);
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.12);
    z-index: 10;
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    flex-wrap: nowrap;
    overflow: hidden;
  }
  .header-placeholder-tag {
    display: inline-block;
    padding: 1px 7px;
    background: var(--primary);
    color: #fff;
    border-radius: 4px;
    font-size: 0.78rem;
    font-family: monospace;
    white-space: nowrap;
    flex-shrink: 0;
  }
  /* "作为"操作区：始终撑满中间空间，右对齐，关闭按钮因此始终固定在最右 */
  .header-apply-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    justify-content: flex-end;
    min-width: 0;
  }
  .apply-as-label {
    font-size: 0.85rem;
    color: var(--text-secondary);
    font-weight: 500;
    white-space: nowrap;
  }
  .panel-close {
    background: none;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 1rem;
    padding: 2px 6px;
    border-radius: 4px;
    line-height: 1;
    flex-shrink: 0;
  }
  .panel-close:hover { background: var(--border); color: var(--text); }

  .panel-body {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* 左侧：占剩余宽度，有自己的滚动条 */
  .panel-left {
    flex: 1;
    overflow-y: auto;
    min-width: 0;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    border-right: 1px solid var(--border);
  }

  .panel-left-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  /* 右侧：宽度随内容自然生长，有自己的滚动条 */
  .panel-right {
    min-width: 160px;
    max-width: 280px;
    width: max-content;
    flex-shrink: 0;
    overflow-y: auto;
    padding: 8px;
    background: var(--bg-secondary);
  }

  .direct-preview {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .btn { padding: 6px 12px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.85rem; white-space: nowrap; flex-shrink: 0; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-secondary {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-secondary:hover { background: var(--bg-secondary); }
</style>
