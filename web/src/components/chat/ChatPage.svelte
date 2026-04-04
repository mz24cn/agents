<script>
  import { inferStream } from '../../lib/api.js'
  import ModelSelector from './ModelSelector.svelte'
  import ToolSelector from './ToolSelector.svelte'
  import PromptTemplateSelector from './PromptTemplateSelector.svelte'
  import PlaceholderInputs from './PlaceholderInputs.svelte'
  import MessageList from './MessageList.svelte'
  import ChatInput from './ChatInput.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  const STORAGE_MODEL_KEY = 'chat_selected_model'
  const STORAGE_TOOLS_KEY = 'chat_selected_tools'

  let messages = $state([])
  let selectedModelId = $state(localStorage.getItem(STORAGE_MODEL_KEY) ?? '')
  let selectedToolIds = $state(JSON.parse(localStorage.getItem(STORAGE_TOOLS_KEY) ?? '[]'))
  let systemPrompt = $state('')
  let isStreaming = $state(false)
  let errorMsg = $state('')
  let pendingTemplate = $state(null)
  let applyToSystem = $state(false)
  let inputText = $state('')
  let abortStream = $state(null)

  function handleTemplateSelect(result) {
    if (!result) { systemPrompt = ''; pendingTemplate = null; return }
    if (result.type === 'direct') {
      if (applyToSystem) { systemPrompt = result.content; pendingTemplate = null }
      else { inputText = result.content; pendingTemplate = null }
    } else if (result.type === 'template') {
      pendingTemplate = result.template
    }
  }

  function handlePlaceholderApply(finalText) {
    if (applyToSystem) systemPrompt = finalText
    else inputText = finalText
    pendingTemplate = null
  }

  function buildApiMessage(msg) {
    const out = { role: msg.role, content: msg.content || '' }
    if (msg.name) out.name = msg.name
    if (msg.tool_calls) out.tool_calls = msg.tool_calls
    if (msg.images) out.images = msg.images
    if (msg.audio) out.audio = msg.audio
    return out
  }

  function handleSend(text) {
    if (!selectedModelId || isStreaming) return
    errorMsg = ''
    messages = [...messages, { role: 'user', content: text }]
    const apiMessages = []
    if (systemPrompt) apiMessages.push({ role: 'system', content: systemPrompt })
    for (const m of messages) {
      if (m.role === 'system') continue
      apiMessages.push(buildApiMessage(m))
    }
    isStreaming = true
    let aIdxRef = { value: messages.length }
    messages = [...messages, { role: 'assistant', content: '', thinking: null }]
    abortStream = inferStream(
      { model_id: selectedModelId, tool_ids: selectedToolIds, messages: apiMessages, stream: true },
      (msg) => onStreamMsg(msg, aIdxRef),
      () => onStreamDone(),
      (err) => onStreamErr(err),
    )
  }

  function handleStop() {
    if (abortStream) { abortStream(); abortStream = null }
  }

  function onStreamMsg(msg, aIdxRef) {
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
    } else if (msg.role === 'function') {
      messages = [...messages, { role: 'function', name: msg.name || '', content: msg.content || '' }]
      aIdxRef.value = -1
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
</script>

<div class="chat-page">
  <div class="selection-bar">
    <ModelSelector bind:selectedModelId onchange={(id) => localStorage.setItem(STORAGE_MODEL_KEY, id)} />
    <ToolSelector bind:selectedToolIds onchange={(ids) => localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(ids))} />
    <PromptTemplateSelector onSelect={handleTemplateSelect} bind:applyToSystem />
  </div>
  {#if systemPrompt}
    <div class="system-prompt-bar">
      <span class="sp-label">{t('systemPromptLabel')}</span>
      <span class="sp-text">{systemPrompt.length > 80 ? systemPrompt.slice(0, 80) + '...' : systemPrompt}</span>
      <button class="sp-clear" onclick={() => { systemPrompt = '' }}>✕</button>
    </div>
  {/if}
  {#if pendingTemplate}
    <PlaceholderInputs template={pendingTemplate} onApply={handlePlaceholderApply} />
  {/if}
  {#if errorMsg}
    <div class="error-bar">{errorMsg}</div>
  {/if}
  <MessageList {messages} />
  <ChatInput disabled={isStreaming || !selectedModelId} onSend={handleSend} onStop={handleStop} {isStreaming} bind:text={inputText} onClear={() => { messages = []; errorMsg = '' }} hasMessages={messages.length > 0} />
</div>

<style>
  .chat-page { display: flex; flex-direction: column; height: 100vh; overflow: hidden; position: absolute; top: 0; left: 0; right: 0; bottom: 0; }
  .selection-bar { display: flex; flex-wrap: wrap; gap: 12px; padding: 12px; border-bottom: 1px solid var(--border); background: var(--bg); align-items: center; }
  .system-prompt-bar { display: flex; align-items: center; gap: 8px; padding: 6px 12px; background: var(--bg-secondary); border-bottom: 1px solid var(--border); font-size: 0.8rem; }
  .sp-label { font-weight: 600; color: var(--text-secondary); white-space: nowrap; }
  .sp-text { color: var(--text); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sp-clear { background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 0.9rem; padding: 2px 6px; border-radius: 4px; }
  .sp-clear:hover { background: var(--border); }
  .error-bar { padding: 8px 12px; background: var(--danger); color: #fff; font-size: 0.85rem; }
</style>
