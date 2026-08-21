<script>
  import { onMount, onDestroy, setContext, untrack } from 'svelte'
  import { writable } from 'svelte/store'
  import { inferStream, abortInferStream, subscribeSessionEvents, agents as agentsApi, sessions as sessionsApi } from '../../lib/api.js'
  import { catalog, loadAgents, loadTools, loadEnvVars, refreshAgents } from '../../lib/catalog-state.svelte.js'
  import ModelSelector from './ModelSelector.svelte'
  import ToolSelector from './ToolSelector.svelte'
  import AgentSelector from './AgentSelector.svelte'
  import PromptTemplateSelector from './PromptTemplateSelector.svelte'
  import PlaceholderInputs from './PlaceholderInputs.svelte'
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import MessageList from './MessageList.svelte'
  import ChatInput from './ChatInput.svelte'
  import WorkspaceFileManager from './WorkspaceFileManager.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { buildFileJournalTurnKeyMap } from '../../lib/file-journals.js'
  import { t } from '../../lib/i18n.svelte.js'
  import { navigate } from '../../lib/router.svelte.js'
  import { sessionRestore, newSessionCreated, sessionDeleted, currentSession, newSessionRequest, terminalOpen, openSessionLogDir } from '../../lib/session-state.svelte.js'
  import { collapseSidebar } from '../../lib/sidebar-width.svelte.js'
  import Terminal from '../Terminal.svelte'

  const STORAGE_MODEL_KEY = 'chat_selected_model'
  const STORAGE_TOOLS_KEY = 'chat_selected_tools'
  
  // 应用配置 store
  const appLogoStore = writable('') // 初始为空，等待异步加载
  
  // 设置 context，供子组件使用
  setContext('appLogoStore', appLogoStore)

  let selectedModelId = $state(localStorage.getItem(STORAGE_MODEL_KEY) ?? '')
  let selectedToolIds = $state(JSON.parse(localStorage.getItem(STORAGE_TOOLS_KEY) ?? '[]'))
  let errorMsg = $state('')
  let inputText = $state('')
  let sessionId = $state(null)   // currently displayed session ID

  // Per-session state store: each session's messages & streaming state live independently.
  // Key = session ID (or '__new__' before backend assigns one).
  // Stream callbacks write to their own key; switching sessions just changes which key is displayed.
  let sessionStore = $state({})
  function storeKey(sid) { return sid || '__new__' }

  function migrateSessionStoreKey(oldKey, newKey) {
    if (!newKey || oldKey === newKey) return oldKey
    if (sessionStore[oldKey]) {
      sessionStore[newKey] = sessionStore[oldKey]
      delete sessionStore[oldKey]
    }
    return newKey
  }

  let messages = $derived(sessionStore[storeKey(sessionId)]?.messages ?? [])
  let isStreaming = $derived(sessionStore[storeKey(sessionId)]?.isStreaming ?? false)
  let collapsedGroups = $derived(sessionStore[storeKey(sessionId)]?.collapsedGroups ?? new Set())

  let isGroupChat = $derived(selectedAgentIds.length > 1)
  let retryAssistantIndex = $derived.by(() => {
    if (isStreaming) return -1
    const idx = messages.length - 1
    return idx >= 0 && messages[idx]?.role === 'assistant' ? idx : -1
  })

  let revokeConflict = $state(null)
  let revokeConfirm = $state(null)  // initial revoke confirmation dialog state
  let retryConfirm = $state(false)

  // File journal / diff viewer state
  let fileJournalTurnKeyMap = $state({})         // message timestamp alias -> exact backend journal key
  let fileDiffCache = $state({})                 // turnKey -> { turn_key, files: [...] }
  let fileDiffVisible = $state(new Set())        // turnKeys currently showing diff
  let fileJournalLoadVersion = 0                 // prevents stale session/list responses from winning

  // Terminal state: sessionId -> { ref, status }
  // Terminal sessionId == Chat sessionId
  let terminals = $state(new Map())
  let terminalVisible = $state(false)  // Whether current session's terminal is shown

  // Derived: current session's terminal data
  let currentTerminalData = $derived(terminals.get(sessionId))
  let currentTerminalStatus = $derived(currentTerminalData?.status || null)

  // Listen for terminal open requests from sidebar
  $effect(() => {
    if (terminalOpen.token > 0 && terminalOpen.sessionId) {
      openTerminal(terminalOpen.sessionId)
    }
  })

  // Listen for "打开会话日志目录" requests from sidebar: show the file manager
  // panel and navigate it to the session's conversation.json directory.
  let lastOpenLogDirToken = 0
  $effect(() => {
    const req = openSessionLogDir
    if (!req.token || req.token === lastOpenLogDirToken || !req.path) return
    lastOpenLogDirToken = req.token
    // 切换到聊天页，确保文件管理器面板可见
    navigate('#/chat')
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
    workspacePanelOpen = true
    // 若工作区路径尚未加载（首次打开），先拉取再导航
    if (!workspacePath) {
      fetchWorkspacePath().then(() => {
        fileManagerNavigateTarget = { path: req.path, token: req.token }
      })
    } else {
      fileManagerNavigateTarget = { path: req.path, token: req.token }
    }
  })

  function openTerminal(sid) {
    if (!sid) return
    // Create terminal entry if not exists
    if (!terminals.has(sid)) {
      terminals = new Map(terminals).set(sid, {
        ref: null,
        status: { connected: false, error: null, loading: true, terminalId: null }
      })
    }
    // If opening terminal for current session, show it.
    // Use untrack() to avoid making sessionId a dependency of the caller
    // effect (line 67), which would otherwise re-fire on every session switch.
    if (sid === untrack(() => sessionId)) {
      terminalVisible = true
    }
  }

  // Close button: single click = hide, double click = destroy
  let closeClickTimer = null
  function handleTerminalCloseClick() {
    if (closeClickTimer) {
      // Double click - destroy terminal
      clearTimeout(closeClickTimer)
      closeClickTimer = null
      destroyTerminal(sessionId)
    } else {
      // Single click - hide terminal (go back to messages)
      closeClickTimer = setTimeout(() => {
        closeClickTimer = null
        terminalVisible = false
      }, 250)
    }
  }

  function destroyTerminal(sid) {
    if (!sid) return
    // Call Terminal component's destroy to prevent auto-reconnect
    const termData = terminals.get(sid)
    if (termData?.ref?.destroy) {
      termData.ref.destroy()
    }
    // Call backend API to destroy terminal session
    fetch(`/v1/terminals/${encodeURIComponent(sid)}`, { method: 'DELETE' })
      .catch(err => console.warn('Failed to delete terminal:', err))
    // Remove from local state
    const newTerminals = new Map(terminals)
    newTerminals.delete(sid)
    terminals = newTerminals
    // If destroying current session's terminal, hide it
    if (sid === sessionId) {
      terminalVisible = false
    }
    // Clear terminalOpen to prevent the effect from re-creating this terminal
    if (terminalOpen.sessionId === sid) {
      terminalOpen.sessionId = null
      terminalOpen.token = 0
    }
  }

  function handleTerminalStatusChange(sid, status) {
    if (terminals.has(sid)) {
      const newTerminals = new Map(terminals)
      const terminal = { ...newTerminals.get(sid), status }
      newTerminals.set(sid, terminal)
      terminals = newTerminals
    }
  }

  // Track whether the message list is scrolled to the bottom (used for mark-read logic)
  let isAtBottom = $state(true)
  // Set when a stream ends while user is NOT at the bottom; cleared once read
  let needsRead = $state(false)
  // Set when sessionRestore loads a session; triggers mark-read once auto-scroll reaches bottom
  let sessionRestored = $state(false)
  // Set when user sends a message; forces scroll to bottom even if not currently at bottom
  let shouldScrollToBottom = $state(false)

  function handleScrollAtBottom(atBottom) {
    isAtBottom = atBottom
    // When user scrolls to the bottom and the session needs reading, mark it read
    if (atBottom && needsRead) {
      needsRead = false
      markSessionRead(sessionId)
    }
    // Reset shouldScrollToBottom when we reach the bottom
    if (atBottom) {
      shouldScrollToBottom = false
    }
  }

  // When a session is restored and auto-scroll reaches bottom, mark it as read
  $effect(() => {
    if (sessionRestored && isAtBottom && sessionId) {
      sessionRestored = false
      markSessionRead(sessionId)
    }
  })

  /** Mark the given session as read on the backend (fire-and-forget). */
  function markSessionRead(sid) {
    if (!sid) return
    sessionsApi.markRead(sid).catch(() => {})
  }

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

  // 工作区文件管理器面板状态
  let workspacePanelOpen = $state(false)
  // 文件管理器外部导航目标：{ path, token }，由 Sidebar"打开会话日志目录"触发
  let fileManagerNavigateTarget = $state(null)
  // 当前工作区路径（从环境变量获取）
  let workspacePath = $state('')
  let defaultWorkspacePath = $state('')
  // true: retain the original detailed per-message rendering;
  // false/default: use the compact agent-block rendering.
  let displayMessageDetails = $state(false)
  // 选中的文件列表（用于输入框）
  let selectedWorkspaceFiles = $state([])
  // 工作区是否为自定义路径（不等于默认路径，且默认路径已知）
  // Ensure trailing slash for display so that root paths like "D:\" or "/root"
  // render with a trailing separator. We preserve the OS-native separator
  // (backslash on Windows) so the displayed path matches what users expect.
  let displayWorkspacePath = $derived.by(() => {
    let p = workspacePath || ''
    if (!p) return ''
    const hasBackslash = p.includes('\\')
    const sep = hasBackslash ? '\\' : '/'
    if (!p.endsWith(sep)) p += sep
    return p
  })
  let isWorkspaceCustom = $derived(workspacePath && defaultWorkspacePath && workspacePath !== defaultWorkspacePath)

  // --- Path truncation (show end when overflow) ---
  // We avoid CSS direction:rtl because it renders backslash paths incorrectly
  // on Windows (BiDi algorithm garbles "\:" sequences). Instead we use a
  // ResizeObserver + binary search to compute "…" + tail that fits.
  let pathEl = $state(null)

  $effect(() => {
    const path = displayWorkspacePath
    const el = pathEl
    if (!el) return

    function update() {
      if (!path) { el.textContent = ''; return }
      // Set full text first
      el.textContent = path
      if (el.scrollWidth <= el.clientWidth) return  // fits, done

      // Binary search: how many end-chars can we show with "…" prefix?
      const ellipsis = '\u2026'
      let lo = 0, hi = path.length
      while (lo < hi) {
        const mid = Math.ceil((lo + hi) / 2)
        el.textContent = ellipsis + path.slice(-mid)
        if (el.scrollWidth <= el.clientWidth) lo = mid; else hi = mid - 1
      }
      el.textContent = lo > 0 ? ellipsis + path.slice(-lo) : ellipsis
    }

    update()
    const ro = new ResizeObserver(() => requestAnimationFrame(update))
    ro.observe(el)
    return () => ro.disconnect()
  })

  // 添加为AI代理状态
  let addAgentMode = $state(false)
  let addAgentNickname = $state('')
  let addAgentSaving = $state(false)

  // AI代理选择器状态
  let agentList = $derived(catalog.agents.items)
  let selectedAgentIds = $state(JSON.parse(localStorage.getItem('chat_selected_agents') ?? '[]'))
  let selectedAgentId = $derived(selectedAgentIds.length === 1 ? selectedAgentIds[0] : '') // 向后兼容单选场景
  let loadingAgents = $derived(catalog.agents.loading && !catalog.agents.loaded)

  function toggleTemplatePanel() {
    templatePanelOpen = !templatePanelOpen
  }

  // 工作区文件管理器相关函数
  function toggleWorkspacePanel() {
    workspacePanelOpen = !workspacePanelOpen
    // 从环境变量获取工作区路径
    if (workspacePanelOpen && !workspacePath) {
      fetchWorkspacePath()
    }
  }

  async function fetchWorkspacePath() {
    try {
      const envItems = await loadEnvVars()
      const envMap = Object.fromEntries(envItems.map(item => [item.key, item.value]))
      document.title = envMap.APP_TITLE || t('appTitle')
      displayMessageDetails = String(envMap.DISPLAY_MESSAGE_DETAILS ?? '').trim().toLowerCase() === 'true'
      
      // 处理 APP_LOGO 配置
      const logoConfig = envMap.APP_LOGO ?? ''  // 空字符串表示未配置
      
      // 更新 store：如果配置为空，store 为空（不显示）
      // AppLogo 组件会自动处理 favicon 更新
      appLogoStore.set(logoConfig)
      
      if (envMap.AGENTS_WORKSPACE) {
        defaultWorkspacePath = envMap.AGENTS_WORKSPACE
        workspacePath = envMap.AGENTS_WORKSPACE
      }
    } catch (err) {
      console.error('Failed to fetch workspace path:', err)
    }
  }

  function handleWorkspaceChange(path) {
    workspacePath = path
  }

  function handleSelectFiles(files) {
    selectedWorkspaceFiles = files
    // 将文件路径添加到输入框，使用 <file> 标签避免和前后文本粘连，后端会解析该标签
    const fileRefs = files.map(f => `<file>${f.relative_path}</file>`).join(' ')
    inputText = inputText ? `${inputText} ${fileRefs}` : fileRefs
    workspacePanelOpen = false
  }

  function handleTemplatePanelSelect(result) {
    panelSelectedResult = result
  }

  function getPromptEditHref(templateId) {
    return `#/setup?tab=prompt-edit&templateId=${encodeURIComponent(templateId ?? '')}`
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
    templatePanelOpen = false
  }

  function handleApplyAsSystemTemplate(templateId, args) {
    systemPromptTemplate = { template_id: templateId, arguments: args }
    systemPromptText = ''
    templatePanelOpen = false
  }

  function handleApplyAsUserTemplate(templateId, args) {
    templatePanelOpen = false
    handleSendTemplate(templateId, args)
  }

  function handleApplyAsUserSend(finalText) {
    templatePanelOpen = false
    handleSend(finalText)
  }

  function handleSend(text) {
    if (!selectedModelId && selectedAgentIds.length === 0 || isStreaming) return
    errorMsg = ''
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
    const pendingUserMsg = { role: 'user', content: text, timestamp: new Date().toISOString().slice(0, 19).replace('T', ' ') }
    _doSend(apiMessages, pendingUserMsg)
  }

  // Continue is destructive: ask the user before removing the final assistant
  // message and starting a replacement inference.
  function handleRetryLastInference() {
    if ((!selectedModelId && selectedAgentIds.length === 0) || isStreaming || !sessionId) return
    if (retryAssistantIndex < 0) return
    retryConfirm = true
  }

  function cancelRetryLastInference() {
    retryConfirm = false
  }

  function confirmRetryLastInference() {
    retryConfirm = false
    const key = storeKey(sessionId)
    const store = sessionStore[key]
    const last = store?.messages?.[store.messages.length - 1]
    if (!store || last?.role !== 'assistant') return
    const previousAgentId = last.agent_id || last.assistant_id || null
    // Default to the removed message's author when that agent is still selected.
    // If the user changed the selection, the first selected agent takes over.
    // With no selected agent, retry uses the selected model + tool combination.
    const retryAgentId = selectedAgentIds.length === 0
      ? null
      : (previousAgentId && selectedAgentIds.includes(previousAgentId)
          ? previousAgentId
          : selectedAgentIds[0])
    errorMsg = ''
    _doSend([], null, true, retryAgentId)
  }

  function handleSendTemplate(templateId, args) {
    if (!selectedModelId && selectedAgentIds.length === 0 || isStreaming) return
    errorMsg = ''
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
    const pendingUserMsg = { role: 'user', content: '', timestamp: new Date().toISOString().slice(0, 19).replace('T', ' '), prompt_template: templateId, arguments: args }
    _doSend(apiMessages, pendingUserMsg)
  }

  function _doSend(apiMessages, pendingUserMsg, isContinue = false, retryAgentId = null) {
    // Each stream writes to its own sessionStore entry via keyRef.
    // keyRef.key may change from '__new__' to the real session ID once the
    // backend assigns one (onInit / first onStreamMsg with session_id).
    const keyRef = { key: storeKey(sessionId) }
    if (!sessionStore[keyRef.key]) {
      sessionStore[keyRef.key] = { messages: [], isStreaming: false, collapsedGroups: new Set() }
    }
    sessionStore[keyRef.key].isStreaming = true
    // Force scroll to bottom when user sends a new message
    shouldScrollToBottom = true

    let aIdxRef = { value: -1, groupMode: false, groupMap: {} }
    // 继续推理时不携带新用户消息（messages: [] + continue: true），
    // 后端基于会话既有上下文再跑一轮推理。
    const reqBody = { model_id: selectedModelId, tool_ids: selectedToolIds, messages: isContinue ? [] : apiMessages, stream: true }
    if (isContinue) {
      reqBody.continue = true
      if (retryAgentId) reqBody.retry_agent_id = retryAgentId
    }
    if (selectedAgentIds.length > 0) {
      reqBody.agent_ids = selectedAgentIds
    }
    if (workspacePath) {
      reqBody.workspace = workspacePath
    }
    reqBody.session_id = sessionId ?? 'new'
    // 记录本次发送的第一条用户消息，用于新会话的临时标题
    const pendingFirstUserMsg = !sessionId
      ? (apiMessages.find(m => m.role === 'user')?.content || null)
      : null

    // 当后端分配了真正的 session_id 时，将 store 数据从临时 key 迁移过去
    function migrateKey(newKey) {
      keyRef.key = migrateSessionStoreKey(keyRef.key, newKey)
    }

    // 收到 init 事件后，才创建用户消息和助手占位消息
    const onInit = (initData) => {
      // 同步 sessionId
      if (initData.session_id) {
        migrateKey(initData.session_id)
        sessionId = initData.session_id
        currentSession.sessionId = initData.session_id
      }
      // 通知 Sidebar 有新会话创建（仅当之前没有 sessionId 时）
      if (!newSessionCreated.sessionId && initData.session_id) {
        newSessionCreated.sessionId = initData.session_id
        newSessionCreated.firstUserMessage = pendingFirstUserMsg ?? null
        if (initData.title) {
          newSessionCreated.title = initData.title
        }
      }
      const store = sessionStore[keyRef.key]
      if (!store) return
      // Continue explicitly removed exactly the final assistant message on disk.
      if (initData.removed_trailing_assistant && store.messages.at(-1)?.role === 'assistant') {
        store.messages = store.messages.slice(0, -1)
      }
      // 创建用户消息（带服务端返回的时间戳）
      if (initData.user_message_timestamp) {
        const userMsg = { ...pendingUserMsg }
        userMsg.timestamp = initData.user_message_timestamp
        store.messages = [...store.messages, userMsg]
      }
      // 创建空助手消息占位（群聊模式不创建，由 onStreamMsg 按 agent_id 动态创建）
      if (initData.group_chat) {
        aIdxRef.groupMode = true
        aIdxRef.groupMap = {}
        aIdxRef.value = -1
      } else {
        const assistantMsg = { role: 'assistant', content: '', thinking: null }
        if (selectedAgentIds.length === 1) {
          assistantMsg.agent_id = selectedAgentIds[0]
        } else if (selectedAgentIds.length > 1) {
          assistantMsg.assistant_ids = selectedAgentIds
        }
        store.messages = [...store.messages, assistantMsg]
        aIdxRef.value = store.messages.length - 1
        // 继承 agent_nickname
        const prevAgent = [...store.messages].reverse().find(m => m.role === 'assistant' && m.agent_nickname)
        if (prevAgent) {
          store.messages[aIdxRef.value] = { ...store.messages[aIdxRef.value], agent_nickname: prevAgent.agent_nickname }
          store.messages = [...store.messages]  // trigger reactivity
        }
      }
    }

    inferStream(
      reqBody,
      (msg) => onStreamMsg(msg, aIdxRef, pendingFirstUserMsg, keyRef),
      () => onStreamDone(keyRef),
      (err) => onStreamErr(err, keyRef),
      onInit,
    )
  }

  function handleStop() {
    const key = storeKey(sessionId)
    const store = sessionStore[key]
    if (!sessionId || !store?.isStreaming) return

    // 通知后端 set cancel_event，后端会发送终止消息并关闭连接。
    // 不再主动终止前端连接，等待后端发送终止消息后自然关闭。
    abortInferStream(sessionId)
  }

  // Double-click stop = forced abort: kills running tool processes (exec_shell, MCP)
  // and forces session status to done.  Use when the session is stuck in a
  // tool call that won't respond to a normal abort.
  function handleStopForce() {
    const key = storeKey(sessionId)
    if (sessionId) abortInferStream(sessionId, true)
    // Immediately mark local state as not streaming so the UI unblocks.
    const store = sessionStore[key]
    if (store) store.isStreaming = false
  }

  function mergeToolCallDeltas(existing = [], incoming = []) {
    const merged = existing.map(tc => ({ ...tc }))

    for (const inc of incoming) {
      const incIndex = inc._index
      const incId = inc.id || inc.tool_use_id
      let pos = -1

      // OpenAI-compatible streaming tool calls are deltas. Prefer the explicit
      // delta index, then fall back to id/tool_use_id; otherwise append as a
      // complete/non-streaming tool call.
      if (incIndex !== undefined && incIndex !== null) {
        pos = merged.findIndex(tc => (tc._index ?? 0) === incIndex)
      }
      if (pos < 0 && incId) {
        pos = merged.findIndex(tc => tc.id === incId || tc.tool_use_id === incId)
      }
      if (pos < 0) {
        merged.push({ ...inc })
        continue
      }

      const cur = { ...merged[pos] }
      if (incIndex !== undefined && incIndex !== null) cur._index = incIndex
      if (inc.id) cur.id = inc.id
      if (inc.tool_use_id) cur.tool_use_id = inc.tool_use_id

      // name/arguments may arrive as multiple delta fragments.
      if (inc.name) cur.name = (cur.name || '') + inc.name
      if (inc.arguments !== undefined && inc.arguments !== null) {
        if (typeof inc.arguments === 'string') {
          cur.arguments = (cur.arguments || '') + inc.arguments
        } else {
          cur.arguments = inc.arguments
        }
      }

      merged[pos] = cur
    }

    return merged
  }

  function getToolUseId(msg) {
    return msg?.tool_use_id || null
  }

  function normalizeToolMessage(msg, overrides = {}) {
    const toolUseId = getToolUseId(msg) || getToolUseId(overrides)
    return {
      ...msg,
      ...overrides,
      ...(toolUseId ? { tool_use_id: toolUseId } : {}),
    }
  }

  function onStreamMsg(msg, aIdxRef, pendingFirstUserMsg, keyRef) {
    if (msg.session_id && !msg.role) {
      // Migrate store key if backend assigns a new session ID mid-stream
      keyRef.key = migrateSessionStoreKey(keyRef.key, msg.session_id)
      sessionId = msg.session_id
      currentSession.sessionId = msg.session_id
      // 通知 Sidebar 有新会话创建（仅当之前没有 sessionId 时才视为新会话）
      if (!newSessionCreated.sessionId) {
        newSessionCreated.sessionId = msg.session_id
        newSessionCreated.firstUserMessage = pendingFirstUserMsg ?? null
      }
      return
    }
    const store = sessionStore[keyRef.key]
    if (!store) return  // session store was cleaned up
    const msgs = store.messages

    if (msg.role === 'assistant') {
      if (aIdxRef.groupMode) {
        // ── Group chat: route each agent's stream to its own bubble ──
        const agentId = msg.agent_id || msg.assistant_id || '__unknown__'
        const gIdx = aIdxRef.groupMap[agentId]
        if (gIdx === undefined) {
          // First frame for this agent → create new bubble
          const idx = msgs.length
          aIdxRef.groupMap[agentId] = idx
          const initialMsg = { role: 'assistant', content: '', thinking: null, ...msg }
          if (msg.tool_calls) initialMsg.tool_calls = mergeToolCallDeltas([], msg.tool_calls)
          store.messages = [...msgs, initialMsg]
        } else {
          // Subsequent frames → append to existing bubble
          let u = [...msgs]
          if (!u[gIdx]) return
          const { content: _inc, thinking: _incT, tool_calls: _incTC, ...msgMeta } = msg
          const newMsg = { ...u[gIdx], ...msgMeta }
          if (msg.content) newMsg.content = (u[gIdx].content || '') + msg.content
          if (msg.thinking) newMsg.thinking = (u[gIdx].thinking || '') + msg.thinking
          if (msg.tool_calls) newMsg.tool_calls = mergeToolCallDeltas(u[gIdx].tool_calls || [], msg.tool_calls)
          u[gIdx] = newMsg
          store.messages = u
        }
      } else if (aIdxRef.value === -1) {
        aIdxRef.value = msgs.length
        // Inherit agent_nickname from the previous assistant message (if any)
        const prevAgent = [...msgs].reverse().find(m => m.role === 'assistant' && m.agent_nickname)
        // 首次帧：直接展开 msg 的所有字段（含 content/thinking/tool_calls 等），无需增量拼接
        const initialMsg = { role: 'assistant', content: '', thinking: null, agent_nickname: prevAgent?.agent_nickname, ...msg }
        if (msg.tool_calls) initialMsg.tool_calls = mergeToolCallDeltas([], msg.tool_calls)
        store.messages = [...msgs, initialMsg]
        return  // 首次创建已完成所有字段的设置，跳过后续合并逻辑
      } else {
        // 后续帧：增量追加 content 和 thinking
        let u = [...msgs]
        const aIdx = aIdxRef.value
        if (!u[aIdx]) return
        // 仅合并非内容元数据字段（role, timestamp 等），
        // 避免 msg 中的 content:"" 覆盖已累积的内容（工具调用帧常携带 content:""）
        const { content: _inc, thinking: _incT, tool_calls: _incTC, ...msgMeta } = msg
        const newMsg = { ...u[aIdx], ...msgMeta }
        if (msg.content) newMsg.content = (u[aIdx].content || '') + msg.content
        if (msg.thinking) newMsg.thinking = (u[aIdx].thinking || '') + msg.thinking
        if (msg.tool_calls) newMsg.tool_calls = mergeToolCallDeltas(u[aIdx].tool_calls || [], msg.tool_calls)
        u[aIdx] = newMsg
        store.messages = u
      }
    } else if (msg.role === 'tool') {
      if (msg.streaming === true) {
        const delta = msg.delta || ''
        const tcId = getToolUseId(msg)
        const isTalkTo = msg.name === 'talk_to'
        const existingIdx = tcId
          ? msgs.findLastIndex(m => m.role === 'tool' && getToolUseId(m) === tcId)
          : -1

        if (isTalkTo && msg.target_agent_id) {
          // talk_to delta: the outer tool message belongs to the caller
          // (msg.agent_id), while each streamed child reply is keyed by its
          // explicit target_agent_id.
          const agentKey = msg.target_agent_id
          const subMessage = {
            agent_id: agentKey,
            agent_nickname: msg.target_agent_nickname || agentKey,
            content: delta,
            streaming: true,
          }
          if (existingIdx >= 0) {
            const arr = [...msgs]
            const existing = arr[existingIdx]
            const subMsgs = { ...(existing.sub_messages || {}) }
            const prev = subMsgs[agentKey]
            subMsgs[agentKey] = {
              ...subMessage,
              content: (prev?.content || '') + delta,
            }
            arr[existingIdx] = normalizeToolMessage(msg, {
              ...existing,
              content: (existing.content || '') + delta,
              streaming: true,
              sub_messages: subMsgs,
            })
            store.messages = arr
          } else {
            store.messages = [...msgs, normalizeToolMessage(msg, {
              role: 'tool',
              name: msg.name || '',
              content: delta,
              streaming: true,
              sub_messages: { [agentKey]: subMessage },
            })]
          }
        } else if (existingIdx >= 0) {
          // delegate / legacy tool streaming: concatenate to content
          const arr = [...msgs]
          const newMsg = normalizeToolMessage(msg, { ...arr[existingIdx] })
          newMsg.content = (arr[existingIdx].content || '') + delta
          arr[existingIdx] = newMsg
          store.messages = arr
        } else {
          store.messages = [...msgs, normalizeToolMessage(msg, {
            role: 'tool',
            name: msg.name || '',
            content: delta,
            streaming: true,
          })]
        }
        // 流式帧不重置 aIdxRef，让 assistant 消息继续累积
      } else if (msg.streaming === false) {
        // delegate / talk_to 结束帧：标记流式消息框已完成
        const tcId = getToolUseId(msg)
        const existingIdx = tcId
          ? msgs.findLastIndex(m => m.role === 'tool' && getToolUseId(m) === tcId)
          : -1
        if (existingIdx >= 0) {
          const arr = [...msgs]
          const existing = arr[existingIdx]
          const newMsg = normalizeToolMessage(msg, { ...existing, streaming: false })
          if (msg.content) {
            newMsg.content = msg.content
          }
          // For talk_to, finalize all sub_messages as well
          if (existing.sub_messages) {
            const finalized = {}
            for (const [k, v] of Object.entries(existing.sub_messages)) {
              finalized[k] = { ...v, streaming: false }
            }
            newMsg.sub_messages = finalized
          }
          arr[existingIdx] = newMsg
          store.messages = arr
        } else if (msg.content) {
          store.messages = [...msgs, normalizeToolMessage(msg, { role: 'tool' })]
        }
        aIdxRef.value = -1
        // Group-chat: reset per-agent index so next assistant frame creates new bubble
        if (aIdxRef.groupMode) {
          const agId = msg.agent_id || msg.assistant_id
          if (agId) delete aIdxRef.groupMap[agId]
        }
      } else {
        // Formal tool result. Self-streaming tools already have a temporary
        // message with this tool_use_id, so finalize it in place.
        const tcId = getToolUseId(msg)
        const existingIdx = tcId
          ? msgs.findLastIndex(m => m.role === 'tool' && getToolUseId(m) === tcId)
          : -1
        if (existingIdx >= 0) {
          const arr = [...msgs]
          const existing = arr[existingIdx]
          let subMessages = existing.sub_messages
          if (subMessages) {
            subMessages = Object.fromEntries(
              Object.entries(subMessages).map(([key, value]) => [key, { ...value, streaming: false }])
            )
          }
          arr[existingIdx] = normalizeToolMessage(msg, {
            ...existing,
            ...msg,
            role: 'tool',
            streaming: false,
            ...(subMessages ? { sub_messages: subMessages } : {}),
          })
          store.messages = arr
        } else {
          store.messages = [...msgs, normalizeToolMessage(msg, { role: 'tool' })]
        }
        aIdxRef.value = -1
        // Group-chat: reset per-agent index
        if (aIdxRef.groupMode) {
          const agId = msg.agent_id || msg.assistant_id
          if (agId) delete aIdxRef.groupMap[agId]
        }
      }
    } else if (msg.role === 'system') {
      if (aIdxRef.groupMode) {
        // Group-chat: system messages don't affect per-agent routing
      } else {
        aIdxRef.value = -1
      }
    } else if (msg.role === 'usage') {
      try {
        const s = JSON.parse(msg.content || '{}')
        let lastAIdx
        if (aIdxRef.groupMode && (msg.agent_id || msg.assistant_id)) {
          // Group-chat: find last assistant message from the same agent
          const agId = msg.agent_id || msg.assistant_id
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role === 'assistant' && (msgs[i].agent_id === agId || msgs[i].assistant_id === agId)) {
              lastAIdx = i
              break
            }
          }
        } else {
          lastAIdx = msgs.map((m, i) => m.role === 'assistant' ? i : -1).filter(i => i >= 0).pop()
        }
        if (lastAIdx !== undefined) {
          const arr = [...msgs]
          const update = { ...arr[lastAIdx], stat: s }
          arr[lastAIdx] = update
          store.messages = arr
        }
      } catch (_) {}
    }
  }

  function onStreamDone(keyRef) {
    const store = sessionStore[keyRef.key]
    if (store) {
      store.isStreaming = false
      if (store.messages.length > 0) {
        // Remove trailing empty assistant bubbles (placeholder or failed starts)
        while (store.messages.length > 0) {
          const last = store.messages[store.messages.length - 1]
          if (last.role === 'assistant' && !last.content && !last.thinking && !last.tool_calls) {
            store.messages = store.messages.slice(0, -1)
          } else {
            break
          }
        }
      }
      // 2 秒后自动收起本轮工具调用消息
      autoCollapseLastGroup(keyRef.key)
    }
    // If the stream that just finished belongs to the currently viewed session:
    // - at bottom → mark read immediately
    // - not at bottom → set needsRead so it gets marked when user scrolls down
    if (keyRef.key === sessionId) {
      if (isAtBottom) {
        markSessionRead(sessionId)
      } else {
        needsRead = true
      }
      // Refresh file journals list after stream completes
      loadFileJournals(sessionId)
    }
  }

  function toggleCollapse(startIndex) {
    const key = storeKey(sessionId)
    const store = sessionStore[key]
    if (!store) return
    const old = store.collapsedGroups ?? new Set()
    const next = new Set(old)
    if (next.has(startIndex)) {
      next.delete(startIndex)
    } else {
      next.add(startIndex)
    }
    sessionStore[key] = { ...store, collapsedGroups: next }
  }

  // 记录已安排的自动收起定时器，避免重复
  let collapseTimers = {}
  function autoCollapseLastGroup(key) {
    const store = sessionStore[key]
    if (!store) return
    const msgs = store.messages
    if (msgs.length === 0) return
    // 找到最后一个 user 消息的索引
    let lastUserIdx = -1
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') { lastUserIdx = i; break }
    }
    if (lastUserIdx < 0) return
    // 确认 user 消息后有 tool 消息（即有工具调用过程），才自动收起
    let hasToolMessages = false
    for (let i = lastUserIdx + 1; i < msgs.length; i++) {
      if (msgs[i].role === 'tool') { hasToolMessages = true; break }
    }
    if (!hasToolMessages) return
    // 清除该 key 的旧定时器
    if (collapseTimers[key]) clearTimeout(collapseTimers[key])
    const capturedIdx = lastUserIdx
    collapseTimers[key] = setTimeout(() => {
      const s = sessionStore[key]
      if (!s) { delete collapseTimers[key]; return }
      const old = s.collapsedGroups ?? new Set()
      if (!old.has(capturedIdx)) {
        const next = new Set(old)
        next.add(capturedIdx)
        sessionStore[key] = { ...s, collapsedGroups: next }
      }
      delete collapseTimers[key]
    }, 2000)
  }

  function onStreamErr(err, keyRef) {
    const store = sessionStore[keyRef.key]
    if (store) store.isStreaming = false
    errorMsg = err?.message || t('streamError')
    // If the errored stream belongs to the currently viewed session:
    // - at bottom → mark read immediately
    // - not at bottom → set needsRead so it gets marked when user scrolls down
    if (keyRef.key === sessionId) {
      if (isAtBottom) {
        markSessionRead(sessionId)
      } else {
        needsRead = true
      }
    }
  }

  function applyRevokeSuccess(timestamp) {
    const key = storeKey(sessionId)
    const store = sessionStore[key]
    if (!store) return
    const revokeIndex = store.messages.findIndex(m => m.timestamp === timestamp)
    const revokedMessage = revokeIndex >= 0 ? store.messages[revokeIndex] : null
    if (revokeIndex >= 0) {
      store.messages = store.messages.slice(0, revokeIndex)
    }
    if (revokedMessage?.content) {
      inputText = revokedMessage.content
    }
  }

  function formatRevokeConflictMessage(files = []) {
    const fileList = files.length ? `\n\n${t('revokeConflictFiles')}\n${files.map(f => `- ${f}`).join('\n')}` : ''
    return `${t('revokeConflictMessage')}${fileList}`
  }

  async function handleRevoke(timestamp, hasFileChanges = false) {
    if (!sessionId) return
    revokeConfirm = { timestamp, hasFileChanges }
  }

  async function confirmRevoke() {
    if (!sessionId || !revokeConfirm) return
    const timestamp = revokeConfirm.timestamp
    revokeConfirm = null
    errorMsg = ''
    try {
      await sessionsApi.revoke(sessionId, timestamp)
      applyRevokeSuccess(timestamp)
    } catch (err) {
      if (err?.status === 409 && err?.code === 'JournalConflict') {
        revokeConflict = {
          timestamp,
          files: err.data?.files ?? [],
        }
        return
      }
      errorMsg = err?.message || t('revokeFailed')
    }
  }

  async function keepFilesRevoke() {
    if (!sessionId || !revokeConfirm) return
    const timestamp = revokeConfirm.timestamp
    revokeConfirm = null
    errorMsg = ''
    try {
      await sessionsApi.revoke(sessionId, timestamp, { keepFiles: true })
      applyRevokeSuccess(timestamp)
    } catch (err) {
      errorMsg = err?.message || t('revokeFailed')
    }
  }

  function cancelRevoke() {
    revokeConfirm = null
  }

  async function confirmForceRevoke() {
    if (!sessionId || !revokeConflict) return
    const timestamp = revokeConflict.timestamp
    revokeConflict = null
    errorMsg = ''
    try {
      await sessionsApi.revoke(sessionId, timestamp, { forced: true })
      applyRevokeSuccess(timestamp)
    } catch (err) {
      errorMsg = err?.message || t('revokeFailed')
    }
  }

  async function keepFilesAndRevoke() {
    if (!sessionId || !revokeConflict) return
    const timestamp = revokeConflict.timestamp
    revokeConflict = null
    errorMsg = ''
    try {
      await sessionsApi.revoke(sessionId, timestamp, { keepFiles: true })
      applyRevokeSuccess(timestamp)
    } catch (err) {
      errorMsg = err?.message || t('revokeFailed')
    }
  }

  function cancelForceRevoke() {
    revokeConflict = null
  }

  async function handleToggleFileDiff(turnKey) {
    if (!sessionId || !turnKey) return
    // If already visible, hide it
    if (fileDiffVisible.has(turnKey)) {
      const next = new Set(fileDiffVisible)
      next.delete(turnKey)
      fileDiffVisible = next
      return
    }
    // Fetch diff data if not cached
    if (!fileDiffCache[turnKey]) {
      try {
        const data = await sessionsApi.fileJournalDiff(sessionId, turnKey)
        fileDiffCache = { ...fileDiffCache, [turnKey]: data }
      } catch (err) {
        errorMsg = err?.message || 'Failed to load file diff'
        return
      }
    }
    // Show the diff
    const next = new Set(fileDiffVisible)
    next.add(turnKey)
    fileDiffVisible = next
  }

  // Fetch file journals list when a session is restored
  async function loadFileJournals(sid) {
    if (!sid) return
    const loadVersion = ++fileJournalLoadVersion
    try {
      const data = await sessionsApi.fileJournals(sid)
      if (loadVersion !== fileJournalLoadVersion || sid !== sessionId) return
      fileJournalTurnKeyMap = buildFileJournalTurnKeyMap(data.turn_keys || [])
      // Reset diff state for new session
      fileDiffCache = {}
      fileDiffVisible = new Set()
    } catch {
      if (loadVersion !== fileJournalLoadVersion || sid !== sessionId) return
      fileJournalTurnKeyMap = {}
    }
  }

  // Watch for session restore requests
  $effect(() => {
    const pending = sessionRestore.pending;
    if (pending) {
      const { sessionId: sid, messages: msgs, meta } = pending
      sessionRestore.pending = null
      
      // Hide terminal when switching sessions
      terminalVisible = false
      
      // If the target session is not currently streaming, use fresh backend data.
      // If it IS streaming, keep the live data — don't overwrite with stale backend data.
      if (!sessionStore[sid]?.isStreaming) {
        // 计算所有 user 消息的索引，用于默认折叠
        const userIndices = new Set()
        for (let i = 0; i < msgs.length; i++) {
          if (msgs[i].role === 'user') {
            userIndices.add(i)
          }
        }
        sessionStore[sid] = { messages: msgs, isStreaming: false, collapsedGroups: userIndices }
      }
      sessionId = sid
      currentSession.sessionId = sid
      errorMsg = ''
      needsRead = false
      sessionRestored = true
      shouldScrollToBottom = false

      // Load file journals for this session
      loadFileJournals(sid)

      // 优先使用 meta 中的设置（向下兼容：旧会话可能没有 meta）
      if (meta) {
        // 恢复AI代理选择
        if (meta.agent_ids && Array.isArray(meta.agent_ids)) {
          selectedAgentIds = meta.agent_ids
          localStorage.setItem('chat_selected_agents', JSON.stringify(meta.agent_ids))
        } else {
          // meta 中没有 agent_ids，清除选择
          selectedAgentIds = []
          localStorage.removeItem('chat_selected_agents')
        }
        // 恢复模型选择
        if (meta.model_id) {
          selectedModelId = meta.model_id
          localStorage.setItem(STORAGE_MODEL_KEY, meta.model_id)
        }
        // 恢复工具选择
        if (meta.tool_ids) {
          selectedToolIds = meta.tool_ids
          localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(meta.tool_ids))
        }
        // 恢复工作区路径（仅当不同于当前设置时才触发更新）
        const newWorkspace = meta.workspace || defaultWorkspacePath
        if (newWorkspace && newWorkspace !== workspacePath) {
          workspacePath = newWorkspace
        }
      } else {
        // 无 meta 的旧会话，工作区回退到默认值
        if (defaultWorkspacePath && defaultWorkspacePath !== workspacePath) {
          workspacePath = defaultWorkspacePath
        }
        // 向下兼容：从最后一条 assistant 消息恢复 agent_id
        const lastAssistantMsg = [...msgs].reverse().find(m => m.role === 'assistant')
        if (lastAssistantMsg?.assistant_ids && Array.isArray(lastAssistantMsg.assistant_ids)) {
          selectedAgentIds = lastAssistantMsg.assistant_ids
          localStorage.setItem('chat_selected_agents', JSON.stringify(lastAssistantMsg.assistant_ids))
        } else if (lastAssistantMsg?.agent_id || lastAssistantMsg?.assistant_id) {
          const aid = lastAssistantMsg.agent_id || lastAssistantMsg.assistant_id
          selectedAgentIds = [aid]
          localStorage.setItem('chat_selected_agents', JSON.stringify([aid]))
        }
        // 模型和工具选中状态维持不变（使用 localStorage 中的值）
      }
    }
  })

  function startNewSession() {
    delete sessionStore['__new__']
    errorMsg = ''
    sessionId = null
    currentSession.sessionId = null
    shouldScrollToBottom = false
    workspacePath = defaultWorkspacePath
    fileJournalLoadVersion += 1
    fileJournalTurnKeyMap = {}
    fileDiffCache = {}
    fileDiffVisible = new Set()
  }

  // 监听 Sidebar 顶部的新建会话按钮。即使当前会话正在推理，也允许切换到新会话，
  // 已有流式回调仍会写入各自 sessionStore，不会被中断。
  let lastNewSessionToken = 0
  $effect(() => {
    const token = newSessionRequest.token
    if (token && token !== lastNewSessionToken) {
      lastNewSessionToken = token
      startNewSession()
    }
  })

  // 监听会话删除事件：同步清空右侧面板（效果等同新建会话）
  $effect(() => {
    const deletedSid = sessionDeleted.sessionId
    if (deletedSid) {
      sessionDeleted.sessionId = null
      delete sessionStore[deletedSid]
      // Clean up terminal for deleted session
      if (terminals.has(deletedSid)) {
        destroyTerminal(deletedSid)
      }
      errorMsg = ''
      sessionId = null
      shouldScrollToBottom = false
    }
  })

  // 加载AI代理列表（共享数据源：设置页变更后会即时反映到这里）
  async function fetchAgents({ force = false } = {}) {
    try {
      if (force) await refreshAgents()
      else await loadAgents()
    } catch {
      catalog.agents.error = catalog.agents.error || t('fetchAgentsFailed')
    }
  }

  $effect(() => { fetchAgents() })

  $effect(() => {
    if (catalog.agents.loaded && selectedAgentIds.length > 0) {
      const validIds = new Set(agentList.map(a => a.agent_id))
      const validSelected = selectedAgentIds.filter(id => validIds.has(id))
      if (validSelected.length !== selectedAgentIds.length) {
        selectedAgentIds = validSelected
        localStorage.setItem('chat_selected_agents', JSON.stringify(validSelected))
      }
    }
  })


  // 生成添加AI代理的默认昵称
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
      // 获取选中工具的详细信息用于描述；复用工具选择器/列表页的共享数据源
      await loadTools().catch(() => {})
      const toolNames = catalog.tools.items
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
        group: '',
      }
      await agentsApi.create(payload)
      addAgentMode = false
      addAgentNickname = ''
      await fetchAgents({ force: true })
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

  // Listen for session events so the UI can recover from force-abort.
  // When the backend force-aborts a session, it sets status to
  // "done_error_unread".  We detect this and clear the local isStreaming
  // flag, which lets the user continue interacting.
  let _unsubscribeSessionEvents = null
  onMount(() => {
    fetchWorkspacePath()
    _unsubscribeSessionEvents = subscribeSessionEvents(
      (data) => {
        if (data.event === 'message' && data.session_id && data.status) {
          const s = sessionStore[storeKey(data.session_id)]
          if (s && s.isStreaming && data.status.startsWith('done_')) {
            s.isStreaming = false
          }
        }
      },
      () => { /* silently ignore SSE errors */ },
    )
  })
  onDestroy(() => {
    if (_unsubscribeSessionEvents) {
      _unsubscribeSessionEvents()
      _unsubscribeSessionEvents = null
    }
  })
