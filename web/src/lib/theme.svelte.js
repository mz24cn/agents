/**
 * Theme management module using Svelte 5 runes.
 *
 * Persists the user's preference to localStorage and falls back to the
 * system's prefers-color-scheme media query on first visit.
 */

function getSystemTheme() {
  if (typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    return 'dark'
  }
  return 'light'
}

function getInitialTheme() {
  if (typeof localStorage !== 'undefined') {
    const stored = localStorage.getItem('theme')
    if (stored === 'light' || stored === 'dark') return stored
  }
  return getSystemTheme()
}

/** Theme state object — use themeState.current to read the current theme. */
export const themeState = $state({ current: getInitialTheme() })

// Apply the initial theme to the document immediately
if (typeof document !== 'undefined') {
  document.documentElement.dataset.theme = themeState.current
}

/**
 * Toggle between light and dark themes.
 * Updates the DOM attribute and persists to localStorage.
 */
export function toggleTheme() {
  themeState.current = themeState.current === 'light' ? 'dark' : 'light'
  document.documentElement.dataset.theme = themeState.current
  localStorage.setItem('theme', themeState.current)
}
