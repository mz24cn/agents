<script>
  import Sidebar from './Sidebar.svelte'
  import { sidebarWidth } from '../lib/sidebar-width.svelte.js'

  let { children } = $props()
</script>

<div class="layout">
  <!-- Sidebar wrapper -->
  <div class="sidebar-wrapper" class:open={!sidebarWidth.collapsed} style="width: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px">
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