</script>

<div class="chat-page">
  <div class="selection-bar">
    <div class="selector-wrapper" class:disabled={selectedAgentIds.length > 0}>
      📦<a href="#/setup?tab=models" class="nav-link">{t('modelLabel')}</a>
      <ModelSelector bind:selectedModelId onchange={(id) => localStorage.setItem(STORAGE_MODEL_KEY, id)} disabled={selectedAgentIds.length > 0} />
    </div>
    <div class="selector-wrapper" class:disabled={selectedAgentIds.length > 0}>
      🛠️<a href="#/setup?tab=tools" class="nav-link">{t('tools')}</a>
      <ToolSelector bind:selectedToolIds onchange={(ids) => localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(ids))} disabled={selectedAgentIds.length > 0} />
    </div>
    {#if isWorkspaceCustom}
      <div class="workspace-indicator" title={workspacePath} onclick={toggleWorkspacePanel}>
        <span class="workspace-icon">📁</span>
        <span class="workspace-path" bind:this={pathEl}></span>
      </div>
    {/if}
    <!-- Terminal control: show terminal icon/button if current session has terminal -->
    {#if currentTerminalData}
      <div class="terminal-control">
        {#if terminalVisible}
          <!-- Terminal is visible: show status + close button -->
          <span>🖥️</span>
          {#if currentTerminalStatus?.connected}
            <button 
              class="terminal-close-btn"
              onclick={handleTerminalCloseClick}
              title="Click to hide, double-click to destroy"
            >
              ▼
            </button>
          {:else}
            <span class="terminal-status">
              {currentTerminalStatus?.loading ? 'Loading...' : 'Connecting...'}
            </span>
          {/if}
        {:else}
          <!-- Terminal exists but hidden: show icon to switch to it -->
          <button 
            class="terminal-show-btn"
            onclick={() => terminalVisible = true}
            title="Show terminal"
          >
            💻
          </button>
        {/if}
      </div>
    {/if}
    <div class="agent-selector-spacer"></div>
    <div class="agent-selector-wrapper">
      🤖<a href="#/setup?tab=agents" class="nav-link">{t('agentSelector')}</a>
      <AgentSelector 
        bind:selectedAgentIds 
        onchange={(ids) => {
          if (ids.length > 0) {
            localStorage.setItem('chat_selected_agents', JSON.stringify(ids))
          } else {
            localStorage.removeItem('chat_selected_agents')
          }
        }}
        disabled={loadingAgents}
      />
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

  <ConfirmDialog
    open={!!revokeConfirm}
    title={t('revoke')}
    message={t('confirmRevoke')}
    confirmText={t('revoke')}
    customText={revokeConfirm?.hasFileChanges ? t('revokeKeepFiles') : ''}
    onConfirm={confirmRevoke}
    onCustom={revokeConfirm?.hasFileChanges ? keepFilesRevoke : null}
    onCancel={cancelRevoke}
  />

  <ConfirmDialog
    open={!!revokeConflict}
    title={t('revokeConflictTitle')}
    message={formatRevokeConflictMessage(revokeConflict?.files ?? [])}
    customText={t('revokeKeepFiles')}
    onCustom={keepFilesAndRevoke}
    onConfirm={confirmForceRevoke}
    onCancel={cancelForceRevoke}
  />

  <ConfirmDialog
    open={retryConfirm}
    title={t('retryLastInference')}
    message={t('confirmRetryLastInference')}
    confirmText={t('retryLastInference')}
    onConfirm={confirmRetryLastInference}
    onCancel={cancelRetryLastInference}
  />

  <div class="message-area">
    <!-- Terminals: render all instances, show only current session's if visible -->
    {#each Array.from(terminals.entries()) as [termId, termData] (termId)}
      <div class="terminal-view" class:visible={terminalVisible && termId === sessionId}>
        <Terminal 
          bind:this={termData.ref}
          sessionId={termId}
          workspace={workspacePath}
          visible={terminalVisible && termId === sessionId}
          onStatusChange={(status) => handleTerminalStatusChange(termId, status)}
        />
      </div>
    {/each}

    <!-- Message list: hidden when terminal is visible -->
    <div class="message-list-container" class:hidden={terminalVisible}>
      {#key sessionId}
        <MessageList {messages} {agentList} {displayMessageDetails} onRevoke={handleRevoke} onScrollAtBottom={handleScrollAtBottom} {shouldScrollToBottom} {collapsedGroups} onToggleCollapse={toggleCollapse} {fileJournalTurnKeyMap} {fileDiffCache} {fileDiffVisible} onToggleFileDiff={handleToggleFileDiff} {retryAssistantIndex} onRetryLastInference={handleRetryLastInference} retryDisabled={isStreaming} />
      {/key}

      <WorkspaceFileManager
        bind:open={workspacePanelOpen}
        bind:workspacePath={workspacePath}
        bind:navigateTarget={fileManagerNavigateTarget}
        onWorkspaceChange={handleWorkspaceChange}
        onSelectFiles={handleSelectFiles}
        onClose={toggleWorkspacePanel}
      />
    </div>

    <div class="template-panel" class:hidden={!templatePanelOpen}>
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
              <a href={getPromptEditHref(panelSelectedResult.template?.template_id)} class="nav-link">{panelSelectedResult.template?.template_id ?? ''}</a>
              <span class="apply-as-label">{t('applyAs')}</span>
              <button class="btn btn-secondary" onclick={() => handleHeaderApply('system')}>{t('applyAsSystem')}</button>
              <button class="btn btn-primary" onclick={() => handleHeaderApply('user')}>{t('applyAsUserSend')}</button>
            {/if}
            <button class="panel-close" onclick={toggleTemplatePanel} title={t('close')}>✕</button>
          </div>
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
  </div>

  <ChatInput
    disabled={!selectedModelId && selectedAgentIds.length === 0}
    onSend={handleSend}
    onStop={handleStop}
    onStopForce={handleStopForce}
    onToggleTemplatePanel={toggleTemplatePanel}
    onToggleWorkspacePanel={toggleWorkspacePanel}
    bind:workspacePanelOpen
    bind:templatePanelOpen
    {isStreaming}
    bind:text={inputText}
    {selectedAgentIds}
    {agentList}
    onError={(msg) => errorMsg = msg}
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
    position: relative;
    z-index: 100;
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
  .workspace-indicator {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 4px 8px;
    background: var(--bg-secondary);
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--text-secondary);
    cursor: pointer;
    max-width: 300px;
    overflow: hidden;
  }
  .workspace-indicator:hover {
    background: var(--border);
  }
  .workspace-icon {
    flex-shrink: 0;
  }
  .workspace-path {
    overflow: hidden;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }
  .agent-selector-wrapper { display: flex; align-items: center; gap: 8px; }

  /* Terminal close button in header */
  .terminal-control {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 6px 10px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-size: 0.85rem;
    color: var(--text-secondary);
  }

  .terminal-close-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: inherit;
    padding: 0;
    line-height: 1;
    color: inherit;
    display: flex;
    align-items: center;
    gap: 4px;
    transition: color 0.2s;
  }

  .terminal-close-btn:hover {
    color: var(--text);
  }

  .terminal-show-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1rem;
    padding: 2px 4px;
    line-height: 1;
    opacity: 0.7;
    transition: opacity 0.2s;
  }

  .terminal-show-btn:hover {
    opacity: 1;
  }

  .terminal-status {
    font-size: 0.75rem;
    color: var(--text-secondary);
    animation: pulse 1.5s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  /* Terminal and message list containers */
  .terminal-view {
    display: none;
    flex: 1;
    overflow: hidden;
  }

  .terminal-view.visible {
    display: flex;
  }

  .message-list-container {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .message-list-container.hidden {
    display: none;
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

  /* Terminal view styles moved to terminal-control section */

  .template-panel {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: calc(100% - 40px); /* 减去系统提示词行高 */
    background: var(--bg);
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.12);
    z-index: 10;
  }
  .template-panel.hidden {
    display: none;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 16px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    flex-wrap: nowrap;
    overflow: hidden;
    background: var(--bg-secondary);
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
