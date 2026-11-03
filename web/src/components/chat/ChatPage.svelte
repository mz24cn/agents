<script>
  import { onMount, onDestroy, setContext, untrack } from 'svelte'
  import { writable } from 'svelte/store'
  import { inferStream, abortInferStream, interjectInfer, subscribeSessionEvents, subscribeSessionStream, isSessionStreamWatched, agents as agentsApi, sessions as sessionsApi, remoteEnv as remoteEnvApi } from '../../lib/api.js'
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
  import { restoreFileRefTags } from '../../lib/file-ref.js'
  import { mergeToolCallDeltas, startsNewToolCallRound } from '../../lib/stream-messages.js'
  import AskUserDialog from './AskUserDialog.svelte'
  import {
    askUserRequest,
    clientToolCallFrames,
    clientToolDefinitions,
    clientToolNames,
    createQuestionLedger,
    enabledClientToolNames,
    clientToolResultMessage,
    pickUnansweredClientCalls,
    runClientToolCall,
  } from '../../lib/client-tools.js'
  import { assistantIndexForToolCall, insertToolMessage } from '../../lib/stream-tool-placement.js'
  import { t } from '../../lib/i18n.svelte.js'
  import { forkContextWarning } from '../../lib/session-fork.js'
  import { navigate } from '../../lib/router.svelte.js'
  import { sessionRestore, sessionDownload, newSessionCreated, sessionDeleted, sessionForked, currentSession, newSessionRequest, terminalOpen, openSessionLogDir, awaitingUserAnswer } from '../../lib/session-state.svelte.js'
  import DownloadProgress from './DownloadProgress.svelte'
  import { collapseSidebar } from '../../lib/sidebar-width.svelte.js'
  import Terminal from '../Terminal.svelte'
  import {
    remoteExecution,
    bindSessionToRemoteEnv,
    clearRemoteBinding,
    fetchRemoteEnvList,
    fetchRemoteWorkspacePath,
    remoteSessions,
    destroyTerminalInEnv,
    resolvePanelRemoteEnv,
  } from '../../lib/remote-execution.svelte.js'
  import {
    sessionEnvRegistry,
    rememberSessionEnv,
    rememberSessionWorkspace,
    forgetSession,
    resolveTerminalEnvId,
    sessionWorkspace,
  } from '../../lib/session-env-registry.svelte.js'

  const STORAGE_MODEL_KEY = 'chat_selected_model'
  const STORAGE_TOOLS_KEY = 'chat_selected_tools'
  const DRAFT_KEY_PREFIX = 'chat_draft_'
  const DRAFT_SAVE_DELAY_MS = 3000
  function draftKeyFor(sid) { return DRAFT_KEY_PREFIX + (sid || 'new') }
  
  // 应用配置 store
  const appLogoStore = writable('') // 初始为空，等待异步加载
  
  // 设置 context，供子组件使用
  setContext('appLogoStore', appLogoStore)

  let selectedModelId = $state(localStorage.getItem(STORAGE_MODEL_KEY) ?? '')
  let selectedToolIds = $state(JSON.parse(localStorage.getItem(STORAGE_TOOLS_KEY) ?? '[]'))
  // 客户端工具跟其它工具一样，由工具列表的勾选决定：模型能弹出 AskUser 对话框，
  // 就说明用户确实启用过它。以前每轮请求都无条件声明，结果用户没勾选也会被弹窗。
  let enabledClientNames = $derived(
    enabledClientToolNames(
      selectedToolIds.map((id) => catalog.tools.items.find((tool) => tool.tool_id === id))
    )
  )
  // 问题已经摆在用户面前时，这一轮的续跑要继续带着它，哪怕对话框开着时被取消了勾选。
  let answeringClientNames = $state([])
  let errorMsg = $state('')
  // 输入草稿：从本地存储初始化，新打开页面 / 切换到会话页时还原未发送的输入（含 <file> 引用文本）
  let inputText = $state('')
  let sessionId = $state(null)   // currently displayed session ID
  // 衍生新会话（fork）请求态：forkBusyIndex 让被点的那条消息按钮显示忙态，
  // forkBusy 期间禁用全部 fork 按钮，避免连点创建出多条会话。
  let forkBusy = $state(false)
  let forkBusyIndex = $state(-1)

  // ── 输入草稿保护 ─────────────────────────────────────────────────────────────
  // inputText 的任何变更（键入 / 粘贴 / 工作区选文件 / 撤销回填 / 发送后清空）
  // 都重置 3 秒延迟后再写入 localStorage，保证只落盘最后一次输入的最终状态；
  // 输入框清空时立即删除 key，避免下次加载时还原出已发送的内容。
  // activeDraftSid：当前 inputText 归属的会话（null = 未分配 ID 的新会话）。
  // 用它（而非响应式 sessionId）决定写哪个 key，避免 $effect 读取 sessionId 造成跨会话串写。
  let activeDraftSid = null
  let draftSaveTimer = null
  let draftPending = null   // { sid, text }：定时器窗口内尚未落盘的草稿

  // 迁移旧版本的全局草稿（chat_input_draft）到"新会话"key，只迁移一次
  try {
    const legacyDraft = localStorage.getItem('chat_input_draft')
    if (legacyDraft && !localStorage.getItem(draftKeyFor(null))) {
      localStorage.setItem(draftKeyFor(null), legacyDraft)
      localStorage.removeItem('chat_input_draft')
    }
  } catch { /* 忽略存储异常 */ }

  // 初始恢复"新会话"草稿
  inputText = localStorage.getItem(draftKeyFor(null)) ?? ''

  // 切换到某个会话：先把旧会话未落盘的草稿 flush 到它自己的 key，再加载目标会话草稿
  function loadDraftFor(sid) {
    flushDraftNow()
    activeDraftSid = sid
    inputText = localStorage.getItem(draftKeyFor(sid)) ?? ''
  }
  $effect(() => {
    const draft = inputText
    const ownerSid = activeDraftSid
    if (draftSaveTimer) {
      clearTimeout(draftSaveTimer)
      draftSaveTimer = null
    }
    draftPending = { sid: ownerSid, text: draft }
    if (!draft) {
      try { localStorage.removeItem(draftKeyFor(ownerSid)) } catch { /* 忽略存储异常 */ }
      return
    }
    draftSaveTimer = setTimeout(() => {
      draftSaveTimer = null
      // 只在归属会话未变时落盘；若期间已切走，交由 loadDraftFor 的 flush 处理
      if (draftPending && draftPending.sid === ownerSid) {
        try { localStorage.setItem(draftKeyFor(ownerSid), draft) } catch { /* 忽略存储异常 */ }
      }
      draftPending = null
    }, DRAFT_SAVE_DELAY_MS)
  })
  // 同步落盘尚未写入的草稿；页面隐藏（关闭/刷新）与组件销毁时调用
  function flushDraftNow() {
    if (draftSaveTimer) {
      clearTimeout(draftSaveTimer)
      draftSaveTimer = null
    }
    if (draftPending) {
      const { sid, text } = draftPending
      draftPending = null
      try {
        if (text) localStorage.setItem(draftKeyFor(sid), text)
        else localStorage.removeItem(draftKeyFor(sid))
      } catch { /* 忽略存储异常 */ }
    }
  }

  // Per-session state store: each session's messages & streaming state live independently.
  // Key = session ID (or '__new__' before backend assigns one).
  // Stream callbacks write to their own key; switching sessions just changes which key is displayed.
  let sessionStore = $state({})
  // Client-side tools this browser runs: the model calls one, the service ends
  // its loop, the call comes back here, and the browser performs it. For AskUser
  // that means this dialog. They are declared per request only for the tools the
  // user ticked (see enabledClientNames), never behind their back.
  //
  // Questions live in a ledger keyed by session rather than in one dialog slot:
  // a question survives switching sessions or pages and is only ended by the
  // user answering it, closing it, or stopping the turn that asked it. `queueTick`
  // exists so the derived view below re-runs when the (non-reactive) ledger does.
  const questionLedger = createQuestionLedger()
  let queueTick = $state(0)
  const askUserBump = () => { queueTick += 1 }
  const visibleAskUser = $derived.by(() => {
    queueTick  // dependency only: the ledger itself is not reactive state
    return questionLedger.visibleFor(sessionId)
  })
  const waitingAnswerSessions = $derived.by(() => {
    queueTick
    return questionLedger.pendingSessionIds()
  })
  // 侧边栏需要在任何页面都能看到"这条会话在等你回答"，所以镜像到全局状态。
  $effect(() => {
    awaitingUserAnswer.sessionIds = waitingAnswerSessions
  })
  let sessionStreamSubscriptions = {}
  // Sessions whose inference stream is already consumed by this page through
  // the original POST /v1/infer/stream connection. They must not also get a
  // retained GET /v1/sessions/{id}/stream subscription, otherwise every frame
  // is applied twice with two independent aIdxRef instances.
  let directlyConsumedSessions = new Set()
  let directStreamLastSeq = new Map()
  let backendStreamingSessions = new Set()
  // One frame channel per tab made watching a *running* inference almost free,
  // so a streaming session must never end up with no observer: the backend
  // cancels a non-flight generation once nobody watches it, and a flight-mode
  // one would keep running while this tab silently receives nothing. If the
  // channel entry of a still-streaming session disappears (torn-down channel, an
  // unexpected per-session done), re-attach it here.
  let streamWatchSweep = null
  function rearmMissingStreamWatches() {
    for (const sid of backendStreamingSessions) {
      if (!sessionStore[storeKey(sid)] || isSessionStreamWatched(sid)) continue
      subscribeToExistingSession(sid)
    }
  }
  // Sessions currently in the soft-paused resting state (fed by the
  // session-events init snapshot and live status frames). Reactive sets so
  // the input bar can swap to the ▶ resume affordance and remember a pending
  // pause request while the backend drains at the round boundary.
  let backendPausedSessions = $state(new Set())
  let pauseRequestedSessions = $state(new Set())
  // $state's proxy does not track in-place Set mutations in this Svelte
  // version (only property assignments and array methods are reactive) —
  // every mutation must copy-then-reassign, like the rest of this file does
  // for fileDiffVisible / terminals / collapsedGroups. Mutating in place
  // kept the sets correct but invisible to the UI: a live "paused" event
  // changed the Set without ever re-rendering the ⏸ control (a page reload
  // worked only because the init snapshot fills the sets before first
  // render). These helpers keep the invariant in one place.
  function markPaused(sid, paused = true) {
    if (backendPausedSessions.has(sid) === paused) return
    const next = new Set(backendPausedSessions)
    if (paused) next.add(sid)
    else next.delete(sid)
    backendPausedSessions = next
  }
  function markPauseRequested(sid, requested = true) {
    if (pauseRequestedSessions.has(sid) === requested) return
    const next = new Set(pauseRequestedSessions)
    if (requested) next.add(sid)
    else next.delete(sid)
    pauseRequestedSessions = next
  }
  function storeKey(sid) { return sid || '__new__' }

  function streamUiDebug(event, details = {}) {
    try {
      if (localStorage.getItem('session_stream_debug') !== '1') return
      const structural = details.streaming === false
        || details.role === 'usage'
        || details.role === 'system'
        || details.role === 'user'
      if (!structural) return
      console.warn(`[session-stream-ui] ${event}`, {
        at: new Date().toISOString(),
        viewedSessionId: sessionId,
        ...details,
      })
    } catch { /* diagnostics must never affect rendering */ }
  }

  function canMergeStreamFrames(previous, next) {
    if (!previous || !next || previous.role !== next.role) return false
    // Sequence IDs are transport acknowledgements. Keep frames carrying them
    // separate so the retained cursor can advance only after each frame has
    // actually been applied to the UI.
    if (previous.__streamSeq != null || next.__streamSeq != null) return false
    if (next.role === 'assistant') {
      return !previous.tool_calls && !next.tool_calls
        && (previous.agent_id || previous.assistant_id || '') === (next.agent_id || next.assistant_id || '')
    }
    if (next.role === 'tool' && previous.streaming === true && next.streaming === true) {
      return previous.name === next.name
        && getToolUseId(previous) === getToolUseId(next)
        && (previous.agent_id || '') === (next.agent_id || '')
        && (previous.target_agent_id || '') === (next.target_agent_id || '')
    }
    return false
  }

  function mergeStreamFrame(previous, next) {
    if (next.role === 'assistant') {
      return {
        ...previous,
        ...next,
        content: (previous.content || '') + (next.content || ''),
        thinking: (previous.thinking || '') + (next.thinking || ''),
      }
    }
    return {
      ...previous,
      ...next,
      delta: (previous.delta || '') + (next.delta || ''),
    }
  }

  // Network readers can deliver thousands of one-character frames faster than
  // Svelte/Markdown can render them. Apply at most once per animation frame so
  // the browser remains responsive and commits one DOM update for each batch.
  function createStreamFrameBatcher(onFrame) {
    let queue = []
    let scheduled = false
    let afterDrain = []
    const schedule = () => {
      if (scheduled) return
      scheduled = true
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(flush)
      else setTimeout(flush, 0)
    }
    const flush = () => {
      scheduled = false
      const pending = queue
      queue = []
      const merged = []
      for (const frame of pending) {
        const last = merged.at(-1)
        if (canMergeStreamFrames(last, frame)) merged[merged.length - 1] = mergeStreamFrame(last, frame)
        else merged.push(frame)
      }
      for (const frame of merged) onFrame(frame)
      if (queue.length) schedule()
      else {
        const callbacks = afterDrain
        afterDrain = []
        for (const callback of callbacks) callback()
      }
    }
    return {
      push(frame) { queue.push(frame); schedule() },
      then(callback) { afterDrain.push(callback); schedule() },
    }
  }

  function markSessionDirectlyConsumed(sid) {
    if (!sid) return
    directlyConsumedSessions.add(sid)
    // Defensive race handling: a status event may have opened the retained
    // subscription just before the POST stream's init frame assigned/confirmed
    // the session ID. The POST stream is authoritative in this page.
    const unsubscribe = sessionStreamSubscriptions[sid]
    if (unsubscribe) {
      unsubscribe()
      delete sessionStreamSubscriptions[sid]
    }
  }

  function subscribeToExistingSession(sid, { handoff = false } = {}) {
    if (!sid) return
    // The POST stream owns this session until its init hands the session over.
    if (!handoff && directlyConsumedSessions.has(sid)) return
    // Already watched by this tab and the shared frame channel still carries it.
    // `isSessionStreamWatched` is the authoritative half: an entry can be gone
    // (a torn-down channel, an unexpected per-session "done") while this page
    // still holds the old unsubscribe closure, and then nothing would ever
    // resume the frames of a still-running inference.
    if (sessionStreamSubscriptions[sid] && isSessionStreamWatched(sid)) return
    if (handoff) directlyConsumedSessions.delete(sid)
    const keyRef = { key: sid }
    const aIdxRef = { value: -1, groupMode: false, groupMap: {} }
    const frameBatcher = createStreamFrameBatcher((msg) => {
      if (noteClientToolFrame(msg, keyRef)) return
      const { __streamSeq: streamSeq, ...frame } = msg
      // Remember the last *applied* frame so a re-subscription (lost channel
      // entry, reconnect) resumes exactly where this page stopped.
      if (Number.isFinite(streamSeq)) directStreamLastSeq.set(sid, streamSeq)
      // 保留流订阅的总是已有会话，wasNewSession 恒为 false。
      onStreamMsg(frame, aIdxRef, null, false, keyRef)
    })
    const existing = sessionStore[sid]
    const existingMessages = existing?.messages || []
    const currentTurnStart = existingMessages.findLastIndex(message => message.role === 'user')
    const agentIds = new Set(
      existingMessages
        .slice(currentTurnStart + 1)
        .filter(message => message.role === 'assistant')
        .map(getMessageAgentId)
        .filter(Boolean),
    )
    aIdxRef.groupMode = agentIds.size > 1
    if (aIdxRef.groupMode) {
      for (let index = currentTurnStart + 1; index < existingMessages.length; index++) {
        const message = existingMessages[index]
        const agentId = message.role === 'assistant' ? getMessageAgentId(message) : null
        if (agentId && !message.stat) aIdxRef.groupMap[agentId] = index
      }
    }
    let unsubscribe = null
    unsubscribe = subscribeSessionStream(
      sid,
      (msg, seq, eventName) => {
        const frame = eventName === 'usage'
          ? {
              role: 'usage',
              agent_id: msg.agent_id,
              name: msg.name,
              content: JSON.stringify(Object.fromEntries(
                Object.entries(msg).filter(([key]) => !['agent_id', 'assistant_id', 'name'].includes(key))
              )),
            }
          : msg
        frameBatcher.push(seq == null ? frame : { ...frame, __streamSeq: seq })
      },
      () => {
        frameBatcher.then(() => {
          // A retained GET can legitimately observe an inactive snapshot and
          // receive [DONE] immediately. If inference starts before this queued
          // callback runs, the status event sees the still-registered old
          // subscription and cannot open the live one. Remove only this exact
          // subscription, then re-check authoritative backend status.
          if (sessionStreamSubscriptions[sid] === unsubscribe) {
            unsubscribe?.()
            delete sessionStreamSubscriptions[sid]
          }
          if (backendStreamingSessions.has(sid)) {
            const store = sessionStore[sid]
            if (store) {
              store.isStreaming = true
              store.streamConnection = 'connecting'
            }
            setTimeout(() => {
              if (backendStreamingSessions.has(sid) && !sessionStreamSubscriptions[sid]) {
                subscribeToExistingSession(sid)
              }
            }, 0)
          } else {
            onStreamDone(keyRef)
          }
        })
      },
      (err) => {
        // subscribeSessionStream retries recoverable EOF/read/network errors and
        // resumes from the last applied frame sequence. Keep this subscription
        // registered so status events cannot create a duplicate consumer.
        const store = sessionStore[sid]
        if (store) store.streamConnection = 'reconnecting'
        errorMsg = err?.message || t('streamError')
      },
      (init) => {
        const store = sessionStore[sid]
        if (store) {
          store.isStreaming = !!init.active
          store.streamConnection = init.active ? 'connected' : 'done'
        }
        errorMsg = ''
      },
      handoff ? (directStreamLastSeq.get(sid) ?? -1) : -1,
      (acknowledge) => frameBatcher.then(acknowledge),
    )
    sessionStreamSubscriptions[sid] = unsubscribe
  }

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
  let isPaused = $derived(!!sessionId && backendPausedSessions.has(sessionId))
  let pauseRequested = $derived(!!sessionId && pauseRequestedSessions.has(sessionId))
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

  // Which environment / workspace a session's terminal belongs to — per
  // session, because terminals render for **every** opened session while
  // `remoteExecution` describes the current one only (see
  // lib/session-env-registry.svelte.js for why a background session must not
  // follow it).  Only the env **id** is needed: the tab's single terminal
  // socket routes by `env_id` (lib/terminals-mux.js).
  function terminalRemoteEnvId(termId) {
    return resolveTerminalEnvId(termId, sessionId, selectedRemoteEnvId)
  }

  function terminalWorkspace(termId) {
    return termId === sessionId ? workspacePath : sessionWorkspace(termId)
  }

  // A terminal can be opened for a session this page load never visited, and
  // its binding lives in that session's meta.  Fetch just the meta (cheap: no
  // conversation) instead of guessing "local".
  async function resolveSessionRemoteEnv(sid) {
    try {
      const data = await sessionsApi.meta(sid)
      const meta = data?.meta || {}
      // Switching sessions in the meantime makes the restore path authoritative.
      if (sid !== sessionId) {
        rememberSessionEnv(sid, typeof meta.remote_env === 'string' ? meta.remote_env : '')
        rememberSessionWorkspace(sid, typeof meta.workspace === 'string' ? meta.workspace : '')
      }
    } catch {
      // Unknown binding: stay local.  A later visit (or the retarget below)
      // fixes it; a wrongly-local terminal is recoverable, a silent kill is not.
    }
  }

  // Derived: current session's terminal data
  let currentTerminalData = $derived(terminals.get(sessionId))
  let currentTerminalStatus = $derived(currentTerminalData?.status || null)

  // Listen for terminal open requests from sidebar
  $effect(() => {
    if (terminalOpen.token > 0 && terminalOpen.sessionId) {
      openTerminal(terminalOpen.sessionId)
    }
  })

  // Listen for "打开会话日志目录" requests from the sidebar: show the file manager
  // panel and navigate it to the LOCAL session log directory (the conversation.json
  // directory). 会话目录始终在本地（父端）：即使会话绑定远程环境（file journal
  // 在子端），也打开本地会话目录，文件管理器需临时锁定本地环境（logDirLocalMode）。
  let lastOpenLogDirToken = 0
  $effect(() => {
    const req = openSessionLogDir
    if (!req.token || req.token === lastOpenLogDirToken || !req.path) return
    lastOpenLogDirToken = req.token
    logDirLocalMode = true
    // 子端 file journal 软链接（父端 log-dir 探测结果）：本地会话 / 子端无 journal 为 null
    const rj = req.remoteJournal
    if (rj && rj.path) {
      const boundEnv = (sessionId && remoteExecution.sessionId === sessionId) ? remoteExecution.env : null
      const envRec = remoteEnvs.find(e => e.id === rj.env_id)
      logDirRemoteJournal = {
        envId: rj.env_id,
        path: rj.path,
        envName: (boundEnv && boundEnv.id === rj.env_id && boundEnv.title)
          || (envRec ? envDisplayName(envRec) : rj.env_id),
      }
    } else {
      logDirRemoteJournal = null
    }
    // 切换到聊天页，确保文件管理器面板可见
    navigate('#/chat')
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
    workspacePanelOpen = true
    const doNavigate = () => {
      fileManagerNavigateTarget = { path: req.path, token: req.token }
    }
    // 会话绑定远程环境时 workspacePath 是子端工作区路径：会话日志目录在母端，
    // 需临时把 workspacePath 切到本地工作区再导航（文件管理器按它重置环境/根），
    // 导航完成后**立即恢复子端路径**——绝不能让它停留在母端 AGENTS_WORKSPACE，
    // 否则用户下一条消息会把母端路径写进 meta.workspace（工作区落盘丢失）。
    const sessionBoundRemote = !!(sessionId && remoteExecution.sessionId === sessionId && remoteExecution.env)
    const childWorkspace = sessionBoundRemote ? (workspacePath || '') : ''
    if (!workspacePath || sessionBoundRemote) {
      fetchWorkspacePath().then(() => {
        if (openSessionLogDir.token !== req.token) return // 已被更新的请求取代
        if (sessionBoundRemote) {
          const prev = workspacePath
          workspacePath = defaultWorkspacePath
          doNavigate()
          workspacePath = childWorkspace || prev
        } else {
          doNavigate()
        }
      })
    } else {
      doNavigate()
    }
  })

  // 切换当前会话时清除 logDirLocalMode：文件管理器回到当前会话绑定环境
  let lastLogDirModeSessionId = sessionId
  $effect(() => {
    if (sessionId !== lastLogDirModeSessionId) {
      lastLogDirModeSessionId = sessionId
      if (logDirLocalMode) {
        logDirLocalMode = false
      }
      if (logDirRemoteJournal) {
        logDirRemoteJournal = null
      }
    }
  })

  // 打开子端 file journal "软链接"：文件管理器切回会话绑定的远程环境
  // （环境切换会整树重建），工作区指向子端，然后导航到子端会话目录。
  async function openSessionRemoteJournal() {
    const rj = logDirRemoteJournal
    if (!rj || !rj.path) return
    const sid = sessionId
    logDirRemoteJournal = null
    if (!remoteExecution.env || remoteExecution.sessionId !== sid) {
      errorMsg = t('remoteEnvNotFound')
      return
    }
    let ws = ''
    try { ws = await fetchRemoteWorkspacePath() } catch { /* 工作区路径仅用于展示，失败不影响导航 */ }
    if (sessionId !== sid) return // 请求期间切换了会话
    // 环境切换 + 工作区切换 + 导航必须在同一帧完成：先 await 会让文件管理器
    // 带着旧的本地路径指向子端环境（瞬时错误）
    logDirLocalMode = false
    if (ws) workspacePath = ws
    fileManagerNavigateTarget = { path: rj.path, token: Date.now() }
  }

  function openTerminal(sid) {
    if (!sid) return
    // Background session whose binding this page never loaded: ask, don't guess.
    // The channel is re-targeted once the answer arrives (Terminal.svelte keys
    // its connection target on the env id).
    if (sid !== sessionId && !sessionEnvRegistry.envs[sid]) void resolveSessionRemoteEnv(sid)
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

  function destroyTerminal(sid, envIdOverride = null) {
    if (!sid) return
    // Call Terminal component's destroy to prevent auto-reconnect
    const termData = terminals.get(sid)
    if (termData?.ref?.destroy) {
      termData.ref.destroy()
    }
    // 销毁执行环境侧的终端会话（本地=母端；远程=该会话绑定的子端，终端运行在子端）。
    // 按会话查环境而不是看当前激活绑定：后台会话的终端同样属于它自己的环境。
    const envId = envIdOverride ?? terminalRemoteEnvId(sid)
    if (envId) {
      destroyTerminalInEnv(envId, sid)
    } else {
      fetch(`/v1/terminals/${encodeURIComponent(sid)}`, { method: 'DELETE' })
        .catch(err => console.warn('Failed to delete terminal:', err))
    }
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

  // ── 执行环境（远程执行 / remote execution）────────────────────────────
  // 会话级绑定：默认本地；选择子环境后，该会话的全部工具在子环境执行，
  // 推理（模型 + 会话历史）仍在母环境。绑定持久化到会话 meta.remote_env。
  let remoteEnvs = $state([])          // 已登记的子环境列表
  let remoteEnvsLoading = $state(false)
  let remoteEnvDropdownOpen = $state(false)
  let selectedRemoteEnvId = $state('') // '' = 本地

  // 简化设计：工具清单无论本地/远程执行都只取母环境（catalog），切换执行
  // 环境不改变工具选择，浏览器也无需直连子端拉 /v1/tools。
  let isRemoteMode = $derived(!!selectedRemoteEnvId && !!remoteExecution.env)
  let selectedRemoteEnv = $derived(remoteEnvs.find(e => e.id === selectedRemoteEnvId) || null)
  // 显示名：title [id]，无标题时只显示 id。id 放最后，移动端按钮宽度受限时先露出可区分的标题，
  // 而不是所有环境雷同的 id 前缀（如 http://172.28.x.x:port）
  function envDisplayName(e) {
    return e.app_title ? `${e.app_title} [${e.id}]` : e.id
  }
  // 终端属于哪一端由 terminalRemoteEnvId(termId) 逐会话解析
  // （lib/session-env-registry.svelte.js）：终端总是会话级，后台会话的终端同样
  // 属于它自己的环境，不能跟随「当前会话的激活绑定」。
  // "打开会话日志目录"本地锁定：会话日志目录始终在本地（父端），绑定远程环境
  // 的会话也要显示本地会话目录，故临时断开文件管理器的远程环境（终端仍按
  // terminalRemoteEnvId 解析，互不干扰）。切换会话或关闭面板时清除。
  let logDirLocalMode = $state(false)
  // 子端 file journal "软链接"：父端 log-dir 探测到子端已有 journal 时非空
  // （{ envId, path, envName }），文件管理器在本地会话目录里渲染跳转条目。
  let logDirRemoteJournal = $state(null)
  // 文件管理器实际使用的远程环境：logDirLocalMode 生效时固定本地（null）；
  // 否则跟随「当前会话」的绑定 —— 尚未创建会话时（sessionId 与绑定均为 null）
  // 跟随执行环境下拉框，使发送首条消息前打开文件管理器也能直连所选远端环境，
  // 而不是误开本地环境。
  let workspacePanelRemoteEnv = $derived(
    resolvePanelRemoteEnv(sessionId, logDirLocalMode)
  )

  // ChatInput 粘贴上传的目标环境：与文件管理器走同一解析（resolvePanelRemoteEnv），
  // 但**不**带 logDirLocalMode —— 那只是文件管理器「查看本地会话日志目录」的临时锁定，
  // 会话本身仍在子端执行；若沿用会把粘贴落到母端、插入的 <file> 引用在子端根本不存在。
  // 无会话但已选远端环境时同样跟随（sessionId 与绑定均为 null 即匹配），不比文件管理器弱。
  let chatInputRemoteEnv = $derived(
    resolvePanelRemoteEnv(sessionId)
  )

  async function loadRemoteEnvs() {
    if (remoteEnvsLoading) return
    remoteEnvsLoading = true
    try {
      remoteEnvs = await fetchRemoteEnvList(true)
    } catch {
      // 拉取失败保留已有列表，避免选择器闪烁消失
    } finally {
      remoteEnvsLoading = false
    }
  }

  // 切换执行环境：工作区路由与终端连接随环境变化（决策：切换即重置）。
  // 工具选择不受影响——简化设计下工具清单只取母环境（与本地一致），
  // 执行环境只决定工具在哪里运行。
  async function handleRemoteEnvChange(envId) {
    if (envId === selectedRemoteEnvId) return
    // 环境切换使日志目录面板里的子端 journal 软链接失效（它指向旧环境）
    logDirRemoteJournal = null
    remoteEnvDropdownOpen = false
    // 终端连接目标随执行环境变化：销毁当前会话终端（重新打开会连新目标）。
    // 必须在改 selectedRemoteEnvId 之前取旧环境 —— 终端是在**旧**环境里跑的。
    const prevEnvId = selectedRemoteEnvId || ''
    if (sessionId && terminals.has(sessionId)) destroyTerminal(sessionId, prevEnvId)
    selectedRemoteEnvId = envId
    rememberSessionEnv(sessionId, envId)

    if (!envId) {
      await bindSessionToRemoteEnv(sessionId, '')
      if (defaultWorkspacePath) workspacePath = defaultWorkspacePath
      rememberSessionWorkspace(sessionId, workspacePath)
      return
    }
    const env = await bindSessionToRemoteEnv(sessionId, envId)
    if (!env) {
      // 环境记录不存在（可能已被删除）：回退本地
      selectedRemoteEnvId = ''
      rememberSessionEnv(sessionId, '')
      errorMsg = t('remoteEnvNotFound')
      return
    }
    // 工作区指向子环境默认工作区（文件管理器直连子环境）
    if (selectedRemoteEnvId === envId) {
      try {
        const ws = await fetchRemoteWorkspacePath()
        if (selectedRemoteEnvId === envId && ws) {
          workspacePath = ws
          rememberSessionWorkspace(sessionId, ws)
        }
      } catch { /* 工作区路径仅用于展示，失败不影响执行 */ }
    }
  }

  function toggleTemplatePanel() {
    templatePanelOpen = !templatePanelOpen
  }

  // 工作区文件管理器相关函数
  function toggleWorkspacePanel() {
    workspacePanelOpen = !workspacePanelOpen
    // 关闭时清除 logDirLocalMode：面板回到当前会话绑定环境
    if (!workspacePanelOpen && logDirLocalMode) {
      logDirLocalMode = false
      logDirRemoteJournal = null
    }
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
        // 守卫：当前会话已绑定远程环境时，workspacePath 是子端工作区路径，
        // 不能被母端 AGENTS_WORKSPACE 覆盖——否则下一轮消息会把母端路径
        // 写进 meta.workspace（曾导致远程会话工作区落盘/显示丢失）。
        if (!resolvePanelRemoteEnv(sessionId)) {
          workspacePath = envMap.AGENTS_WORKSPACE
        }
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

  // 本轮请求上下文快照：与后端写入 conversation.json 的 messages[i].meta 一致。
  // 模型 / 工具集 / 执行环境 / AI代理 / 工作区在下一条用户消息发送时都可能改变，
  // 所以按轮记录；本地先挂上，重新加载会话前也能看到这一条当时用的是什么。
  function currentTurnMeta() {
    const meta = {}
    if (selectedModelId) meta.model_id = selectedModelId
    if (selectedAgentIds.length > 0) meta.agent_ids = [...selectedAgentIds]
    meta.tool_ids = [...selectedToolIds]
    if (selectedRemoteEnvId) meta.remote_env = selectedRemoteEnvId
    if (workspacePath) meta.workspace = workspacePath
    return meta
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
    const pendingUserMsg = { role: 'user', content: text, timestamp: new Date().toISOString().slice(0, 19).replace('T', ' '), meta: currentTurnMeta() }
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
    const pendingUserMsg = { role: 'user', content: '', timestamp: new Date().toISOString().slice(0, 19).replace('T', ' '), prompt_template: templateId, arguments: args, meta: currentTurnMeta() }
    _doSend(apiMessages, pendingUserMsg)
  }

  // The dialog *is* the AskUser tool: queueing a question returns a promise that
  // settles with the user's choice, which becomes the tool result.
  function askUserQuestion(sid, callId, request) {
    const handle = questionLedger.ask(sid, callId, request)
    askUserBump()
    return handle
  }

  function settleQuestion(key, outcome) {
    if (!key || !questionLedger.settle(key, outcome)) return
    askUserBump()
  }

  /**
   * End one session's questions as "no answer".
   *
   * Only for turns that are genuinely over: the user stopped the turn, or the
   * session was deleted. Switching sessions or pages must not do this - the
   * question stays queued and the dialog is there again when the user comes back.
   */
  function abandonQuestions(sid) {
    if (!sid || !questionLedger.dropSession(sid)) return
    askUserBump()
  }

  /**
   * Run handed-back client tool calls and continue the loop with the answers.
   *
   * The service ended its loop normally with these calls declared and
   * unanswered; one more request carrying a `role: 'tool'` message per call is
   * what lets it read them and go on.
   */
  async function handleClientToolCalls(calls, sid) {
    const pending = (calls || []).filter((call) => call && call.name)
    if (!pending.length || !sid) return
    const questions = []
    const answers = []
    const shown = []
    const answeredNames = []
    for (const call of pending) {
      const callKey = call.id || call.call_id || null
      if (callKey && questionLedger.isQueued(sid, callKey)) continue
      const content = await runClientToolCall(call, {
        // The dialog only renders for the session on screen, so a call for a
        // session in the background just waits there (marked in the session
        // list) instead of being answered on the user's behalf.
        askUser: (request) => {
          const handle = askUserQuestion(sid, callKey, request)
          questions.push(handle)
          return handle.promise
        },
      })
      const message = clientToolResultMessage(call, content)
      answers.push(message)
      answeredNames.push(call.name)
      // The service persists submitted messages but does not rebroadcast them as
      // frames, so the answer only shows up here if we show it ourselves.
      shown.push({ ...message, timestamp: new Date().toISOString() })
    }
    if (!answers.length) return
    if (questions.some((handle) => handle.abandoned)) {
      // The user stopped the turn (or the session went away) while a question was
      // open: the service repairs the abandoned call when a new turn starts, and
      // a result posted now would not belong to it.
      return
    }
    if (sessionId !== sid) return
    answeringClientNames = answeredNames
    const store = sessionStore[sid]
    if (store) store.messages = [...store.messages, ...shown]
    _doSend(answers, null)
  }

  // Client-side calls are named by one status frame; collect them where the
  // frames are consumed and act once the stream is over.
  function noteClientToolFrame(msg, keyRef) {
    const frames = clientToolCallFrames(msg)
    if (frames === null) return false
    keyRef.pendingClientCalls = [...(keyRef.pendingClientCalls || []), ...frames]
    return true
  }

  // Page reload / reopening a session: a client tool call that was never
  // answered is still outstanding, and the question belongs to the user again.
  function maybeResumeClientTools(sid, messages) {
    if (!sid || !messages?.length) return
    const last = messages[messages.length - 1]
    // Anything the user did after the question supersedes it (the service
    // repairs abandoned calls when a new turn starts); only the still-open tip
    // of the conversation is worth re-asking.
    if (last?.role !== 'assistant') return
    const pending = pickUnansweredClientCalls(messages, clientToolNames())
    if (pending.length) void handleClientToolCalls(pending, sid)
  }

  function _doSend(apiMessages, pendingUserMsg, isContinue = false, retryAgentId = null, isResume = false) {
    // Starting a new turn in *this* session supersedes its unanswered question:
    // the service repairs the abandoned call, and an answer posted afterwards
    // would not belong to it. Questions belonging to other sessions stay put.
    abandonQuestions(sessionId)
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
    const reqBody = { model_id: selectedModelId, tool_ids: selectedToolIds, messages: (isContinue || isResume) ? [] : apiMessages, stream: true }
    // Client-side tools ride along as JSON definitions: the service merges them
    // into the model's tool set and hands back whatever the model calls them
    // with, instead of running them itself. Only the ones the user ticked (plus
    // the answer we are posting now) - sending [] also clears a set that an
    // earlier request of this session declared.
    const declaredClientNames = [...new Set([...enabledClientNames, ...answeringClientNames])]
    answeringClientNames = []
    reqBody.client_tools = clientToolDefinitions(declaredClientNames)
    if (isContinue) {
      reqBody.continue = true
      if (retryAgentId) reqBody.retry_agent_id = retryAgentId
    }
    if (isResume) {
      // 恢复软暂停的会话：后端从完整历史继续，不删除任何轮、不写中断尾轮。
      reqBody.resume = true
      if (sessionId) {
        markPaused(sessionId, false)
        markPauseRequested(sessionId, false)
      }
    }
    if (selectedAgentIds.length > 0) {
      reqBody.agent_ids = selectedAgentIds
    }
    if (workspacePath) {
      // 防御：会话已绑定远程环境时 workspacePath 应是子端路径；若恰好等于
      // 母端默认工作区，多半是前端状态被母端 AGENTS_WORKSPACE 污染了
      // （曾导致 meta.workspace 落盘丢失）。不阻断发送，只留痕便于取证。
      if (selectedRemoteEnvId && defaultWorkspacePath && workspacePath === defaultWorkspacePath) {
        console.warn('[chat] sending with workspacePath equal to parent AGENTS_WORKSPACE ' +
          `(${workspacePath}) while session is bound to ${selectedRemoteEnvId}; ` +
          'meta.workspace may be recorded against the parent, not the child')
      }
      reqBody.workspace = workspacePath
    }
    reqBody.session_id = sessionId ?? 'new'
    // 远程执行：remote_env = 子环境 id（后端持久化到 meta.remote_env；
    // 会话工具全部来自子端，保持子端原始工具名）。终端运行在子端，
    // 本地终端注册表没有该状态——后端会问子端 /v1/terminals 自动追加
    // exec_cli；这里在前端也按“会话已有终端”补一次，作为兜底并让工具
    // 计数立即可见（与本地执行一致：终端存在即暴露，不要求面板可见）。
    if (selectedRemoteEnvId) {
      reqBody.remote_env = selectedRemoteEnvId
      if (currentTerminalData && !reqBody.tool_ids.includes('exec_cli')) {
        reqBody.tool_ids = [...reqBody.tool_ids, 'exec_cli']
      }
    }
    // 仅当发送时还没有 sessionId 才视为新会话；本次发送的第一条用户消息
    // 用作新会话的临时标题。已有会话绝不能通知 Sidebar"新会话创建"，
    // 否则条目标题会被 init 帧的预览标题（当前这条用户消息）覆盖，
    // 推理结束后也无法恢复为原标题（需要刷新页面）。
    const wasNewSession = !sessionId
    const pendingFirstUserMsg = wasNewSession
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
        markSessionDirectlyConsumed(initData.session_id)
        const seq = Number(initData.stream_seq)
        if (Number.isFinite(seq)) directStreamLastSeq.set(initData.session_id, seq)
        sessionId = initData.session_id
        currentSession.sessionId = initData.session_id
        activeDraftSid = initData.session_id
        // 新远程会话：会话 ID 由后端分配后，把执行环境绑定同步到真实会话 ID
        //（选择环境时 sessionId 可能还是 null）。
        if (selectedRemoteEnvId && remoteExecution.env
            && remoteExecution.sessionId !== initData.session_id) {
          remoteExecution.sessionId = initData.session_id
        }
      }
      // 通知 Sidebar 有新会话创建（仅当本次请求就是新会话时；
      // 已有会话的后续轮次不能覆盖条目标题）
      if (wasNewSession && !newSessionCreated.sessionId && initData.session_id) {
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
      // 仅当本次请求确实携带新用户消息（pendingUserMsg 非空）时才创建；
      // continue/retry 时用户消息已在列表中，init 里的时间戳只用于后端文件
      // journal 归属。若此时仍按 {...pendingUserMsg} 补建，会追加一条无 role
      // 的幽灵消息，在紧凑模式下被拆出一个空的"🤖 助手"卡片。
      // 强制 role: 'user' 并按时间戳去重（与流式 user 帧的去重逻辑一致）。
      if (initData.user_message_timestamp && pendingUserMsg) {
        const userMsg = { ...pendingUserMsg, role: 'user' }
        userMsg.timestamp = initData.user_message_timestamp
        const duplicate = store.messages.some(existing =>
          existing.role === 'user' && existing.timestamp === userMsg.timestamp
        )
        if (!duplicate) store.messages = [...store.messages, userMsg]
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
        // 继承上一条 assistant 消息的 agent 名称。
        // 后端把 agent 昵称统一存放在 name 字段（持久化消息与流式帧均如此），
        // 从不写 agent_nickname；此前查找 agent_nickname 永远是 undefined。
        const prevAgent = [...store.messages].reverse().find(m => m.role === 'assistant' && m.name)
        if (prevAgent) {
          store.messages[aIdxRef.value] = { ...store.messages[aIdxRef.value], name: prevAgent.name }
          store.messages = [...store.messages]  // trigger reactivity
        }
      }
    }

    // Existing sessions already have a concrete ID before the POST stream's
    // init event. Mark them now so a concurrent status event cannot attach the
    // retained session stream in the meantime.
    if (sessionId) markSessionDirectlyConsumed(sessionId)

    const frameBatcher = createStreamFrameBatcher((msg) => onStreamMsg(msg, aIdxRef, pendingFirstUserMsg, wasNewSession, keyRef))
    inferStream(
      reqBody,
      (msg, eventName) => {
        if (noteClientToolFrame(msg, keyRef)) return
        frameBatcher.push(eventName === 'usage'
          ? {
              role: 'usage',
              agent_id: msg.agent_id,
              name: msg.name,
              content: JSON.stringify(Object.fromEntries(
                Object.entries(msg).filter(([key]) => !['agent_id', 'assistant_id', 'name'].includes(key))
              )),
            }
          : msg)
      },
      // Keep direct ownership until the authoritative done_* status event.
      // [DONE] is sent before the backend has necessarily finished persistence,
      // broker shutdown, and status broadcasting; releasing here can make the
      // still-"streaming" status handler subscribe and replay tail frames.
      () => frameBatcher.then(() => onStreamDone(keyRef)),
      (err) => frameBatcher.then(() => onStreamErr(err, keyRef)),
      onInit,
      (seq) => {
        const sid = keyRef.key === '__new__' ? null : keyRef.key
        if (sid) directStreamLastSeq.set(sid, seq)
      },
      {
        // The POST stream is only the initiator handshake. Hand this session's
        // frames to the tab's shared frame channel through a *tracked*
        // subscription, so the page can tell whether the channel still watches
        // it (and re-subscribe if it does not). inferStream releases the POST
        // connection only after the backend confirms the subscription: cancelling
        // a non-flight inference happens the moment its starter is gone with
        // nobody watching, which otherwise looked like a spurious
        // "Error: user interrupted.".
        onHandoff: (sid, afterSeq) => {
          if (Number.isFinite(afterSeq)) directStreamLastSeq.set(sid, afterSeq)
          subscribeToExistingSession(sid, { handoff: true })
        },
        // The shared channel could not take the session over (no channel, failed
        // subscribe command). The POST keeps carrying the frames itself, so undo
        // the hand-off bookkeeping: without this the sweep or a status event
        // would attach a second consumer and every frame would be applied twice.
        onHandoffFallback: (sid) => {
          directlyConsumedSessions.add(sid)
          const unsubscribe = sessionStreamSubscriptions[sid]
          if (unsubscribe) {
            unsubscribe()
            delete sessionStreamSubscriptions[sid]
          }
        },
      },
    )
  }

  function handleStop() {
    // The turn the question belongs to is over, so its answer can never be used:
    // end it as "no answer" instead of leaving the dialog open over a dead turn.
    abandonQuestions(sessionId)
    const key = storeKey(sessionId)
    const store = sessionStore[key]
    if (!sessionId || !store?.isStreaming) return

    // 通知后端 set cancel_event，后端会发送终止消息并关闭连接。
    // 不再主动终止前端连接，等待后端发送终止消息后自然关闭。
    abortInferStream(sessionId, 'stop')
  }

  // 停止动作菜单：强制停止 = 杀掉卡死的工具进程并强制收尾。
  function handleStopForce() {
    const key = storeKey(sessionId)
    if (sessionId) abortInferStream(sessionId, 'force')
    // Immediately mark local state as not streaming so the UI unblocks.
    const store = sessionStore[key]
    if (store) store.isStreaming = false
  }

  // 软暂停：不打断当前模型轮，推理循环在轮边界（上一轮 assistant+tool 已
  // 完整增量落盘之后、下一次 build_request 之前）干净收尾，状态广播 paused。
  // 后端回 effective=false 表示没有活动推理（可能刚好自然收尾），按普通完成处理。
  function handlePauseInference() {
    if (!sessionId) return
    const store = sessionStore[storeKey(sessionId)]
    if (!store?.isStreaming) return
    markPauseRequested(sessionId, true)
    abortInferStream(sessionId, 'pause').then((res) => {
      if (res && res.effective === false) markPauseRequested(sessionId, false)
    }).catch(() => {
      markPauseRequested(sessionId, false)
    })
  }

  // 恢复暂停的推理：不带新消息的空续推（resume: true，不删除任何历史轮）。
  // 输入框里有草稿时走普通发送路径 —— 暂停会话的历史完整，新消息请求天然
  // 就是"带消息的恢复"。
  function handleResumeInference() {
    if (!sessionId) return
    const store = sessionStore[storeKey(sessionId)]
    if (!store || store.isStreaming || !backendPausedSessions.has(sessionId)) return
    _doSend([], null, false, null, true)
  }

  // 推理途中插话：把输入框草稿排队到当前 generation 的下一个轮边界
  // （本轮所有工具调用完成后）并由同一 generation 继续 —— 不打断当前
  // 模型轮，也不新起请求。generation 恰好已结束时降级为普通发送。
  async function handleInterject(textVal) {
    const val = String(textVal ?? '').trim()
    if (!sessionId || !val) return
    // Same timestamp is used by the local bubble, the enqueue publish and
    // the boundary drain re-publish — the user-frame dedup (by timestamp)
    // collapses all three into a single message, so the bubble shows up
    // immediately even while a long model/tool round is in flight.
    const ts = new Date().toISOString().slice(0, 19).replace('T', ' ')
    const res = await interjectInfer(sessionId, val, ts)
    if (res && res.status === 'queued') {
      inputText = ''
      const store = sessionStore[storeKey(sessionId)]
      if (store) {
        const dup = store.messages.some(m => m.role === 'user' && m.timestamp === ts)
        if (!dup) store.messages = [...store.messages, { role: 'user', content: val, timestamp: ts }]
      }
    } else if (!res || res.status === 'not_active') {
      handleSend(val)
    } else {
      errorMsg = res.error || t('streamError')
    }
  }

  // 结束被暂停的会话：不恢复，把 paused 状态正式收敛为普通完成（历史保持
  // 完整）。后端广播 done_success_unread，所有浏览器同步；当前正在查看的
  // 会话顺手标记已读，避免给自己留未读徽标。
  async function handleEndPausedSession() {
    if (!sessionId) return
    const sid = sessionId
    const res = await abortInferStream(sid, 'end')
    if (res && res.ended) {
      markPaused(sid, false)
      markPauseRequested(sid, false)
      sessionsApi.markRead(sid).catch(() => {})
    }
    // ended=false：后端不在 paused（可能其他端已恢复/结束，事件会自行
    // 同步），或收尾竞态 — 保持 UI 现状，用户可重试。
  }

  function getToolUseId(msg) {
    return msg?.tool_use_id || null
  }

  function getMessageAgentId(msg) {
    return msg?.agent_id || msg?.assistant_id || null
  }

  function findAssistantIndexForToolCall(messages, toolUseId) {
    return assistantIndexForToolCall(messages, toolUseId)
  }

  function resolveToolOwnerAgentId(msg, messages) {
    const explicit = getMessageAgentId(msg)
    if (explicit) return explicit
    const assistantIndex = findAssistantIndexForToolCall(messages, getToolUseId(msg))
    return assistantIndex >= 0 ? getMessageAgentId(messages[assistantIndex]) : null
  }

  function normalizeToolMessage(msg, overrides = {}) {
    const toolUseId = getToolUseId(msg) || getToolUseId(overrides)
    return {
      ...msg,
      ...overrides,
      ...(toolUseId ? { tool_use_id: toolUseId } : {}),
    }
  }

  function onStreamMsg(msg, aIdxRef, pendingFirstUserMsg, wasNewSession = false, keyRef) {
    streamUiDebug('apply_begin', {
      storeKey: keyRef.key,
      role: msg?.role,
      name: msg?.name,
      streaming: msg?.streaming,
      toolUseId: msg?.tool_use_id,
      targetAgentId: msg?.target_agent_id,
      currentMessages: sessionStore[keyRef.key]?.messages?.length,
      aIdx: aIdxRef.value,
    })
    if (msg.session_id && !msg.role) {
      // Migrate store key if backend assigns a new session ID mid-stream
      keyRef.key = migrateSessionStoreKey(keyRef.key, msg.session_id)
      sessionId = msg.session_id
      currentSession.sessionId = msg.session_id
      activeDraftSid = msg.session_id
      // 通知 Sidebar 有新会话创建（仅当本次请求就是新会话时；
      // 已有会话的后续轮次不能覆盖条目标题）
      if (wasNewSession && !newSessionCreated.sessionId) {
        newSessionCreated.sessionId = msg.session_id
        newSessionCreated.firstUserMessage = pendingFirstUserMsg ?? null
      }
      return
    }
    const store = sessionStore[keyRef.key]
    if (!store) return  // session store was cleaned up
    const msgs = store.messages

    if (msg.type === 'remove_trailing_assistant') {
      // Continue/retry is destructive on disk. Reproduce that mutation in all
      // retained-stream consumers before routing the replacement inference.
      // Prefer the persisted timestamp identity; fall back to the final message
      // for compatibility with sessions whose old assistant lacked a timestamp.
      let removeIndex = -1
      if (msg.removed_timestamp) {
        removeIndex = msgs.findLastIndex(existing =>
          existing.role === 'assistant'
          && existing.timestamp === msg.removed_timestamp
          && (!msg.removed_agent_id || (existing.agent_id || existing.assistant_id) === msg.removed_agent_id)
        )
      }
      if (removeIndex < 0 && msgs.at(-1)?.role === 'assistant') removeIndex = msgs.length - 1
      if (removeIndex >= 0) store.messages = msgs.filter((_, index) => index !== removeIndex)
      aIdxRef.value = -1
      aIdxRef.groupMap = {}
      return
    }

    if (msg.role === 'user') {
      // User turns are brokered as complete frames so browsers that did not
      // initiate the POST can render the prompt immediately, before the first
      // assistant token and before conversation.json is incrementally saved.
      // Timestamp is the stable identity used to suppress a replay duplicate
      // when the persisted baseline already contains this turn.
      const duplicate = msgs.some(existing =>
        existing.role === 'user'
        && msg.timestamp
        && existing.timestamp === msg.timestamp
      )
      if (!duplicate) store.messages = [...msgs, { ...msg, role: 'user' }]
      return
    }

    if (msg.role === 'assistant') {
      const incomingAgentId = getMessageAgentId(msg)
      if (!aIdxRef.groupMode && incomingAgentId) {
        // A retained subscription can attach before the second participant has
        // produced any persisted/live assistant frame. History then looks like
        // a single-agent chat even though concurrent group output is already in
        // progress. Detect the first different author before merging its frame,
        // promote the router, and seed each existing agent's current bubble.
        const currentTurnStart = msgs.findLastIndex(existing => existing.role === 'user')
        const existingAgentIds = new Set(
          msgs
            .slice(currentTurnStart + 1)
            .filter(existing => existing.role === 'assistant')
            .map(getMessageAgentId)
            .filter(Boolean),
        )
        if ([...existingAgentIds].some(agentId => agentId !== incomingAgentId)) {
          aIdxRef.groupMode = true
          aIdxRef.groupMap = {}
          for (let index = currentTurnStart + 1; index < msgs.length; index++) {
            const existing = msgs[index]
            const agentId = existing.role === 'assistant' ? getMessageAgentId(existing) : null
            // Seed only the still-open model response. A usage/stat frame marks
            // historical bubbles as closed; reopening one would merge a later
            // inference round into old content after retained-stream handoff.
            if (agentId && !existing.stat) aIdxRef.groupMap[agentId] = index
          }
          aIdxRef.value = -1
        }
      }
      // `aIdxRef` is transport-local and is rebuilt after a direct→retained
      // handoff. More importantly, usage frames do not reset it because they
      // carry stats for the current assistant bubble. If a later model round's
      // first indexed tool call arrives without an intervening tool result
      // frame (disconnect/replay edge case), do not merge it into the previous
      // assistant bubble. That corrupts both chronology and call names/args.
      if (!aIdxRef.groupMode && aIdxRef.value >= 0) {
        const activeAssistant = msgs[aIdxRef.value]
        if (startsNewToolCallRound(activeAssistant, msg)) aIdxRef.value = -1
      }
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
        // Inherit the agent name from the previous assistant message (if any).
        // 后端把 agent 昵称统一存放在 name 字段（持久化消息与流式帧均如此），
        // 从不写 agent_nickname；帧自身携带的 name 会在 ...msg 展开时覆盖继承值。
        const prevAgent = [...msgs].reverse().find(m => m.role === 'assistant' && m.name)
        // 首次帧：直接展开 msg 的所有字段（含 content/thinking/tool_calls 等），无需增量拼接
        const initialMsg = { role: 'assistant', content: '', thinking: null, name: prevAgent?.name, ...msg }
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
      // Formal/streaming tool frames should already carry the caller's
      // agent_id. Recover it from the declaring assistant call for older or
      // partially replayed streams, so concurrent agents cannot steal each
      // other's tool card or reset the wrong assistant route.
      const toolOwnerAgentId = resolveToolOwnerAgentId(msg, msgs)
      if (toolOwnerAgentId && !getMessageAgentId(msg)) {
        msg = { ...msg, agent_id: toolOwnerAgentId }
      }
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
            store.messages = insertToolMessage(msgs, normalizeToolMessage(msg, {
              role: 'tool',
              name: msg.name || '',
              content: delta,
              streaming: true,
              sub_messages: { [agentKey]: subMessage },
            }), tcId)
          }
        } else if (existingIdx >= 0) {
          // delegate / legacy tool streaming: concatenate to content
          const arr = [...msgs]
          const newMsg = normalizeToolMessage(msg, { ...arr[existingIdx] })
          newMsg.content = (arr[existingIdx].content || '') + delta
          arr[existingIdx] = newMsg
          store.messages = arr
        } else {
          store.messages = insertToolMessage(msgs, normalizeToolMessage(msg, {
            role: 'tool',
            name: msg.name || '',
            content: delta,
            streaming: true,
          }), tcId)
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
          store.messages = insertToolMessage(msgs, normalizeToolMessage(msg, { role: 'tool' }), tcId)
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
      // usage(name=round) is the authoritative end of one model response. The
      // following assistant output belongs to a new conversation turn, even
      // when a tool result frame was missed around a disconnect/reconnect.
      if (aIdxRef.groupMode) {
        const agId = msg.agent_id || msg.assistant_id
        if (agId) delete aIdxRef.groupMap[agId]
      } else {
        aIdxRef.value = -1
      }
    }
    streamUiDebug('apply_end', {
      storeKey: keyRef.key,
      role: msg?.role,
      name: msg?.name,
      messages: sessionStore[keyRef.key]?.messages?.length,
      aIdx: aIdxRef.value,
    })
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
    if (keyRef.pendingClientCalls?.length) {
      const sid = keyRef.key === '__new__' ? sessionId : keyRef.key
      void handleClientToolCalls(keyRef.pendingClientCalls, sid)
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
    const sid = keyRef.key === '__new__' ? null : keyRef.key
    const store = sessionStore[keyRef.key]
    if (sid) {
      // POST is only the initiator transport. If it drops after init, hand the
      // first browser over to the resumable retained GET stream. GET will send
      // [DONE] immediately when the conversation already finished meanwhile.
      if (store) {
        store.isStreaming = true
        store.streamConnection = 'reconnecting'
      }
      errorMsg = ''
      subscribeToExistingSession(sid, { handoff: true })
      return
    }
    if (store) store.isStreaming = false
    errorMsg = err?.message || t('streamError')
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
      // The stored turn is the *expanded* one: <file> tags were replaced by
      // [Image/Text file attached: …] plus the inlined payload. Refill the
      // editor with the tag form so the chips come back, not raw placeholders.
      inputText = restoreFileRefTags(revokedMessage.content)
    }
  }

  function formatRevokeConflictMessage(files = []) {
    const fileList = files.length ? `\n\n${t('revokeConflictFiles')}\n${files.map(f => `- ${f}`).join('\n')}` : ''
    return `${t('revokeConflictMessage')}${fileList}`
  }

  // ── 衍生新会话（fork）────────────────────────────────────────────
  // 把这条 assistant 收尾回复及之前的所有轮次复制成一条新会话：后端生成新会话
  // ID，并复制 conversation.json 前缀 / memory / summary；前端只通知 Sidebar 把
  // 新会话插入会话列表并选中（走正常的会话加载路径），此后消息都发往新会话 ID。
  async function handleForkSession(messageIndex, msg = null) {
    const sourceSessionId = sessionId
    if (!sourceSessionId || forkBusy) return
    // 推理中不能 fork：本轮尚未落盘（截点会少掉一轮），且 conversation.json
    // 正在被写入。MessageList 的按钮此时也是禁用态，这里兜底。
    if (isStreaming || backendStreamingSessions.has(sourceSessionId)) {
      errorMsg = t('forkSessionBlocked')
      return
    }
    forkBusy = true
    forkBusyIndex = messageIndex
    errorMsg = ''
    try {
      // timestamp 一并上交：它是 message_index 的交叉校验，也是列表错位时的兜底定位依据。
      const result = await sessionsApi.fork(sourceSessionId, {
        messageIndex,
        timestamp: msg?.timestamp || '',
      })
      const forkedSessionId = result?.session_id
      if (!forkedSessionId) throw new Error(t('forkSessionFailed'))
      sessionForked.sessionId = forkedSessionId
      sessionForked.parentSessionId = sourceSessionId
      // 父会话没标题时后端返回空串，退回首条用户消息做占位标题
      sessionForked.title = result.title || result.first_user_message || null
      sessionForked.token++
      const guardWarning = forkContextWarning(result.context_guard, t)
      if (guardWarning) {
        // 会话已建好，只是复制进来的历史超出可运行的上下文、而摘要没能重建
        // （或重建后仍超）。不阻断切换（fork 仍可用），只说明首次推理可能被拒。
        errorMsg = guardWarning
      }
    } catch (err) {
      errorMsg = err?.message || t('forkSessionFailed')
    } finally {
      forkBusy = false
      forkBusyIndex = -1
    }
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

  // 会话的远程执行环境（用于把 file-journals / diff 查询直连子端）。
  // 会话恢复流程先同步设置 selectedRemoteEnvId，绑定记录异步解析；
  // 这里按需补齐绑定，保证 loadFileJournals 调用时已有 env 记录。
  async function _remoteEnvFor(sid) {
    if (!selectedRemoteEnvId || sid !== sessionId) return null
    if (remoteExecution.sessionId === sid && remoteExecution.env) return remoteExecution.env
    return await bindSessionToRemoteEnv(sid, selectedRemoteEnvId)
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
        const env = await _remoteEnvFor(sessionId)
        const data = env
          ? await remoteSessions.fileJournalDiff(sessionId, turnKey)
          : await sessionsApi.fileJournalDiff(sessionId, turnKey)
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
    // 远程会话：file journal 保存在子端会话目录，直连子端查询
    const env = await _remoteEnvFor(sid)
    try {
      const data = env
        ? await remoteSessions.fileJournals(sid)
        : await sessionsApi.fileJournals(sid)
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
      
      // While inference is active, the in-memory store is newer than
      // conversation.json: it contains the current, not-yet-persisted turn.
      // Never replace that live object when switching away and back. Both the
      // original POST stream and an attached retained stream resolve the store
      // by session ID for every frame, so preserving it also preserves their
      // routing state and the stop-button state.
      const userIndices = new Set()
      for (let i = 0; i < msgs.length; i++) {
        if (msgs[i].role === 'user') userIndices.add(i)
      }
      const existing = sessionStore[sid]
      const wasStreaming = !!(
        existing?.isStreaming
        || backendStreamingSessions.has(sid)
        || directlyConsumedSessions.has(sid)
      )
      if (wasStreaming && existing) {
        sessionStore[sid] = {
          ...existing,
          isStreaming: true,
          streamConnection: directlyConsumedSessions.has(sid)
            ? (existing.streamConnection || 'connected')
            : (existing.streamConnection || 'connecting'),
          collapsedGroups: existing.collapsedGroups ?? userIndices,
        }
      } else {
        sessionStore[sid] = {
          messages: msgs,
          isStreaming: wasStreaming,
          streamConnection: wasStreaming ? 'connecting' : 'idle',
          collapsedGroups: userIndices,
        }
      }
      if (wasStreaming) subscribeToExistingSession(sid)
      sessionId = sid
      currentSession.sessionId = sid
      errorMsg = ''
      needsRead = false
      sessionRestored = true
      shouldScrollToBottom = false
      loadDraftFor(sid)
      maybeResumeClientTools(sid, msgs)

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
        // 恢复执行环境绑定（meta.remote_env = 子环境 id）
        const restoredRemoteEnvId = typeof meta.remote_env === 'string' ? meta.remote_env : ''
        rememberSessionEnv(sid, restoredRemoteEnvId)
        if (restoredRemoteEnvId) {
          selectedRemoteEnvId = restoredRemoteEnvId
          bindSessionToRemoteEnv(sid, restoredRemoteEnvId).then(env => {
            if (!env) {
              if (sessionId === sid) selectedRemoteEnvId = ''
              return
            }
            if (sessionId !== sid) return
            // 工作区：优先恢复 meta.workspace（子端路径）；缺失时取子端默认工作区
            fetchRemoteWorkspacePath().then(ws => {
              if (sessionId !== sid || selectedRemoteEnvId !== restoredRemoteEnvId) return
              if (ws && !meta.workspace && ws !== workspacePath) workspacePath = ws
            }).catch(() => {})
          })
        } else {
          selectedRemoteEnvId = ''
          bindSessionToRemoteEnv(sid, '')
        }
        // 恢复工具选择（本地与远程统一：工具清单只取母环境，meta.tool_ids 直接
        // 恢复；失效 id 由 ToolSelector 在 catalog 加载后过滤掉）
        if (meta.tool_ids) {
          selectedToolIds = meta.tool_ids
          localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(meta.tool_ids))
        }
        // 恢复工作区路径（仅当不同于当前设置时才触发更新）
        const newWorkspace = meta.workspace || defaultWorkspacePath
        if (newWorkspace && newWorkspace !== workspacePath) {
          workspacePath = newWorkspace
        }
        // 记住本会话的工作区：切走之后它的终端仍要落在这个目录里
        rememberSessionWorkspace(sid, newWorkspace || '')
      } else {
        // 无 meta 的旧会话：本地执行
        selectedRemoteEnvId = ''
        rememberSessionEnv(sid, '')
        bindSessionToRemoteEnv(sid, '')
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

      // Load file journals for this session（远程会话直连子端查询）
      loadFileJournals(sid)
    }
  })

  function startNewSession() {
    delete sessionStore['__new__']
    errorMsg = ''
    sessionId = null
    currentSession.sessionId = null
    shouldScrollToBottom = false
    workspacePath = defaultWorkspacePath
    loadDraftFor(null)
    // 新会话默认本地执行（决策：会话级绑定不跨会话继承）
    selectedRemoteEnvId = ''
    clearRemoteBinding()
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
      // 会话没了，它未回答的问题也没有归属了：按“未回答”收尾，别留下悬空的对话框。
      abandonQuestions(deletedSid)
      // Clean up terminal for deleted session
      if (terminals.has(deletedSid)) {
        destroyTerminal(deletedSid)
      }
      // 删除该会话的草稿，并把输入框切回"新会话"草稿
      try { localStorage.removeItem(draftKeyFor(deletedSid)) } catch { /* 忽略存储异常 */ }
      errorMsg = ''
      sessionId = null
      shouldScrollToBottom = false
      loadDraftFor(null)
      // 被删会话若是远程会话：解除绑定（子端孤儿目录由母端 DELETE 异步清理）
      if (remoteExecution.sessionId === deletedSid) {
        selectedRemoteEnvId = ''
        clearRemoteBinding()
      }
      forgetSession(deletedSid)
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
    loadRemoteEnvs()
    // 页面隐藏（关闭/刷新/切后台）时，同步落盘 3 秒延迟窗口内尚未写入的草稿
    window.addEventListener('pagehide', flushDraftNow)
    streamWatchSweep = setInterval(rearmMissingStreamWatches, 5_000)
    _unsubscribeSessionEvents = subscribeSessionEvents(
      (data) => {
        if (data.event === 'init') {
          const pausedSnapshot = new Set()
          for (const [sid, status] of Object.entries(data.sessions || {})) {
            if (status === 'streaming') backendStreamingSessions.add(sid)
            else if (status === 'paused') pausedSnapshot.add(sid)
            const s = sessionStore[storeKey(sid)]
            if (s && status === 'streaming') {
              s.isStreaming = true
              subscribeToExistingSession(sid)
            } else if (s && status === 'paused') {
              s.isStreaming = false
            }
          }
          backendPausedSessions = pausedSnapshot
        } else if (data.event === 'message' && data.session_id && data.status) {
          const sid = data.session_id
          const s = sessionStore[storeKey(sid)]
          if (data.status === 'streaming') {
            backendStreamingSessions.add(sid)
            markPaused(sid, false)
            markPauseRequested(sid, false)
          } else if (data.status === 'paused') {
            // Soft-pause settled: the generation is over (its SSE closed with
            // [DONE]); the session rests in a resumable state.
            backendStreamingSessions.delete(sid)
            markPaused(sid, true)
            markPauseRequested(sid, false)
            directlyConsumedSessions.delete(sid)
            directStreamLastSeq.delete(sid)
          } else if (data.status.startsWith('done_')) {
            backendStreamingSessions.delete(sid)
            markPaused(sid, false)
            markPauseRequested(sid, false)
            directlyConsumedSessions.delete(sid)
            directStreamLastSeq.delete(sid)
          }
          if (s && data.status === 'streaming') {
            s.isStreaming = true
            subscribeToExistingSession(sid)
          } else if (s && (data.status.startsWith('done_') || data.status === 'paused')) {
            s.isStreaming = false
          }
        }
      },
      () => { /* silently ignore SSE errors */ },
    )
  })
  onDestroy(() => {
    window.removeEventListener('pagehide', flushDraftNow)
    if (streamWatchSweep) {
      clearInterval(streamWatchSweep)
      streamWatchSweep = null
    }
    // SPA 路由离开聊天页时也落盘草稿，再次进入聊天页可还原
    flushDraftNow()
    if (_unsubscribeSessionEvents) {
      _unsubscribeSessionEvents()
      _unsubscribeSessionEvents = null
    }
    for (const unsubscribe of Object.values(sessionStreamSubscriptions)) unsubscribe?.()
    sessionStreamSubscriptions = {}
  })
