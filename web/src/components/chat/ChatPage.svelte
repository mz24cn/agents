<script>
  import { inferStream } from '../../lib/api.js'
  import ModelSelector from './ModelSelector.svelte'
  import ToolSelector from './ToolSelector.svelte'
  import PromptTemplateSelector from './PromptTemplateSelector.svelte'
  import PlaceholderInputs from './PlaceholderInputs.svelte'
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import MessageList from './MessageList.svelte'
  import ChatInput from './ChatInput.svelte'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'
  import { sessionRestore } from '../../lib/session-state.svelte.js'

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
    if (!selectedModelId || isStreaming) return
    errorMsg = ''
    messages = [...messages, { role: 'user', content: text }]
    const apiMessages = []
    if (systemPromptTemplate) {
      apiMessages.push({ role: 'system', content: '', prompt_template: systemPromptTemplate.template_id, arguments: systemPromptTemplate.arguments })
    } else if (systemPromptText) {
      apiMessages.push({ role: 'system', content: systemPromptText })
    }
    apiMessages.push({ role: 'user', content: text })
    _doSend(apiMessages)
  }

  function handleSendTemplate(templateId, args) {
    if (!selectedModelId || isStreaming) return
    errorMsg = ''
    messages = [...messages, { role: 'user', content: '', prompt_template: templateId, arguments: args }]
    const apiMessages = []
    if (systemPromptTemplate) {
      apiMessages.push({ role: 'system', content: '', prompt_template: systemPromptTemplate.template_id, arguments: systemPromptTemplate.arguments })
    } else if (systemPromptText) {
      apiMessages.push({ role: 'system', content: systemPromptText })
    }
    apiMessages.push({ role: 'user', content: '', prompt_template: templateId, arguments: args })
    _doSend(apiMessages)
  }

  function _doSend(apiMessages) {
    isStreaming = true
    let aIdxRef = { value: messages.length }
    messages = [...messages, { role: 'assistant', content: '', thinking: null }]
    const reqBody = { model_id: selectedModelId, tool_ids: selectedToolIds, messages: apiMessages, stream: true }
    reqBody.session_id = sessionId ?? 'new'
    abortStream = inferStream(
      reqBody,
      (msg) => onStreamMsg(msg, aIdxRef),
      () => onStreamDone(),
      (err) => onStreamErr(err),
    )
  }

  function handleStop() {
    if (abortStream) { abortStream(); abortStream = null }
  }

  function onStreamMsg(msg, aIdxRef) {
    if (msg.session_id && !msg.role) {
      sessionId = msg.session_id
      return
    }
    if (msg.role === 'assistant') {
      if (aIdxRef.value === -1) {
        aIdxRef.value = messages.length
        messages = [...messages, { role: 'assistant', content: '', thinking: null }]
      }
      let u = [...messages]
      const aIdx = aIdxRef.value
      if (!u[aIdx]) return
      if (msg.content) u[aIdx] = { ...u[aIdx], content: (u[aIdx].content || '') + msg.content }
      if (msg.thinking) u[aIdx] = { ...u[aIdx], thinking: (u[aIdx].thinking || '') + msg.thinking }
      if (msg.tool_calls) u[aIdx] = { ...u[aIdx], tool_calls: msg.tool_calls }
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
          arr[existingIdx] = { ...arr[existingIdx], content: (arr[existingIdx].content || '') + delta }
          messages = arr
        } else {
          // 第一帧：创建新的工具消息占位
          messages = [...messages, {
            role: 'tool',
            name: msg.name || '',
            content: delta,
            tool_call_id: tcId,
            streaming: true,
          }]
        }
        // 流式帧不重置 aIdxRef，让 assistant 消息继续累积
      } else if (msg.streaming === false) {
        // delegate 结束帧：用最终内容更新对应工具消息
        const tcId = msg.tool_call_id
        const existingIdx = tcId
          ? messages.findLastIndex(m => m.role === 'tool' && m.tool_call_id === tcId)
          : -1
        if (existingIdx >= 0) {
          const arr = [...messages]
          arr[existingIdx] = { ...arr[existingIdx], content: msg.content || '', streaming: false }
          messages = arr
        } else {
          messages = [...messages, { role: 'tool', name: msg.name || '', content: msg.content || '' }]
        }
        aIdxRef.value = -1
      } else {
        // 普通工具结果帧（bash、fetch 等）
        messages = [...messages, { role: 'tool', name: msg.name || '', content: msg.content || '' }]
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

  $effect(() => {
    if (sessionRestore.pending) {
      const { sessionId: sid, messages: msgs } = sessionRestore.pending
      sessionRestore.pending = null
      messages = msgs
      sessionId = sid
      errorMsg = ''
    }
  })
</script>

<div class="chat-page">
  <div class="selection-bar">
    <ModelSelector bind:selectedModelId onchange={(id) => localStorage.setItem(STORAGE_MODEL_KEY, id)} />
    <ToolSelector bind:selectedToolIds onchange={(ids) => localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(ids))} />
  </div>
  {#if systemPromptTemplate || systemPromptText}
    <div class="system-prompt-bar">
      <span class="sp-label">{t('systemPromptLabel')}</span>
      {#if systemPromptTemplate}
        <span class="sp-text sp-template">{systemPromptTemplate.template_id}</span>
      {:else}
        <span class="sp-text">{systemPromptText.length > 80 ? systemPromptText.slice(0, 80) + '...' : systemPromptText}</span>
      {/if}
      <button class="sp-clear" onclick={() => { systemPromptText = ''; systemPromptTemplate = null }}>✕</button>
    </div>
  {/if}
  {#if errorMsg}
    <div class="error-bar">{errorMsg}</div>
  {/if}

  <div class="message-area">
    <MessageList {messages} />

    {#if templatePanelOpen}
      <div class="template-panel">
        <div class="panel-header">
          <!-- 标题 -->
          <span class="panel-title">{t('promptTemplatePanelTitle')}</span>
          {#if panelSelectedResult}
            <!-- 占位符标签列表紧跟在标题后面 -->
            {#each extractPlaceholders(panelSelectedResult.template?.content ?? '') as ph}
              <span class="header-placeholder-tag">{ph}</span>
            {/each}
          {/if}
          <!-- 撑满中间空间，右对齐区域 -->
          <div class="header-apply-row">
            {#if panelSelectedResult}
              <!-- template_id 紧靠"作为"前面，复用 panel-title 样式 -->
              <span class="panel-title">{panelSelectedResult.template?.template_id ?? ''}</span>
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
    disabled={isStreaming || !selectedModelId}
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
  .panel-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
    white-space: nowrap;
    flex-shrink: 0;
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
