<script>
  import Sidebar from './Sidebar.svelte'
  import { t } from '../lib/i18n.svelte.js'
  import { sidebarWidth } from '../lib/sidebar-width.svelte.js'

  let { children } = $props()

  let sidebarOpen = $state(false)

  function toggleSidebar() {
    sidebarOpen = !sidebarOpen
  }

  function closeSidebar() {
    sidebarOpen = false
  }
</script>

<div class="layout">
  <!-- Mobile hamburger button -->
  <button class="hamburger" onclick={toggleSidebar} aria-label={t('openMenu')}>
    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <line x1="3" y1="6" x2="21" y2="6"/>
      <line x1="3" y1="12" x2="21" y2="12"/>
      <line x1="3" y1="18" x2="21" y2="18"/>
    </svg>
  </button>

  <!-- Mobile overlay -->
  {#if sidebarOpen}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="mobile-overlay" onclick={closeSidebar} onkeydown={() => {}}></div>
  {/if}

  <div class="sidebar-wrapper" class:open={sidebarOpen} style="width: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px">
    <Sidebar />
  </div>

  <main class="content">
    {@render children()}
  </main>
</div>

<style>
  .layout {
    display: flex;
    min-height: 100vh;
  }
  .hamburger {
    display: none;
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 200;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px;
    border-radius: 6px;
    cursor: pointer;
  }
  .mobile-overlay {
    display: none;
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

  @media (max-width: 1023px) {
    .hamburger {
      display: flex;
      align-items: center;
      justify-content: center;
    }
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
    .mobile-overlay {
      display: block;
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(0, 0, 0, 0.4);
      z-index: 100;
    }
    .content {
      padding-top: 56px;
    }
  }
</style>
