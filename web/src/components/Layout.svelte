<script>
  import Sidebar from './Sidebar.svelte'
  import { t } from '../lib/i18n.svelte.js'
  import { sidebarWidth, setSidebarWidth, toggleSidebarCollapsed, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from '../lib/sidebar-width.svelte.js'

  let { children } = $props()

  // 拖拽状态
  let isDragging = $state(false)
  let dragStartX = 0
  let dragStartWidth = 0

  function handleDragStart(e) {
    if (e.type === 'mousedown' && e.button !== 0) return
    e.preventDefault()
    isDragging = false
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
    setTimeout(() => { isDragging = false }, 0)
  }

  function handleToggleClick() {
    if (isDragging) return
    toggleSidebarCollapsed()
  }
</script>

<div class="layout">
  <!-- Sidebar wrapper -->
  <div class="sidebar-wrapper" class:open={!sidebarWidth.collapsed} style="width: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px">
    <Sidebar />
  </div>

  <!-- Sidebar toggle button: fixed 定位，不在 sidebar-wrapper 内，移动端 display:none 不影响 -->
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

  <main class="content">
    {@render children()}
  </main>
</div>

<style>
  .layout {
    display: flex;
    min-height: 100vh;
  }
  .sidebar-wrapper {
    display: flex;
    transition: width 0.2s ease;
  }
  .content {
    flex: 1;
    overflow: hidden;
    position: relative;
  }

  .sidebar-toggle-btn {
    position: fixed;
    bottom: 0;
    /* left 由 style 属性动态设置 */
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

  @media (max-width: 1023px) {
    .sidebar-wrapper {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      z-index: 150;
      height: 100vh;
      width: auto !important;
    }
    .sidebar-wrapper.open {
      display: flex;
    }
    .content {
      padding-top: 56px;
    }
  }
</style>
