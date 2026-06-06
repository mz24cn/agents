<script>
  import { onMount, onDestroy, tick } from 'svelte'
  import { router, navigate } from '../lib/router.svelte.js'
  import { t } from '../lib/i18n.svelte.js'
  import { sessions, subscribeSessionEvents } from '../lib/api.js'
  import { sessionRestore, newSessionCreated, sessionDeleted, currentSession, newSessionRequest } from '../lib/session-state.svelte.js'
  import { sidebarWidth, setSidebarWidth, toggleSidebarCollapsed, collapseSidebar, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from '../lib/sidebar-width.svelte.js'

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

  // 弹出菜单状态
  let menuOpenId = $state(null)   // 当前展开菜单的 session id
  let menuPos = $state({ x: 0, y: 0 })  // fixed 定位坐标

  // 拖动状态
  let isDragging = $state(false)
  let dragStartX = 0        // mousedown 时的 clientX
  let dragStartWidth = 0    // mousedown 时的侧边栏宽度

  // --- Session Status Stream ---
  // Maps session_id -> status string from SSE events
  let sessionStatuses = $state({})

  function _applyStatusToSessionList(sid, status) {
    sessionStatuses[sid] = status
    const idx = sessionList.findIndex(s => s.session_id === sid)
    if (idx >= 0) {
      // Update existing entry's status
      sessionList = sessionList.map(s =>
        s.session_id === sid ? { ...s, _status: status } : s
      )
    } else if (status === 'streaming') {
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
          for (const [sid, status] of Object.entries(sids)) {
            _applyStatusToSessionList(sid, status)
          }
        } else if (data.event === 'message') {
          _applyStatusToSessionList(data.session_id, data.status)
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
      const data = activeSearchQuery
        ? await sessions.search(activeSearchQuery, nextPage, SESSION_PAGE_SIZE)
        : await sessions.list(nextPage, SESSION_PAGE_SIZE)
      const incoming = data.sessions ?? []
      sessionList = append ? mergeSessionLists(sessionList, incoming) : incoming
      sessionPage = data.page ?? nextPage
      sessionHasMore = Boolean(data.has_more)
    } catch (err) {
      sessionError = err.message || t('fetchSessionsFailed')
    } finally {
      if (append) {
        sessionLoadingMore = false
      } else {
        sessionLoading = false
      }
    }
  }

  function handleSessionScroll(e) {
    const { scrollTop, scrollHeight, clientHeight } = e.target
    if (scrollHeight - scrollTop - clientHeight < 50) {
      loadSessions(true)
    }
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
    try {
      const data = await sessions.get(sessionId)
      // 直接展开所有字段，保持与后端数据一致
      const msgs = (data.messages ?? []).map(m => ({ ...m }))
      const meta = data.meta ?? null
      sessionRestore.pending = { sessionId, messages: msgs, meta }
      // 恢复会话后跳转到对话页，同时同步浏览器 hash
      navigate('#/chat')
    } catch (err) {
      restoreError = err.message || t('restoreSessionFailed')
      // 后端在 session not found 时会删除该记录，前端同步移除
      sessionList = sessionList.filter(s => s.session_id !== sessionId)
    }
  }

  function openMenu(e, sid) {
    e.stopPropagation()
    if (menuOpenId === sid) {
      menuOpenId = null
      return
    }
    const btn = e.currentTarget
    const rect = btn.getBoundingClientRect()
    // 菜单出现在按钮右下角，用 fixed 定位浮于最顶层
    menuPos = { x: rect.right + 4, y: rect.top }
    menuOpenId = sid
  }

  function closeMenu() {
    menuOpenId = null
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

  async function handleDeleteSession(e, sid) {
    e.stopPropagation()
    closeMenu()
    if (!confirm(t('confirmDeleteSession', { id: sid }))) return
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
        if (!activeSearchQuery || searchable.includes(activeSearchQuery.toLowerCase())) {
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
  >
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleGenerateTitle(e, menuOpenId)}
    >
      <svg class="menu-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M2 4h12M2 8h10M2 12h8"/>
        <circle cx="13" cy="12" r="2.5" fill="none"/>
        <line x1="14.5" y1="13.5" x2="16" y2="15"/>
      </svg>
      {t('generateTitle')}
    </button>
    <button
      class="session-dropdown-item session-dropdown-danger"
      role="menuitem"
      onclick={(e) => handleDeleteSession(e, menuOpenId)}
    >
      <svg class="menu-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <polyline points="3,4 13,4"/>
        <path d="M5 4V3a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v1"/>
        <rect x="4" y="4" width="8" height="9" rx="1"/>
        <line x1="6.5" y1="7" x2="6.5" y2="11"/>
        <line x1="9.5" y1="7" x2="9.5" y2="11"/>
      </svg>
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
  <!-- 最近会话面板 -->
  <div class="session-panel">
    <div class="session-panel-title">{t('sessionPanelTitle')}</div>
    <div class="session-list" onscroll={handleSessionScroll}>
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
            </button>
            <button
              class="session-menu-btn"
              onclick={(e) => openMenu(e, entry.session_id)}
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
  .session-panel-title {
    padding: 6px 0 4px 10px;
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-secondary);
    opacity: 0.45;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    flex-shrink: 0;
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
    min-width: 130px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
    padding: 4px 0;
    overflow: hidden;
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
  .session-dropdown-item:hover {
    background-color: var(--border);
  }
  .session-dropdown-danger {
    color: var(--danger, #e53e3e);
  }
  .session-dropdown-danger:hover {
    background-color: rgba(229, 62, 62, 0.08);
  }
  .menu-icon {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
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
