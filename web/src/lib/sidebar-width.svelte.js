/**
 * Sidebar width management module using Svelte 5 runes.
 *
 * Persists the sidebar width to localStorage and provides
 * drag/resize functionality.
 */

const STORAGE_KEY = 'sidebar-width'
const DEFAULT_WIDTH = 220
const MIN_WIDTH = 120
const MAX_WIDTH = 500

function getInitialWidth() {
  if (typeof localStorage !== 'undefined') {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored) {
      const parsed = parseInt(stored, 10)
      if (!isNaN(parsed) && parsed >= MIN_WIDTH && parsed <= MAX_WIDTH) {
        return parsed
      }
    }
  }
  return DEFAULT_WIDTH
}

/** Sidebar width state — use sidebarWidth.current to read the current width. */
export const sidebarWidth = $state({ 
  current: getInitialWidth(),
  collapsed: false,
  widthBeforeCollapse: getInitialWidth(),  // 收缩前的宽度，展开时恢复
})

/**
 * Set sidebar width and persist to localStorage.
 * @param {number} width
 */
export function setSidebarWidth(width) {
  const clamped = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, width))
  sidebarWidth.current = clamped
  localStorage.setItem(STORAGE_KEY, String(clamped))
}

/**
 * Toggle sidebar collapsed state.
 * 收缩时保存当前宽度，展开时恢复。
 */
export function toggleSidebarCollapsed() {
  if (sidebarWidth.collapsed) {
    // 展开：恢复收缩前的宽度
    sidebarWidth.current = sidebarWidth.widthBeforeCollapse
    sidebarWidth.collapsed = false
  } else {
    // 收缩：先保存当前宽度
    sidebarWidth.widthBeforeCollapse = sidebarWidth.current
    sidebarWidth.collapsed = true
  }
}

/**
 * Collapse the sidebar.
 */
export function collapseSidebar() {
  sidebarWidth.collapsed = true
}

/**
 * Expand the sidebar.
 */
export function expandSidebar() {
  sidebarWidth.collapsed = false
}

export const SIDEBAR_MIN_WIDTH = MIN_WIDTH
export const SIDEBAR_MAX_WIDTH = MAX_WIDTH
