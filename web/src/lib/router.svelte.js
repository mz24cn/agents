/**
 * Hash-based SPA router using Svelte 5 runes.
 *
 * Routes are simple hash fragments: #/chat, #/models, #/tools, #/prompts.
 * Default route is #/chat.
 */

/** Router state object — use router.current to read the current route. */
export const router = $state({ current: window.location.hash || '#/chat' })

// Ensure the URL has a hash on first load
if (!window.location.hash) {
  window.location.hash = '#/chat'
}

// Keep router.current in sync with the browser hash
window.addEventListener('hashchange', () => {
  router.current = window.location.hash || '#/chat'
})

/**
 * Navigate to a new hash route.
 * @param {string} hash  e.g. "#/models"
 */
export function navigate(hash) {
  window.location.hash = hash
}
