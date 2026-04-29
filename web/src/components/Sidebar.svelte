<script>
  import { router } from '../lib/router.svelte.js'
  import ThemeToggle from './ThemeToggle.svelte'
  import { t, i18n, setLang } from '../lib/i18n.svelte.js'
  import { sessions } from '../lib/api.js'
  import { sessionRestore, newSessionCreated } from '../lib/session-state.svelte.js'
  import { sidebarWidth, setSidebarWidth, toggleSidebarCollapsed, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from '../lib/sidebar-width.svelte.js'

  const navItems = [
    { hash: '#/chat', key: 'nav_chat' },
    { hash: '#/models', key: 'nav_models' },
    { hash: '#/tools', key: 'nav_tools' },
    { hash: '#/prompts', key: 'nav_prompts' },
  ]

  let sessionList = $state([])
  let sessionError = $state('')
  let sessionLoading = $state(false)
  let restoreError = $state('')

  // 弹出菜单状态
  let menuOpenId = $state(null)   // 当前展开菜单的 session id
  let menuPos = $state({ x: 0, y: 0 })  // fixed 定位坐标

  // 拖动状态
  let isDragging = $state(false)
  let dragStartX = 0        // mousedown 时的 clientX
  let dragStartWidth = 0    // mousedown 时的侧边栏宽度
  // footer 高度，用于对齐 fixed 按钮
  let footerEl = $state(null)
  let footerHeight = $derived(footerEl ? footerEl.offsetHeight : 49)

  async function loadSessions() {
    sessionLoading = true
    sessionError = ''
    try {
      const data = await sessions.list()
      sessionList = data.sessions ?? []
    } catch (err) {
      sessionError = err.message || t('fetchSessionsFailed')
    } finally {
      sessionLoading = false
    }
  }

  async function handleSessionClick(sessionId) {
    restoreError = ''
    try {
      const data = await sessions.get(sessionId)
      const msgs = (data.messages ?? []).map(m => {
        const msg = { role: m.role, content: m.content }
        if (m.thinking != null) msg.thinking = m.thinking
        if (m.tool_calls != null) msg.tool_calls = m.tool_calls
        if (m.name != null) msg.name = m.name
        if (m.stat != null) msg.stat = m.stat
        if (m.images != null) msg.images = m.images
        if (m.audio != null) msg.audio = m.audio
        if (m.prompt_template != null) msg.prompt_template = m.prompt_template
        if (m.arguments != null) msg.arguments = m.arguments
        return msg
      })
      sessionRestore.pending = { sessionId, messages: msgs }
      // 恢复会话后跳转到对话页
      router.current = '#/chat'
    } catch (err) {
      restoreError = err.message || t('restoreSessionFailed')
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

  $effect(() => { loadSessions() })

  // 监听新会话创建，动态添加到列表
  $effect(() => {
    const sid = newSessionCreated.sessionId
    if (sid) {
      // 检查是否已存在
      const exists = sessionList.some(s => s.session_id === sid)
      if (!exists) {
        // 动态添加新会话条目到列表顶部
        sessionList = [{ session_id: sid, title: sid }, ...sessionList]
      }
      // 重置状态，避免重复处理
      newSessionCreated.sessionId = null
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
    {#each navItems as item}
      <a
        href={item.hash}
        class="nav-item"
        class:active={router.current === item.hash}
      >
        {t(item.key)}
      </a>
    {/each}
  </nav>
  <!-- 历史会话面板 -->
  <div class="session-panel">
    <div class="session-panel-title">{t('sessionPanelTitle')}</div>
    <div class="session-list">
      {#if sessionLoading}
        <div class="session-loading">{t('loading')}</div>
      {:else if sessionError}
        <div class="session-error">{sessionError}</div>
      {:else if sessionList.length === 0}
        <div class="session-empty">{t('noSessions')}</div>
      {:else}
        {#each sessionList as entry (entry.session_id)}
          <div class="session-row">
            <button class="session-item" onclick={() => handleSessionClick(entry.session_id)}>
              {entry.title}
            </button>
            <button
              class="session-menu-btn"
              onclick={(e) => openMenu(e, entry.session_id)}
              aria-label={t('deleteSession')}
            >···</button>
          </div>
        {/each}
      {/if}
      {#if restoreError}
        <div class="session-error">{restoreError}</div>
      {/if}
    </div>
  </div>
  <div class="sidebar-footer" bind:this={footerEl}>
    <div class="footer-theme-wrap">
      <ThemeToggle />
    </div>
    <button
      class="lang-btn"
      onclick={() => setLang(i18n.lang === 'zh' ? 'en' : 'zh')}
      title={i18n.lang === 'zh' ? 'Switch to English' : '切换为中文'}
    >{i18n.lang === 'zh' ? '中' : 'En'}</button>
    <a
      href="#/env"
      class="env-btn"
      class:active={router.current === '#/env'}
      title={t('nav_env')}
    >Env</a>
  </div>
</aside>

<!-- 统一的收缩/展开按钮，固定在侧边栏右边缘底部 -->
<button
  class="sidebar-toggle-btn"
  style="left: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px; height: {footerHeight}px;"
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
    padding: 16px 0;
    flex-shrink: 0;
  }
  .nav-item {
    display: block;
    padding: 12px 20px;
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.95rem;
    transition: background-color 0.15s, color 0.15s;
  }
  .nav-item:hover {
    background-color: var(--border);
    color: var(--text);
  }
  .nav-item.active {
    color: var(--primary);
    background-color: var(--bg);
    font-weight: 600;
    border-right: 3px solid var(--primary);
  }
  .sidebar-footer {
    padding: 12px 12px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: row;
    gap: 8px;
    align-items: stretch;
  }
  .sidebar-toggle-btn {
    position: fixed;
    bottom: 0;
    /* left & height 由 style 属性动态设置 */
    width: fit-content;
    min-width: 0;
    padding: 0 2px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-left: none;
    border-radius: 0 6px 6px 0;
    cursor: ew-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
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
  .footer-theme-wrap {
    flex: 1;
    display: flex;
  }
  .footer-theme-wrap :global(.theme-toggle) {
    flex: 1;
    width: 100%;
    padding: 6px 0;
    font-size: 1rem;
  }
  .lang-btn {
    flex: 1;
    padding: 6px 0;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
    text-align: center;
  }
  .lang-btn:hover {
    background: var(--border);
    color: var(--text);
  }
  .env-btn {
    flex: 1;
    padding: 6px 0;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
    text-align: center;
    text-decoration: none;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .env-btn:hover {
    background: var(--border);
    color: var(--text);
  }
  .env-btn.active {
    color: var(--primary);
    background: var(--bg);
    border-color: var(--primary);
  }
  .session-panel {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .session-panel-title {
    padding: 6px 20px 4px;
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-secondary);
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
  }
  .session-item {
    flex: 1;
    min-width: 0;
    padding: 7px 4px 7px 20px;
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
  .session-menu-btn {
    flex-shrink: 0;
    width: 26px;
    height: 26px;
    margin-right: 6px;
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
    transition: opacity 0.15s, background-color 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .session-row:hover .session-menu-btn {
    opacity: 1;
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
    padding: 6px 20px;
    font-size: 0.82rem;
    color: var(--text-secondary);
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