</script>

<!-- 点击外部 / Esc 关闭执行环境下拉 -->
<svelte:window
  onclick={(ev) => { if (remoteEnvDropdownOpen && !ev.target?.closest?.('.env-selector')) remoteEnvDropdownOpen = false }}
  onkeydown={(ev) => { if (ev.key === 'Escape') remoteEnvDropdownOpen = false }}
/>

<div class="chat-page">
  <div class="selection-bar">
    <div class="selector-wrapper" class:disabled={selectedAgentIds.length > 0}>
      📦<a href="#/setup?tab=models" class="nav-link">{t('modelLabel')}</a>
      <ModelSelector bind:selectedModelId onchange={(id) => localStorage.setItem(STORAGE_MODEL_KEY, id)} disabled={selectedAgentIds.length > 0} />
    </div>
    <div class="selector-wrapper" class:disabled={selectedAgentIds.length > 0}>
      🛠️<a href="#/setup?tab=tools" class="nav-link">{t('tools')}</a>
      <ToolSelector
        bind:selectedToolIds
        onchange={(ids) => localStorage.setItem(STORAGE_TOOLS_KEY, JSON.stringify(ids))}
        disabled={selectedAgentIds.length > 0}
      />
    </div>
    <!-- 执行环境选择器：存在已登记子环境时显示；默认本地执行 -->
    {#if remoteEnvs.length > 0}
      <div class="selector-wrapper env-selector">
        🌐<a href="#/setup?tab=auth" class="nav-link">{t('executionEnv')}</a>
        <div class="env-dropdown">
          <button
            type="button"
            class="toggle-btn"
            onclick={() => { remoteEnvDropdownOpen = !remoteEnvDropdownOpen; if (remoteEnvDropdownOpen) loadRemoteEnvs() }}
          >
            <span class="env-label">{selectedRemoteEnv ? envDisplayName(selectedRemoteEnv) : t('localEnv')}</span>
            <span class="arrow">{remoteEnvDropdownOpen ? '▲' : '▼'}</span>
          </button>
          {#if remoteEnvDropdownOpen}
            <div class="env-list">
              <label class="env-item">
                <input type="radio" checked={selectedRemoteEnvId === ''} onchange={() => handleRemoteEnvChange('')} />
                <span class="env-name">{t('localEnv')}</span>
              </label>
              {#each remoteEnvs as e (e.id)}
                <label class="env-item">
                  <input type="radio" checked={selectedRemoteEnvId === e.id} onchange={() => handleRemoteEnvChange(e.id)} />
                  <span class="env-name" title={envDisplayName(e)}>
                    {#if e.id.startsWith('tunnel:')}
                      <span class="tunnel-dot" class:online={!!e.online}
                            title={e.online ? 'tunnel online' : 'tunnel offline'}></span>
                    {/if}
                    {envDisplayName(e)}
                  </span>
                </label>
              {/each}
            </div>
          {/if}
        </div>
      </div>
    {/if}
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

  <AskUserDialog
    open={!!visibleAskUser}
    request={visibleAskUser?.request ?? null}
    otherWaiting={waitingAnswerSessions.filter((sid) => sid !== sessionId).length}
    onChoose={(outcome) => settleQuestion(visibleAskUser?.key, outcome)}
    onCancel={() => settleQuestion(visibleAskUser?.key, { value: null, dismissed: true })}
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
    <DownloadProgress
      visible={sessionDownload.visible}
      loading={sessionDownload.loading}
      received={sessionDownload.received}
      total={sessionDownload.total}
      ariaLabel="Loading session"
    />
    <!-- Terminals: render all instances, show only current session's if visible -->
    {#each Array.from(terminals.entries()) as [termId, termData] (termId)}
      <div class="terminal-view" class:visible={terminalVisible && termId === sessionId}>
        <Terminal
          bind:this={termData.ref}
          sessionId={termId}
          workspace={terminalWorkspace(termId)}
          remoteEnvId={terminalRemoteEnvId(termId)}
          visible={terminalVisible && termId === sessionId}
          onStatusChange={(status) => handleTerminalStatusChange(termId, status)}
        />
      </div>
    {/each}

    <!-- Message list: hidden when terminal is visible -->
    <div class="message-list-container" class:hidden={terminalVisible}>
      {#key sessionId}
        <MessageList {messages} {agentList} {displayMessageDetails} onRevoke={handleRevoke} onScrollAtBottom={handleScrollAtBottom} {shouldScrollToBottom} {collapsedGroups} onToggleCollapse={toggleCollapse} {fileJournalTurnKeyMap} {fileDiffCache} {fileDiffVisible} onToggleFileDiff={handleToggleFileDiff} {retryAssistantIndex} onRetryLastInference={handleRetryLastInference} retryDisabled={isStreaming} onFork={handleForkSession} forkDisabled={isStreaming || forkBusy} {forkBusyIndex} />
      {/key}

      <WorkspaceFileManager
        bind:open={workspacePanelOpen}
        bind:workspacePath={workspacePath}
        bind:navigateTarget={fileManagerNavigateTarget}
        onWorkspaceChange={handleWorkspaceChange}
        onSelectFiles={handleSelectFiles}
        onClose={toggleWorkspacePanel}
        remote={workspacePanelRemoteEnv}
        logDirRemoteJournal={logDirRemoteJournal}
        onOpenRemoteJournal={openSessionRemoteJournal}
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
    onPause={handlePauseInference}
    onResume={handleResumeInference}
    onEndPause={handleEndPausedSession}
    onInterject={handleInterject}
    {isPaused}
    {pauseRequested}
    canPause={selectedAgentIds.length <= 1}
    onToggleTemplatePanel={toggleTemplatePanel}
    onToggleWorkspacePanel={toggleWorkspacePanel}
    bind:workspacePanelOpen
    bind:templatePanelOpen
    {isStreaming}
    bind:text={inputText}
    {selectedAgentIds}
    {agentList}
    remote={chatInputRemoteEnv}
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

  /* Execution environment selector (remote execution) */
  .env-selector .env-dropdown { position: relative; }
  .env-dropdown .toggle-btn {
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    max-width: 260px;
  }
  .env-dropdown .toggle-btn:hover { background: var(--bg-secondary); }
  .env-dropdown .arrow { font-size: 0.7rem; }
  .env-dropdown .env-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .env-list {
    position: absolute;
    top: 100%;
    left: 0;
    margin-top: 4px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px;
    min-width: 260px;
    max-height: 320px;
    overflow-y: auto;
    z-index: 100;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }
  .env-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 6px;
    cursor: pointer;
    font-size: 0.82rem;
    color: var(--text);
    border-radius: 4px;
  }
  .env-item:hover { background: var(--bg-secondary); }
  .env-name {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tunnel-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--border, #888);
    margin-right: 5px;
  }
  .tunnel-dot.online { background: #2ea043; }

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
  /* ===== 移动端适配（≤720px）：提示词面板改为纵向堆叠（预览在上、列表在下） ===== */
  @media (max-width: 720px) {
    .template-panel .panel-body {
      flex-direction: column;
    }
    .template-panel .panel-left {
      flex: 1 1 auto;
      min-height: 0;
      border-right: none;
      border-bottom: 1px solid var(--border);
      padding: 12px;
    }
    .template-panel .panel-right {
      width: 100%;
      min-width: 0;
      max-width: none;
      flex: 0 0 auto;
      max-height: 40%;
      border-left: none;
      border-top: 1px solid var(--border);
    }
    /* 顶栏：允许换行，防止长 template_id / 占位符标签溢出裁切 */
    .template-panel .panel-header {
      flex-wrap: wrap;
      row-gap: 6px;
      padding: 8px 12px;
    }
    .template-panel .header-apply-row {
      flex-wrap: wrap;
      column-gap: 6px;
    }
    /* 执行环境选择器：窄屏占满整行并撑开按钮，尽量多显示名称 */
    .env-selector { flex: 1 1 100%; }
    .env-dropdown .toggle-btn { max-width: 100%; }
    .env-list { max-width: calc(100vw - 24px); }
  }
</style>
