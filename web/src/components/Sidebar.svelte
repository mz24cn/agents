<script>
  import { onMount, onDestroy, tick } from 'svelte'
  import { router, navigate } from '../lib/router.svelte.js'
  import { t } from '../lib/i18n.svelte.js'
  import { sessions, subscribeSessionEvents } from '../lib/api.js'
  import { sessionRestore, sessionDownload, newSessionCreated, sessionDeleted, currentSession, newSessionRequest, terminalOpen, openSessionLogDir, messageScrollRequest } from '../lib/session-state.svelte.js'
  import { sidebarWidth, setSidebarWidth, toggleSidebarCollapsed, collapseSidebar, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from '../lib/sidebar-width.svelte.js'
  import SessionCategoryNode from './SessionCategoryNode.svelte'

  const SESSION_PAGE_SIZE = 100

  let sessionList = $state([])
  let sessionError = $state('')
  let sessionLoading = $state(false)
  let sessionLoadingMore = $state(false)
  let sessionPage = $state(1)
  let sessionHasMore = $state(true)
  let restoreError = $state('')
  let lastClickTime = $state(0)
  let searchOpen = $state(false)
  let searchText = $state('')
  let activeSearchQuery = $state('')
  let searchInput = $state(null)
  let directoryOpen = $state(false)
  let categoryTree = $state([])
  let categoryTreeLoading = $state(false)
  let categoryTreeError = $state('')
  let categoryExpandedPaths = $state(new Set())
  let selectedCategory = $state('')
  let categoryTreePromise = null
  let categoryTreeBuilding = $state(false)
  let categoryTreePollTimer = null
  let categoryTreePolling = false
  const CATEGORY_TREE_POLL_MS = 10000
  let sessionListElement = $state(null)
  let categoryTreeElement = $state(null)
  let recentSessionView = null
  let categorySessionViews = new Map()
  let directoryTreeScrollTop = 0
  let sessionLoadToken = 0

  // 弹出菜单状态
  let menuOpenId = $state(null)   // 当前展开菜单的 session id
  let menuPos = $state({ x: 0, y: 0 })  // fixed 定位坐标
  let userMessageMenuOpen = $state(false)
  let userMessageMenuLoading = $state(false)
  let userMessageMenuError = $state('')
  let menuUserMessages = $state([])
  let menuSessionData = $state(null)
  let executionAnalysisOpen = $state(false)
  let executionAnalysisLoading = $state(false)
  let executionAnalysisError = $state('')
  let executionAnalysisData = $state(null)
  let sessionDirectoryMenuOpen = $state(false)
  let sessionDirectoryMenuLoading = $state(false)
  let sessionDirectoryMenuError = $state('')
  let menuSessionDirectories = $state([])

  // hover 弹出菜单状态：鼠标悬停到 ... 按钮即弹出，悬停在菜单上保持显示
  let hoverBtnId = $state(null)   // 当前鼠标悬停的 ... 按钮 session id
  let hoverMenuId = $state(null)  // 当前鼠标悬停的弹出菜单 session id
  let closeTimer = null           // 延迟关闭定时器（跨越按钮与菜单之间的缝隙）

  // 拖动状态
  let isDragging = $state(false)
  let dragStartX = 0        // mousedown 时的 clientX
  let dragStartWidth = 0    // mousedown 时的侧边栏宽度

  // --- Session Status Stream ---
  // Maps session_id -> status string from SSE events
  let sessionStatuses = $state({})
  let flightSessions = $state(new Set())

  function _applyStatusToSessionList(sid, status) {
    sessionStatuses[sid] = status
    const idx = sessionList.findIndex(s => s.session_id === sid)
    if (idx >= 0) {
      // Update existing entry's status
      sessionList = sessionList.map(s =>
        s.session_id === sid ? { ...s, _status: status } : s
      )
    } else if (status === 'streaming' && !(directoryOpen && selectedCategory)) {
      // New session detected via SSE before newSessionCreated fires —
      // add it immediately so the user sees it during inference.
      // Title is a placeholder (session_id); it will be updated by
      // the newSessionCreated effect or a title_update SSE event.
      sessionList = [{ session_id: sid, title: sid, _status: status }, ...sessionList]
    }
  }

  let _unsubscribeSessionEvents = null

  onMount(() => {
    loadSessions()
    _unsubscribeSessionEvents = subscribeSessionEvents(
      (data) => {
        if (data.event === 'init') {
          // Merge all statuses from init snapshot
          const sids = data.sessions || {}
          flightSessions = new Set(data.flight_sessions || [])
          for (const [sid, status] of Object.entries(sids)) {
            _applyStatusToSessionList(sid, status)
          }
        } else if (data.event === 'message') {
          _applyStatusToSessionList(data.session_id, data.status)
        } else if (data.event === 'flight_mode') {
          const next = new Set(flightSessions)
          if (data.enabled) next.add(data.session_id)
          else next.delete(data.session_id)
          flightSessions = next
        } else if (data.event === 'title_update') {
          const sid = data.session_id
          const newTitle = data.title
          if (sid && newTitle) {
            sessionList = sessionList.map(s =>
              s.session_id === sid ? { ...s, title: newTitle } : s
            )
          }
        }
      },
      (_err) => {
        // SSE error — silently ignore; no automatic reconnect per spec
      },
    )
  })

  onDestroy(() => {
    stopCategoryTreePolling()
    if (_unsubscribeSessionEvents) {
      _unsubscribeSessionEvents()
      _unsubscribeSessionEvents = null
    }
  })

  /**
   * Derive a CSS class string for a session row based on its status.
   * Combines status with active state.
   */
  function getSessionStatusClass(entry) {
    const status = entry._status || sessionStatuses[entry.session_id] || ''
    if (status === 'streaming') return 'status-streaming'
    if (status === 'done_success_unread') return 'status-done-success-unread'
    if (status === 'done_error_unread') return 'status-done-error-unread'
    return ''
  }

  function mergeSessionLists(existing, incoming) {
    const seen = new Set(existing.map(s => s.session_id))
    const appended = incoming.filter(s => {
      if (!s?.session_id || seen.has(s.session_id)) return false
      seen.add(s.session_id)
      return true
    })
    return [...existing, ...appended]
  }

  async function loadSessions(append = false) {
    if (append && (!sessionHasMore || sessionLoading || sessionLoadingMore)) return

    const requestToken = ++sessionLoadToken
    const requestedCategory = directoryOpen ? selectedCategory : ''
    const requestedSearch = activeSearchQuery
    const nextPage = append ? sessionPage + 1 : 1
    if (append) {
      sessionLoadingMore = true
    } else {
      sessionLoading = true
      sessionPage = 1
      sessionHasMore = true
      sessionList = []
    }
    sessionError = ''
    try {
      const data = requestedSearch
        ? await sessions.search(requestedSearch, nextPage, SESSION_PAGE_SIZE)
        : await sessions.list(nextPage, SESSION_PAGE_SIZE, requestedCategory)
      if (requestToken !== sessionLoadToken) return
      // A list response contains persisted metadata but no live inference
      // status. Preserve the SSE snapshot/events that may have arrived while
      // this request was in flight; otherwise replacing sessionList briefly or
      // permanently drops the streaming style.
      const incoming = (data.sessions ?? []).map(entry => ({
        ...entry,
        _status: sessionStatuses[entry.session_id]
          || sessionList.find(current => current.session_id === entry.session_id)?._status
          || entry._status,
      }))
      if (append) {
        sessionList = mergeSessionLists(sessionList, incoming)
      } else {
        // A just-created inference is announced as streaming before its initial
        // user message has necessarily reached conversation.json/session index.
        // Keep those SSE-only rows until a later list response can supply the
        // persisted title and metadata.
        const incomingIds = new Set(incoming.map(entry => entry.session_id))
        const liveOnly = requestedCategory ? [] : sessionList.filter(entry =>
          !incomingIds.has(entry.session_id)
          && (sessionStatuses[entry.session_id] === 'streaming' || entry._status === 'streaming')
        )
        sessionList = [...liveOnly, ...incoming]
      }
      sessionPage = data.page ?? nextPage
      sessionHasMore = Boolean(data.has_more)
      if (append) sessionLoadingMore = false
      else sessionLoading = false
      await tick()
      if (directoryOpen && requestedCategory === selectedCategory) {
        categorySessionViews.set(requestedCategory, currentSessionView())
      } else if (!directoryOpen && !requestedCategory && !requestedSearch) {
        recentSessionView = currentSessionView()
      }
    } catch (err) {
      if (requestToken === sessionLoadToken) {
        sessionError = err.message || t('fetchSessionsFailed')
      }
    } finally {
      if (requestToken === sessionLoadToken) {
        if (append) {
          sessionLoadingMore = false
        } else {
          sessionLoading = false
        }
      }
    }
  }

  function handleSessionScroll(e) {
    const { scrollTop, scrollHeight, clientHeight } = e.target
    if (directoryOpen && selectedCategory) {
      const view = categorySessionViews.get(selectedCategory)
      if (view) view.scrollTop = scrollTop
    } else if (!directoryOpen && !activeSearchQuery && recentSessionView) {
      recentSessionView.scrollTop = scrollTop
    }
    if (scrollHeight - scrollTop - clientHeight < 50) {
      loadSessions(true)
    }
  }

  function currentSessionView() {
    return {
      sessionList: sessionList.map(entry => ({ ...entry })),
      sessionError,
      sessionPage,
      sessionHasMore,
      loaded: !sessionLoading && !sessionLoadingMore,
      scrollTop: sessionListElement?.scrollTop || 0,
    }
  }

  function saveCurrentSessionView() {
    if (sessionLoading || sessionLoadingMore) return
    if (directoryOpen && selectedCategory) {
      categorySessionViews.set(selectedCategory, currentSessionView())
    } else if (!directoryOpen && !activeSearchQuery) {
      recentSessionView = currentSessionView()
    }
  }

  async function restoreSessionView(view) {
    if (!view?.loaded) return false
    sessionLoadToken++
    sessionList = view.sessionList.map(entry => ({ ...entry }))
    sessionError = view.sessionError || ''
    sessionPage = view.sessionPage || 1
    sessionHasMore = Boolean(view.sessionHasMore)
    sessionLoading = false
    sessionLoadingMore = false
    await tick()
    if (sessionListElement) sessionListElement.scrollTop = view.scrollTop || 0
    return true
  }

  async function restoreDirectoryScroll() {
    await tick()
    if (categoryTreeElement) categoryTreeElement.scrollTop = directoryTreeScrollTop
  }

  function scrollItemWithinList(listElement, itemElement) {
    if (!listElement || !itemElement) return
    const listRect = listElement.getBoundingClientRect()
    const itemRect = itemElement.getBoundingClientRect()
    let nextScrollTop = listElement.scrollTop
    if (itemRect.top < listRect.top) {
      nextScrollTop += itemRect.top - listRect.top
    } else if (itemRect.bottom > listRect.bottom) {
      nextScrollTop += itemRect.bottom - listRect.bottom
    }
    if (nextScrollTop !== listElement.scrollTop) {
      listElement.scrollTo({ top: nextScrollTop, behavior: 'smooth' })
    }
  }

  async function scrollCategoryIntoView(path) {
    if (!path) return
    await tick()
    const row = [...(categoryTreeElement?.querySelectorAll('[data-category-path]') || [])]
      .find(element => element.dataset.categoryPath === path)
    scrollItemWithinList(categoryTreeElement, row)
  }

  async function scrollSessionIntoView(sessionId) {
    if (!sessionId) return
    await tick()
    const row = [...(sessionListElement?.querySelectorAll('[data-session-id]') || [])]
      .find(element => element.dataset.sessionId === sessionId)
    scrollItemWithinList(sessionListElement, row)
  }

  async function loadCategoryTree(force = false) {
    if (!force && categoryTree.length > 0 && !categoryTreeBuilding) return categoryTree
    if (categoryTreePromise) return categoryTreePromise
    categoryTreeLoading = true
    categoryTreeError = ''
    categoryTreePromise = sessions.tree()
      .then(data => {
        categoryTree = data.tree || []
        if (data.building) {
          // 后端正在后台生成分类树：显示提示并按固定间隔重试，直到 building 消失
          categoryTreeBuilding = true
          startCategoryTreePolling()
        } else {
          categoryTreeBuilding = false
          stopCategoryTreePolling()
        }
        // "所属目录"菜单打开期间树可能已变化（新会话挂载、后台构建完成等），同步刷新
        if (sessionDirectoryMenuOpen && menuOpenId) {
          menuSessionDirectories = findSessionDirectories(menuOpenId)
        }
        return categoryTree
      })
      .catch(err => {
        categoryTreeError = err.message || t('sessionDirectoryLoadFailed')
        return []
      })
      .finally(() => {
        categoryTreeLoading = false
        categoryTreePromise = null
      })
    return categoryTreePromise
  }

  function startCategoryTreePolling() {
    stopCategoryTreePolling()
    categoryTreePollTimer = setInterval(async () => {
      if (categoryTreePolling) return
      categoryTreePolling = true
      try {
        await loadCategoryTree(true)
      } finally {
        categoryTreePolling = false
      }
    }, CATEGORY_TREE_POLL_MS)
  }

  function stopCategoryTreePolling() {
    if (categoryTreePollTimer) {
      clearInterval(categoryTreePollTimer)
      categoryTreePollTimer = null
    }
  }

  function expandCategoryAncestors(path) {
    const parts = String(path || '').split('/').filter(Boolean)
    const next = new Set(categoryExpandedPaths)
    for (let index = 1; index < parts.length; index++) {
      next.add(parts.slice(0, index).join('/'))
    }
    categoryExpandedPaths = next
  }

  async function showDirectoryState() {
    saveCurrentSessionView()
    directoryOpen = true
    searchOpen = false
    activeSearchQuery = ''
    await loadCategoryTree()
    if (selectedCategory) {
      expandCategoryAncestors(selectedCategory)
      const restored = await restoreSessionView(categorySessionViews.get(selectedCategory))
      if (!restored) await loadSessions()
    } else {
      sessionLoadToken++
      sessionList = []
      sessionError = ''
      sessionPage = 1
      sessionHasMore = false
      sessionLoading = false
      sessionLoadingMore = false
    }
    await restoreDirectoryScroll()
  }

  async function toggleDirectory(e) {
    e.preventDefault()
    e.stopPropagation()
    if (directoryOpen) await showRecentSessions()
    else await showDirectoryState()
  }

  function toggleCategoryPath(path) {
    const next = new Set(categoryExpandedPaths)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    categoryExpandedPaths = next
  }

  async function selectCategory(path) {
    if (selectedCategory === path && sessionList.length > 0) return
    saveCurrentSessionView()
    selectedCategory = path
    activeSearchQuery = ''
    const restored = await restoreSessionView(categorySessionViews.get(path))
    if (!restored) await loadSessions()
  }

  async function showRecentSessions() {
    if (!directoryOpen && !activeSearchQuery) return
    saveCurrentSessionView()
    if (categoryTreeElement) directoryTreeScrollTop = categoryTreeElement.scrollTop
    directoryOpen = false
    searchOpen = false
    activeSearchQuery = ''
    const restored = await restoreSessionView(recentSessionView)
    if (!restored) await loadSessions()
  }

  function handleNewSession(e) {
    e.preventDefault()
    e.stopPropagation()
    closeMenu()
    newSessionRequest.token += 1
    navigate('#/chat')
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
  }

  function toggleSearch(e) {
    e.preventDefault()
    e.stopPropagation()
    directoryOpen = false
    selectedCategory = ''
    searchOpen = !searchOpen
    if (searchOpen) {
      // 等待 DOM 更新（{#if} 渲染 input）后再聚焦
      tick().then(() => {
        if (searchInput) searchInput.focus()
      })
    } else {
      // 收起搜索框 = 取消搜索，恢复默认列表
      activeSearchQuery = ''
      loadSessions()
    }
  }

  async function handleSearchKeydown(e) {
    if (e.key !== 'Enter') return
    e.preventDefault()
    activeSearchQuery = searchText.trim()
    await loadSessions()
  }

  async function handleSessionClick(sessionId) {
    const now = Date.now()
    if (now - lastClickTime < 300) {
      return
    }
    lastClickTime = now
    restoreError = ''
    // 移动端：立即收起侧边栏，不能等网络请求返回后再收起，
    // 否则 fixed 遮罩持续挡住对话内容，用户体验为"点两下"
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
    // 先进入 ChatPage，再开始下载。这样从 Setup 等页面切换会话时，
    // 500ms 后出现的下载进度条也有可见的挂载区域。
    navigate('#/chat')
    const downloadToken = sessionDownload.token + 1
    sessionDownload.token = downloadToken
    sessionDownload.loading = true
    sessionDownload.visible = false
    sessionDownload.received = 0
    sessionDownload.total = 0
    const progressDelay = setTimeout(() => {
      if (sessionDownload.loading && sessionDownload.token === downloadToken) {
        sessionDownload.visible = true
      }
    }, 500)
    try {
      const data = await sessions.get(sessionId, ({ received, total }) => {
        if (sessionDownload.token !== downloadToken) return
        sessionDownload.received = received
        sessionDownload.total = total
      })
      // A newer click owns the UI now; do not let this response replace it.
      if (sessionDownload.token !== downloadToken) return
      // 直接展开所有字段，保持与后端数据一致
      const msgs = (data.messages ?? []).map(m => ({ ...m }))
      const meta = data.meta ?? null
      sessionRestore.pending = { sessionId, messages: msgs, meta }
    } catch (err) {
      if (sessionDownload.token !== downloadToken) return
      restoreError = err.message || t('restoreSessionFailed')
      // 后端在 session not found 时会删除该记录，前端同步移除
      sessionList = sessionList.filter(s => s.session_id !== sessionId)
    } finally {
      clearTimeout(progressDelay)
      if (sessionDownload.token === downloadToken) {
        sessionDownload.loading = false
        sessionDownload.visible = false
        sessionDownload.received = 0
        sessionDownload.total = 0
      }
    }
  }

  function openMenuAt(e, sid) {
    e.stopPropagation()
    const btn = e.currentTarget
    const rect = btn.getBoundingClientRect()
    // 菜单出现在按钮右下角，用 fixed 定位浮于最顶层
    menuPos = { x: rect.right + 4, y: rect.top }
    if (menuOpenId !== sid) {
      userMessageMenuOpen = false
      userMessageMenuLoading = false
      userMessageMenuError = ''
      menuUserMessages = []
      menuSessionData = null
      executionAnalysisOpen = false
      executionAnalysisLoading = false
      executionAnalysisError = ''
      executionAnalysisData = null
      sessionDirectoryMenuOpen = false
      sessionDirectoryMenuLoading = false
      sessionDirectoryMenuError = ''
      menuSessionDirectories = []
    }
    menuOpenId = sid
  }

  function openMenu(e, sid) {
    e.stopPropagation()
    openMenuAt(e, sid)
  }

  function closeMenu() {
    menuOpenId = null
    userMessageMenuOpen = false
    userMessageMenuLoading = false
    userMessageMenuError = ''
    menuUserMessages = []
    menuSessionData = null
    executionAnalysisOpen = false
    executionAnalysisLoading = false
    executionAnalysisError = ''
    executionAnalysisData = null
    sessionDirectoryMenuOpen = false
    sessionDirectoryMenuLoading = false
    sessionDirectoryMenuError = ''
    menuSessionDirectories = []
    hoverBtnId = null
    hoverMenuId = null
    if (closeTimer) {
      clearTimeout(closeTimer)
      closeTimer = null
    }
  }

  // --- hover 弹出/关闭逻辑 ---
  // 鼠标进入 ... 按钮：立即弹出菜单
  function handleMenuBtnEnter(e, sid) {
    hoverBtnId = sid
    cancelClose()
    openMenuAt(e, sid)
  }

  // 鼠标离开 ... 按钮：延迟关闭（给鼠标移入弹出菜单留出时间）
  function handleMenuBtnLeave() {
    hoverBtnId = null
    scheduleClose()
  }

  // 鼠标进入弹出菜单：保持稳定显示，取消关闭
  function handleMenuEnter() {
    if (menuOpenId !== null) hoverMenuId = menuOpenId
    cancelClose()
  }

  // 鼠标离开弹出菜单：延迟关闭
  function handleMenuLeave() {
    hoverMenuId = null
    scheduleClose()
  }

  // 仅当鼠标既不在 ... 按钮上也不在弹出菜单上时才真正关闭
  function scheduleClose() {
    if (closeTimer) clearTimeout(closeTimer)
    closeTimer = setTimeout(() => {
      closeTimer = null
      if (hoverBtnId === null && hoverMenuId === null) {
        menuOpenId = null
      }
    }, 150)
  }

  function cancelClose() {
    if (closeTimer) {
      clearTimeout(closeTimer)
      closeTimer = null
    }
  }

  function submenuPanelWidth(preferred = 600) {
    if (typeof window === 'undefined') return preferred
    const chatAreaWidth = Math.max(320, window.innerWidth - sidebarWidth.current)
    const availableWidth = Math.max(260, window.innerWidth - menuPos.x - 210)
    return Math.min(preferred, chatAreaWidth * 0.85, availableWidth)
  }

  function userMessageMenuWidth() {
    return submenuPanelWidth(600)
  }

  function formatDuration(ms) {
    const value = Number(ms) || 0
    if (value < 1000) return `${Math.round(value)} ms`
    if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`
    if (value < 3600000) {
      const minutes = Math.floor(value / 60000)
      const seconds = ((value % 60000) / 1000).toFixed(1)
      return `${minutes}m ${seconds}s`
    }
    const hours = Math.floor(value / 3600000)
    const minutes = Math.floor((value % 3600000) / 60000)
    const seconds = Math.floor((value % 60000) / 1000)
    return `${hours}h ${minutes}m ${seconds}s`
  }

  function analysisName(item, kind) {
    if (kind === 'agent') return item.agent_id || 'null'
    if (kind === 'model') return item.model_label ? `${item.model_id || 'null'} (${item.model_label})` : (item.model_id || 'null')
    return item.tool_name ? `${item.tool_name}${item.tool_id && item.tool_id !== item.tool_name ? ` (${item.tool_id})` : ''}` : (item.tool_id || 'null')
  }

  async function openExecutionAnalysis(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (executionAnalysisOpen) return
    userMessageMenuOpen = false
    sessionDirectoryMenuOpen = false
    executionAnalysisOpen = true
    executionAnalysisLoading = true
    executionAnalysisError = ''
    executionAnalysisData = null
    const requestedSessionId = sessionId
    try {
      const data = await sessions.executionAnalysis(sessionId)
      if (menuOpenId !== requestedSessionId || !executionAnalysisOpen) return
      executionAnalysisData = data
    } catch (err) {
      if (menuOpenId === requestedSessionId) {
        executionAnalysisError = err.message || t('executionAnalysisFailed')
      }
    } finally {
      if (menuOpenId === requestedSessionId) executionAnalysisLoading = false
    }
  }

  function userMessageText(content) {
    if (typeof content === 'string') return content.replace(/\s+/g, ' ').trim()
    if (Array.isArray(content)) {
      return content.map(part => typeof part === 'string' ? part : (part?.text || '')).join(' ').replace(/\s+/g, ' ').trim()
    }
    return content == null ? '' : String(content).replace(/\s+/g, ' ').trim()
  }

  function findSessionDirectories(sessionId) {
    const matches = []
    function visit(nodes, pathIds = [], pathNames = []) {
      for (const node of nodes || []) {
        if (!node || typeof node !== 'object') continue
        const ids = [...pathIds, String(node.id)]
        const names = [...pathNames, node.name || String(node.id)]
        const childNodes = (node.children || []).filter(child => child && typeof child === 'object')
        if (childNodes.length === 0 && (node.children || []).includes(sessionId)) {
          matches.push({ path: ids.join('/'), names, label: names.join(' → ') })
        }
        visit(childNodes, ids, names)
      }
    }
    visit(categoryTree)
    return matches.sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: 'base' }))
  }

  async function openSessionDirectoryMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (sessionDirectoryMenuOpen) return
    userMessageMenuOpen = false
    executionAnalysisOpen = false
    sessionDirectoryMenuOpen = true
    sessionDirectoryMenuLoading = true
    sessionDirectoryMenuError = ''
    menuSessionDirectories = []
    const requestedSessionId = sessionId
    await loadCategoryTree()
    if (menuOpenId !== requestedSessionId || !sessionDirectoryMenuOpen) return
    if (categoryTreeError) {
      sessionDirectoryMenuError = categoryTreeError
    } else {
      menuSessionDirectories = findSessionDirectories(sessionId)
      // 查不到且不在后台构建中：本地缓存树可能滞后于磁盘 tree.json，
      // 强制拉取一次（结果由 loadCategoryTree 回调自动同步到菜单）
      if (menuSessionDirectories.length === 0 && !categoryTreeBuilding) {
        await loadCategoryTree(true)
      }
    }
    sessionDirectoryMenuLoading = false
  }

  async function handleSessionDirectoryClick(e, directory) {
    e.stopPropagation()
    const targetPath = directory.path
    const targetSessionId = menuOpenId
    closeMenu()
    saveCurrentSessionView()
    if (categoryTreeElement) directoryTreeScrollTop = categoryTreeElement.scrollTop
    directoryOpen = true
    searchOpen = false
    activeSearchQuery = ''
    await loadCategoryTree()
    expandCategoryAncestors(targetPath)
    selectedCategory = targetPath
    const restored = await restoreSessionView(categorySessionViews.get(targetPath))
    if (!restored) await loadSessions()
    await restoreDirectoryScroll()
    await scrollCategoryIntoView(targetPath)
    await scrollSessionIntoView(targetSessionId)
  }

  async function openUserMessageMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (userMessageMenuOpen) return

    sessionDirectoryMenuOpen = false
    executionAnalysisOpen = false
    userMessageMenuOpen = true
    userMessageMenuLoading = true
    userMessageMenuError = ''
    menuUserMessages = []
    menuSessionData = null
    const requestedSessionId = sessionId
    try {
      const data = await sessions.get(sessionId)
      if (menuOpenId !== requestedSessionId || !userMessageMenuOpen) return
      const messages = (data.messages ?? []).map(m => ({ ...m }))
      menuSessionData = { messages, meta: data.meta ?? null }
      menuUserMessages = messages
        .map((message, index) => ({ index, text: userMessageText(message.content) }))
        .filter(item => messages[item.index]?.role === 'user')
    } catch (err) {
      if (menuOpenId === requestedSessionId) {
        userMessageMenuError = err.message || t('restoreSessionFailed')
      }
    } finally {
      if (menuOpenId === requestedSessionId) userMessageMenuLoading = false
    }
  }

  function handleUserMessageClick(e, sessionId, messageIndex) {
    e.stopPropagation()
    const data = menuSessionData
    if (!data) return

    if (currentSession.sessionId !== sessionId) {
      sessionRestore.pending = { sessionId, messages: data.messages, meta: data.meta }
    }
    messageScrollRequest.sessionId = sessionId
    messageScrollRequest.messageIndex = messageIndex
    messageScrollRequest.token++
    closeMenu()
    navigate('#/chat')
    if (window.innerWidth < 1024) collapseSidebar()
  }

  function handleOpenTerminal(e, sessionId) {
    e.stopPropagation()
    closeMenu()
    terminalOpen.sessionId = sessionId
    terminalOpen.token++
  }

  // 打开会话日志目录：请求后端返回 conversation.json 所在目录，
  // 通知 ChatPage 显示文件管理器面板并导航到该目录
  async function handleOpenSessionLogDir(e, sessionId) {
    e.stopPropagation()
    closeMenu()
    try {
      const data = await sessions.logDir(sessionId)
      if (!data?.path) throw new Error(t('openSessionLogDirFailed') || 'Failed to resolve log directory')
      openSessionLogDir.path = data.path
      openSessionLogDir.token++
    } catch (err) {
      restoreError = err.message || t('openSessionLogDirFailed')
    }
  }

  // 移动端长按手势：长按会话条目 500ms 后弹出操作菜单
  let longPressTimer = $state(null)

  function handleRowTouchStart(e, sid) {
    // 记录触摸位置，后续移动超过 10px 则取消长按（区分滚动）
    const touch = e.touches[0]
    longPressTimer = setTimeout(() => {
      longPressTimer = null
      // 在触摸位置弹出菜单
      menuPos = { x: touch.clientX, y: touch.clientY }
      menuOpenId = sid
    }, 500)
  }

  function handleRowTouchMove(e) {
    if (longPressTimer) {
      clearTimeout(longPressTimer)
      longPressTimer = null
    }
  }

  function handleRowTouchEnd(_e, _sid) {
    if (longPressTimer) {
      clearTimeout(longPressTimer)
      longPressTimer = null
    }
  }

  async function handleGenerateTitle(e, sid) {
    e.stopPropagation()
    closeMenu()
    try {
      const result = await sessions.generateTitle(sid)
      if (result.status === 'success') {
        // 更新本地列表中的标题
        sessionList = sessionList.map(s => 
          s.session_id === sid ? { ...s, title: result.title } : s
        )
      }
    } catch (err) {
      restoreError = err.message || t('generateTitleFailed')
    }
  }

  async function handleRegenerateSummary(e, sid) {
    e.stopPropagation()
    closeMenu()
    try {
      const result = await sessions.regenerateSummary(sid)
      if (result.status !== 'success') {
        restoreError = t('regenerateSummaryFailed')
      }
    } catch (err) {
      restoreError = err.message || t('regenerateSummaryFailed')
    }
  }

  async function handleToggleFlight(e, sid) {
    e.stopPropagation()
    const enabled = !flightSessions.has(sid)
    closeMenu()
    try {
      await sessions.setFlightMode(sid, enabled)
      const next = new Set(flightSessions)
      if (enabled) next.add(sid)
      else next.delete(sid)
      flightSessions = next
    } catch (err) {
      restoreError = err.message || t('flightModeFailed')
    }
  }

 async function handleDeleteSession(e, sid) {
    e.stopPropagation()
    closeMenu()
   // 优先展示会话标题，标题缺失或与 id 相同时回退为只显示 id
    const entry = sessionList.find(s => s.session_id === sid)
    const title = entry?.title
    const hasTitle = !!(title && title !== sid)
    if (!confirm(t(hasTitle ? 'confirmDeleteSession' : 'confirmDeleteSessionById', { title, id: sid }))) return
    try {
      await sessions.delete(sid)
      sessionList = sessionList.filter(s => s.session_id !== sid)
      // 通知 ChatPage 同步清空右侧面板
      if (currentSession.sessionId === sid) {
        sessionDeleted.sessionId = sid
      }
    } catch (err) {
      restoreError = err.message || t('deleteSessionFailed')
    }
  }

  // 拖动处理：记录起始点，用增量计算，避免按钮偏移导致宽度跳变
  function handleDragStart(e) {
    // 只响应鼠标左键
    if (e.type === 'mousedown' && e.button !== 0) return
    e.preventDefault()
    isDragging = false  // 先不标记，等真正移动再标记
    dragStartX = e.type.startsWith('touch') ? e.touches[0].clientX : e.clientX
    dragStartWidth = sidebarWidth.current
    document.addEventListener('mousemove', handleDragMove)
    document.addEventListener('mouseup', handleDragEnd)
    document.addEventListener('touchmove', handleDragMove, { passive: false })
    document.addEventListener('touchend', handleDragEnd)
  }

  function handleDragMove(e) {
    e.preventDefault()
    const clientX = e.type.startsWith('touch') ? e.touches[0].clientX : e.clientX
    const delta = clientX - dragStartX
    // 超过 3px 才认为是拖动，避免误触
    if (!isDragging && Math.abs(delta) < 3) return
    isDragging = true
    const newWidth = Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, dragStartWidth + delta))
    setSidebarWidth(newWidth)
  }

  function handleDragEnd() {
    document.removeEventListener('mousemove', handleDragMove)
    document.removeEventListener('mouseup', handleDragEnd)
    document.removeEventListener('touchmove', handleDragMove)
    document.removeEventListener('touchend', handleDragEnd)
    // 延迟重置 isDragging，让 click 事件能检测到
    setTimeout(() => { isDragging = false }, 0)
  }

  function handleToggleClick(e) {
    // 如果刚刚发生了拖动，不触发收缩切换
    if (isDragging) return
    toggleSidebarCollapsed()
  }

  function handleSetupClick(e) {
    // 不只依赖 <a href> 的默认 hash 跳转：
    // 1. 移动端侧边栏是 fixed 浮层，跳转后如果不收起，页面已切换但仍被侧边栏挡住，容易被感知为“点击无反应”；
    // 2. 当前已在 Setup 页时再次点击不会触发 hashchange，主动通知 SetupPage 回到默认入口。
    e.preventDefault()
    closeMenu()
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
    navigate('#/setup')
    window.dispatchEvent(new CustomEvent('setup:reset'))
  }


  /**
   * 将 session_id（YYMMDD_HHmmss）解析为 "MM/DD HH:mm:ss" 格式的时间字符串。
   * 解析失败时返回空字符串。
   */
  function sessionIdToTime(sessionId) {
    const m = /^(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(sessionId)
    if (!m) return ''
    const [, , MM, DD, HH, mm, ss] = m
    return `${MM}/${DD} ${HH}:${mm}:${ss}`
  }

  /**
   * 计算会话条目的显示标题和 tooltip。
   * - 长度 < 10：display 追加时间字符串
   * - 长度 > 30：display 截断至 30 字符
   * - tooltip 始终为完整标题 + 时间（供 CSS ellipsis 和手动截断两种情况使用）
   * 返回 { display, tooltip }
   */
  function getSessionDisplay(entry) {
    const raw = entry.title || entry.session_id
    const time = sessionIdToTime(entry.session_id)
    // tooltip 始终携带完整内容，浏览器在文字被 CSS 截断时也会显示
    const tooltip = time ? `${raw}\n${time}` : raw
    let display = raw

    if (raw.length < 10) {
      display = time ? `${raw}  ${time}` : raw
    } else if (raw.length > 30) {
      display = raw.slice(0, 30) + '…'
    }
    return { display, tooltip }
  }

  // 监听新会话创建，动态添加到列表
  $effect(() => {
    const sid = newSessionCreated.sessionId
    if (sid) {
      const firstMsg = newSessionCreated.firstUserMessage
      const title = newSessionCreated.title
          || (firstMsg && firstMsg.trim() ? firstMsg.trim() : sid)
      const exists = sessionList.some(s => s.session_id === sid)
      if (!exists) {
        // 动态添加新会话条目到列表顶部。搜索过滤状态下仅当标题/首条消息命中时显示，
        // 避免破坏当前过滤结果；清空搜索后会正常显示所有会话。
        const searchable = `${title} ${firstMsg || ''} ${sid}`.toLowerCase()
        if (!(directoryOpen && selectedCategory) && (!activeSearchQuery || searchable.includes(activeSearchQuery.toLowerCase()))) {
          sessionList = [{ session_id: sid, title }, ...sessionList]
        }
      } else {
        // Session already in list (added by SSE streaming event with placeholder
        // title). Update the title with the proper one from onInit.
        sessionList = sessionList.map(s =>
          s.session_id === sid ? { ...s, title } : s
        )
      }
      // 重置状态，避免重复处理
      newSessionCreated.sessionId = null
      newSessionCreated.firstUserMessage = null
      newSessionCreated.title = null
    }
  })
</script>

<!-- 点击空白处关闭菜单 -->
<svelte:window onclick={closeMenu} />

<!-- 浮层菜单：fixed 定位，渲染在 sidebar 之外确保不被裁剪 -->
{#if menuOpenId !== null}
  <div
    class="session-dropdown"
    role="menu"
    style="left:{menuPos.x}px; top:{menuPos.y}px;"
    onclick={(e) => e.stopPropagation()}
    onmouseenter={handleMenuEnter}
    onmouseleave={handleMenuLeave}
  >
    <div class="session-dropdown-submenu-row" class:active={userMessageMenuOpen}>
      <div class="session-dropdown-item session-dropdown-submenu-label">
       <span class="menu-emoji">💬</span>
        <span>{t('userMessages')}</span>
      </div>
      <button
        class="submenu-more-btn"
        aria-label={t('userMessages')}
        aria-haspopup="menu"
        aria-expanded={userMessageMenuOpen}
        onclick={(e) => openUserMessageMenu(e, menuOpenId)}
        onmouseenter={(e) => openUserMessageMenu(e, menuOpenId)}
      >···</button>
    </div>
    {#if userMessageMenuOpen}
      <div
        class="user-message-submenu"
        role="menu"
        style="width:{userMessageMenuWidth()}px;"
      >
        {#if userMessageMenuLoading}
          <div class="user-message-submenu-status">{t('loading')}</div>
        {:else if userMessageMenuError}
          <div class="user-message-submenu-status error">{userMessageMenuError}</div>
        {:else if menuUserMessages.length === 0}
          <div class="user-message-submenu-status">{t('noUserMessages')}</div>
        {:else}
          <div class="user-message-list">
            {#each menuUserMessages as item (item.index)}
              <button
                class="user-message-item"
                role="menuitem"
                title={item.text || t('emptyUserMessage')}
                onclick={(e) => handleUserMessageClick(e, menuOpenId, item.index)}
              >{item.text || t('emptyUserMessage')}</button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
    <div class="session-dropdown-submenu-row" class:active={sessionDirectoryMenuOpen}>
        <div class="session-dropdown-item session-dropdown-submenu-label">
          <span class="menu-emoji">🌲</span>
          <span>{t('sessionDirectories')}</span>
        </div>
        <button
          class="submenu-more-btn"
          aria-label={t('sessionDirectories')}
          aria-haspopup="menu"
          aria-expanded={sessionDirectoryMenuOpen}
          onclick={(e) => openSessionDirectoryMenu(e, menuOpenId)}
          onmouseenter={(e) => openSessionDirectoryMenu(e, menuOpenId)}
        >···</button>
      </div>
      {#if sessionDirectoryMenuOpen}
        <div
          class="session-directory-submenu"
          role="menu"
          style="width:{submenuPanelWidth(520)}px;"
        >
          {#if sessionDirectoryMenuLoading}
            <div class="user-message-submenu-status">{t('loading')}</div>
          {:else if sessionDirectoryMenuError}
            <div class="user-message-submenu-status error">{sessionDirectoryMenuError}</div>
          {:else if menuSessionDirectories.length === 0}
            <div class="user-message-submenu-status">{t('noSessionDirectories')}</div>
          {:else}
            <div class="user-message-list">
              {#each menuSessionDirectories as directory (directory.path)}
                <button
                  class="user-message-item session-directory-item"
                  role="menuitem"
                  title={directory.label}
                  onclick={(e) => handleSessionDirectoryClick(e, directory)}
                >
                  {#each directory.names as name, index}
                    {#if index > 0}<span class="directory-path-arrow">→</span>{/if}
                    <span>{name}</span>
                  {/each}
                </button>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleGenerateTitle(e, menuOpenId)}
    >
     <span class="menu-emoji">🖋️</span>
      {t('generateTitle')}
    </button>
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleRegenerateSummary(e, menuOpenId)}
    >
     <span class="menu-emoji">📝</span>
      {t('regenerateSummary')}
    </button>
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleOpenTerminal(e, menuOpenId)}
    >
      <span class="menu-emoji">💻</span>
      Open Terminal
    </button>
    <div class="session-dropdown-submenu-row" class:active={executionAnalysisOpen}>
      <div class="session-dropdown-item session-dropdown-submenu-label">
      <span class="menu-emoji">📊</span>
        <span>{t('executionAnalysis')}</span>
      </div>
      <button
        class="submenu-more-btn"
        aria-label={t('executionAnalysis')}
        aria-haspopup="menu"
        aria-expanded={executionAnalysisOpen}
        onclick={(e) => openExecutionAnalysis(e, menuOpenId)}
        onmouseenter={(e) => openExecutionAnalysis(e, menuOpenId)}
      >···</button>
    </div>
    {#if executionAnalysisOpen}
      <div
        class="execution-analysis-submenu"
        role="menu"
        style="width:{submenuPanelWidth(520)}px;"
      >
        {#if executionAnalysisLoading}
          <div class="user-message-submenu-status">{t('loading')}</div>
        {:else if executionAnalysisError}
          <div class="user-message-submenu-status error">{executionAnalysisError}</div>
        {:else if executionAnalysisData}
          <div class="execution-analysis-content">
            <div class="analysis-summary-list">
              <div class="analysis-summary-row"><span>{t('totalExecutionNetTime')}</span><strong>{formatDuration(executionAnalysisData.summary?.total_execution_net_ms)}</strong></div>
              <div class="analysis-summary-row"><span>{t('modelExecutionTotalTime')}</span><strong>{formatDuration(executionAnalysisData.summary?.model_execution_total_ms)}</strong></div>
              <div class="analysis-summary-row"><span>{t('toolExecutionTotalTime')}</span><strong>{formatDuration(executionAnalysisData.summary?.tool_execution_total_ms)}</strong></div>
            </div>
            <div class="analysis-section">
              <div class="analysis-section-title">{t('analysisByAgent')}</div>
              {#if (executionAnalysisData.by_agent || []).length === 0}
                <div class="analysis-empty">{t('analysisNoData')}</div>
              {:else}
                {#each executionAnalysisData.by_agent as item}
                  <div class="analysis-list-row">
                    <span class="analysis-name" title={analysisName(item, 'agent')}>{analysisName(item, 'agent')}</span>
                    <span class="analysis-meta">M {formatDuration(item.model_duration_ms)} · T {formatDuration(item.tool_duration_ms)}</span>
                    <strong>{formatDuration(item.total_duration_ms)}</strong>
                  </div>
                {/each}
              {/if}
            </div>
            <div class="analysis-section">
              <div class="analysis-section-title">{t('analysisByModel')}</div>
              {#if (executionAnalysisData.by_model || []).length === 0}
                <div class="analysis-empty">{t('analysisNoData')}</div>
              {:else}
                {#each executionAnalysisData.by_model as item}
                  <div class="analysis-list-row">
                    <span class="analysis-name" title={analysisName(item, 'model')}>{analysisName(item, 'model')}</span>
                    <span class="analysis-meta">{item.calls} {t('analysisCalls')} · {item.input_tokens}/{item.output_tokens} tok</span>
                    <strong>{formatDuration(item.duration_ms)}</strong>
                  </div>
                {/each}
              {/if}
            </div>
            <div class="analysis-section">
              <div class="analysis-section-title">{t('analysisByTool')}</div>
              {#if (executionAnalysisData.by_tool || []).length === 0}
                <div class="analysis-empty">{t('analysisNoData')}</div>
              {:else}
                {#each executionAnalysisData.by_tool as item}
                  <div class="analysis-list-row">
                    <span class="analysis-name" title={analysisName(item, 'tool')}>{analysisName(item, 'tool')}</span>
                    <span class="analysis-meta">{item.calls} {t('analysisCalls')}</span>
                    <strong>{formatDuration(item.duration_ms)}</strong>
                  </div>
                {/each}
              {/if}
            </div>
          </div>
        {/if}
      </div>
    {/if}
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleOpenSessionLogDir(e, menuOpenId)}
    >
    <span class="menu-emoji">📂</span>
      {t('openSessionLogDir')}
    </button>
    <button
      class="session-dropdown-item"
      role="menuitemcheckbox"
      aria-checked={flightSessions.has(menuOpenId)}
      onclick={(e) => handleToggleFlight(e, menuOpenId)}
    >
      <span class="menu-emoji">✈️</span>
      <span>{t('flightMode')}</span>
      {#if flightSessions.has(menuOpenId)}<span class="menu-check">✓</span>{/if}
    </button>
    <button
      class="session-dropdown-item session-dropdown-danger"
      role="menuitem"
      onclick={(e) => handleDeleteSession(e, menuOpenId)}
    >
    <span class="menu-emoji">🗑️</span>
      {t('deleteSession')}
    </button>
  </div>
{/if}

<aside class="sidebar" class:collapsed={sidebarWidth.collapsed} style="width: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px">
  <nav class="nav">
    <div class="nav-row">
      <button
        class="nav-action-btn new-session-top-btn"
        class:active={router.current === '#/chat' && !currentSession.sessionId}
        onclick={handleNewSession}
        title={t('newSession')}
        aria-label={t('newSession')}
      >
        <span>{t('nav_chat')}</span>
        <span class="plus-mark">✚</span>
      </button>
      <button
        class="nav-action-btn search-toggle-btn"
        class:active={searchOpen}
        onclick={toggleSearch}
        title={t('searchSessionsTooltip')}
        aria-label={t('searchSessions')}
      >🔍</button>
      <a
        href="#/setup"
        class="nav-action-btn setup-top-btn"
        class:active={router.current.split('?')[0] === '#/setup'}
        onclick={handleSetupClick}
        title={t('nav_setup')}
        aria-label={t('nav_setup')}
      >⚙️</a>
    </div>
    {#if searchOpen}
      <input
        class="session-search-input"
        type="text"
        bind:this={searchInput}
        bind:value={searchText}
        onkeydown={handleSearchKeydown}
        placeholder={t('searchSessions')}
        title={t('searchSessionsTooltip')}
      />
    {/if}
  </nav>
  <!-- 最近会话 / 分类目录面板 -->
  <div class="session-panel">
    <div class="session-panel-heading">
      <button class="session-panel-title" class:active={!directoryOpen} onclick={showRecentSessions}>{t('sessionPanelTitle')}</button>
      <button class="directory-toggle" class:active={directoryOpen} onclick={toggleDirectory} title={t('sessionDirectory')}>
        <span class="directory-glyph">🌲</span>{t('sessionDirectory')}
      </button>
    </div>
    {#if directoryOpen}
      <div class="category-tree" bind:this={categoryTreeElement} onscroll={(e) => { directoryTreeScrollTop = e.currentTarget.scrollTop }}>
        {#if categoryTreeBuilding}
          <div class="category-tree-building" role="status">
            <span class="category-tree-building-dot" aria-hidden="true"></span>
            {t('sessionTreeBuilding')}
          </div>
        {/if}
        {#if categoryTreeLoading && categoryTree.length === 0}
          <div class="session-loading">{t('loading')}</div>
        {:else if categoryTreeError && categoryTree.length === 0}
          <div class="session-error">{categoryTreeError}</div>
        {:else if categoryTree.length === 0 && !categoryTreeBuilding}
          <div class="session-empty">{t('sessionDirectoryEmpty')}</div>
        {:else}
          {#each categoryTree as node (node.id)}
            <SessionCategoryNode
              node={{ ...node, category: String(node.id) }}
              expandedPaths={categoryExpandedPaths}
              {selectedCategory}
              onToggle={toggleCategoryPath}
              onSelect={selectCategory}
            />
          {/each}
        {/if}
      </div>
    {/if}
    {#if !directoryOpen || selectedCategory}
      <div class="session-list" bind:this={sessionListElement} onscroll={handleSessionScroll}>
        {#if sessionLoading && sessionList.length === 0}
          <div class="session-loading">{t('loading')}</div>
        {:else if sessionError && sessionList.length === 0}
          <div class="session-error">{sessionError}</div>
        {:else if sessionList.length === 0}
          <div class="session-empty">{t('noSessions')}</div>
        {:else}
          {#each sessionList as entry (entry.session_id)}
            <div
              class="session-row"
              data-session-id={entry.session_id}
              ontouchstart={(e) => handleRowTouchStart(e, entry.session_id)}
              ontouchmove={handleRowTouchMove}
              ontouchend={(e) => handleRowTouchEnd(e, entry.session_id)}
            >
              <button
                class="session-item {getSessionStatusClass(entry)}"
                class:active={entry.session_id === currentSession.sessionId}
                onclick={() => handleSessionClick(entry.session_id)}
                ontouchend={(e) => { e.preventDefault(); handleSessionClick(entry.session_id) }}
                title={getSessionDisplay(entry).tooltip}
              >
                {getSessionDisplay(entry).display}
                {#if flightSessions.has(entry.session_id)}<span class="session-flight-check">✓</span>{/if}
              </button>
              <button
                class="session-menu-btn"
                onclick={(e) => openMenu(e, entry.session_id)}
                onmouseenter={(e) => handleMenuBtnEnter(e, entry.session_id)}
                onmouseleave={handleMenuBtnLeave}
                aria-label={t('deleteSession')}
              >···</button>
            </div>
          {/each}
          {#if sessionLoadingMore}
            <div class="session-loading session-loading-more">{t('loading')}</div>
          {/if}
          {#if sessionError}
            <div class="session-error">{sessionError}</div>
          {/if}
        {/if}
        {#if restoreError}
          <div class="session-error">{restoreError}</div>
        {/if}
      </div>
    {/if}
  </div>
</aside>

<!-- 统一的收缩/展开按钮，固定在侧边栏右边缘顶部，与主内容顶栏重叠 -->
<button
  class="sidebar-toggle-btn"
  style="left: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px;"
  onclick={handleToggleClick}
  onmousedown={handleDragStart}
  ontouchstart={handleDragStart}
  aria-label={sidebarWidth.collapsed ? t('expandSidebar') : t('collapseSidebar')}
  title={sidebarWidth.collapsed ? t('expandSidebar') : t('collapseSidebar')}
>
  {#if sidebarWidth.collapsed}
    <svg class="toggle-arrow" viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="1,1 7,7 1,13"/>
    </svg>
  {:else}
    <svg class="toggle-arrow" viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="7,1 1,7 7,13"/>
    </svg>
  {/if}
</button>

<style>
  .sidebar {
    height: 100vh;
    height: 100dvh; /* 移动端动态视口高度，自动适配浏览器地址栏显隐 */
    overflow: hidden;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    transition: width 0.2s ease;
  }
  .sidebar.collapsed {
    border-right: none;
    overflow: hidden;
  }
  .nav {
    display: flex;
    flex-direction: column;
    padding: 12px 10px 8px 10px;
    flex-shrink: 0;
  }
  .nav-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .nav-action-btn {
    min-width: 32px;
    height: 32px;
    padding: 0 9px;
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    line-height: 1;
    font-size: 0.9rem;
    font-weight: 600;
    white-space: nowrap;
    transition: border-color 0.15s, color 0.15s;
  }
  .nav-action-btn:hover {
    background: transparent;
    color: var(--text);
  }
  .nav-action-btn.active {
    background: transparent;
    border-color: rgba(59, 130, 246, 0.45);
    color: var(--text);
  }
  .new-session-top-btn {
    flex: 0 0 auto;
  }
  .plus-mark {
    font-size: 1rem;
  }
  .search-toggle-btn {
    margin-left: auto;
    font-size: 0.95rem;
  }
  .setup-top-btn {
    text-decoration: none;
    font-size: 0.95rem;
  }
  .session-search-input {
    width: 100%;
    box-sizing: border-box;
    margin: 8px 0 0 0;
    padding: 7px 9px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
  }
  .session-search-input:focus {
    outline: none;
    border-color: var(--primary);
  }
  .sidebar-toggle-btn {
    position: fixed;
    top: 12px;
    /* left 由 style 属性动态设置 */
    width: fit-content;
    min-width: 0;
    height: 32px;
    padding: 0 2px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-left: none;
    border-radius: 0 6px 6px 0;
    cursor: ew-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 180;
    transition: background-color 0.15s, left 0.2s ease;
  }
  .sidebar-toggle-btn:hover {
    background: var(--border);
  }
  .toggle-arrow {
    width: 8px;
    height: 14px;
    color: var(--text-secondary);
    display: block;
    pointer-events: none;
  }
  .session-panel {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .session-panel-heading {
    min-height: 28px;
    display: flex;
    align-items: stretch;
    flex-shrink: 0;
  }
  .session-panel-title,
  .directory-toggle {
    padding: 6px 8px 4px 10px;
    border: none;
    background: none;
    color: var(--text-secondary);
    font-size: 0.78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background-color 0.15s, color 0.15s, opacity 0.15s;
  }
  .session-panel-title {
    flex: 1;
    text-align: left;
  }
  .directory-toggle {
    margin-left: auto;
    padding-left: 6px;
  }
  .session-panel-title:hover,
  .directory-toggle:hover {
    color: var(--text);
    opacity: 1;
  }
  .session-panel-title:not(.active),
  .directory-toggle:not(.active) { opacity: 0.55; }
  .session-panel-title.active,
  .directory-toggle.active { color: var(--text); opacity: 1; }
  .directory-glyph { margin-right: 3px; }
  .category-tree {
    max-height: 46%;
    min-height: 0;
    overflow-y: auto;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    padding: 3px 0;
    flex-shrink: 1;
  }
  .category-tree-building {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 0 6px 10px;
    font-size: 0.8rem;
    color: var(--text-secondary);
    flex-shrink: 0;
  }
  .category-tree-building-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent, #4c9aff);
    animation: category-tree-pulse 1.2s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes category-tree-pulse {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 1; }
  }
  .session-list {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .session-row {
    position: relative;
    display: flex;
    align-items: center;
    overflow: hidden;
    flex-shrink: 0; /* 禁止条目高度被压缩，确保超出时触发滚动 */
  }
  .session-item {
    flex: 1;
    min-width: 0;
    padding: 7px 6px 7px 10px;
    text-align: left;
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: 0.82rem;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: background-color 0.15s, color 0.15s;
  }
  .session-item:hover {
    background-color: var(--border);
    color: var(--text);
  }
  .session-item.active {
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  /* Session Status Stream indicator styles */
  .session-item.status-streaming {
    color: var(--warning);
    font-style: italic;
  }
  .session-item.status-done-success-unread {
    color: var(--success);
    font-weight: 600;
  }
  .session-item.status-done-error-unread {
    color: var(--danger, #e53e3e);
    font-weight: 600;
  }
  .session-menu-btn {
    position: absolute;
    right: 6px;
    width: 26px;
    height: 26px;
    padding: 0;
    background: none;
    border: none;
    border-radius: 5px;
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    opacity: 0;
    pointer-events: none; /* 默认禁止交互，防止移动端误触吞掉点击事件 */
    transition: opacity 0.15s, background-color 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .session-row:hover .session-menu-btn {
    opacity: 1;
    pointer-events: auto; /* 桌面端 hover 时才恢复交互能力 */
  }
  .session-menu-btn:hover {
    background-color: var(--border);
    color: var(--text);
  }
  .session-dropdown {
    position: fixed;
    z-index: 9999;
    min-width: 180px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
    padding: 4px 0;
    overflow: visible;
  }
  .session-dropdown-item {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 16px;
    text-align: left;
    background: none;
    border: none;
    font-size: 0.88rem;
    cursor: pointer;
    transition: background-color 0.15s;
    color: var(--text);
  }
  .session-dropdown-item:hover,
  .session-dropdown-item.active {
    background-color: var(--border);
  }
  .session-dropdown-submenu-row {
    position: relative;
    display: flex;
    align-items: center;
  }
  .session-dropdown-submenu-row:hover,
  .session-dropdown-submenu-row.active {
    background-color: var(--border);
  }
  .session-dropdown-submenu-label {
    flex: 1;
    min-width: 0;
    padding-right: 4px;
    cursor: default;
  }
  .session-dropdown-submenu-label:hover {
    background: transparent;
  }
  .session-dropdown-submenu-label span {
    min-width: 0;
  }
  .submenu-more-btn {
    flex-shrink: 0;
    width: 30px;
    height: 26px;
    margin-right: 6px;
    padding: 0;
    border: none;
    border-radius: 5px;
    background: transparent;
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
  }
  .submenu-more-btn:hover {
    background: var(--bg);
    color: var(--text);
  }
  .user-message-submenu,
  .session-directory-submenu,
  .execution-analysis-submenu {
    position: absolute;
    left: calc(100% + 6px);
    top: 0;
    max-height: min(70vh, 620px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
  }
  .execution-analysis-content {
    min-height: 0;
    overflow-y: auto;
    padding: 10px 12px 12px;
  }
  .analysis-summary-list {
    display: grid;
    gap: 7px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .analysis-summary-row,
  .analysis-list-row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    font-size: 0.82rem;
  }
  .analysis-summary-row span {
    flex: 1;
    color: var(--text-secondary);
  }
  .analysis-summary-row strong,
  .analysis-list-row strong {
    flex-shrink: 0;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .analysis-section {
    padding-top: 11px;
  }
  .analysis-section-title {
    margin-bottom: 6px;
    color: var(--text-secondary);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .analysis-list-row {
    padding: 5px 0;
  }
  .analysis-name {
    flex: 1 1 42%;
    min-width: 0;
    overflow: hidden;
    color: var(--text);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .analysis-meta {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-secondary);
    font-size: 0.75rem;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .analysis-empty {
    padding: 5px 0;
    color: var(--text-secondary);
    font-size: 0.8rem;
  }
  .user-message-list {
    min-height: 0;
    overflow-y: auto;
    padding: 4px 0;
  }
  .user-message-item {
    display: block;
    width: 100%;
    padding: 8px 12px;
    overflow: hidden;
    border: none;
    background: none;
    color: var(--text);
    font: inherit;
    font-size: 0.84rem;
    line-height: 1.25;
    text-align: left;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: pointer;
  }
  .user-message-item:hover {
    background: var(--border);
  }
  .session-directory-item {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .session-directory-item span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .directory-path-arrow {
    flex-shrink: 0;
    color: var(--text-secondary);
  }
  .user-message-submenu-status {
    padding: 14px 12px;
    color: var(--text-secondary);
    font-size: 0.82rem;
  }
  .user-message-submenu-status.error {
    color: var(--danger, #e53e3e);
  }
  .session-dropdown-danger {
    color: var(--danger, #e53e3e);
  }
  .session-dropdown-danger:hover {
    background-color: rgba(229, 62, 62, 0.08);
  }
 .menu-check { margin-left: auto; font-weight: 700; color: #22c55e; }
  .session-flight-check { margin-left: .35rem; color: #22c55e; font-weight: 700; }

  .menu-emoji {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    flex-shrink: 0;
    font-size: 14px;
    line-height: 1;
  }
  .session-loading, .session-empty, .session-error {
    padding: 6px 0 6px 10px;
    font-size: 0.82rem;
    color: var(--text-secondary);
    flex-shrink: 0;
  }
  .session-error { color: var(--danger); }
  
  /* 滚动条样式：默认隐藏，悬停时显示 */
  .session-list::-webkit-scrollbar {
    width: 8px;
  }
  .session-list::-webkit-scrollbar-track {
    background: transparent;
  }
  .session-list::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 4px;
    transition: background 0.2s;
  }
  .session-list:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
  .session-list:hover::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }
</style>
