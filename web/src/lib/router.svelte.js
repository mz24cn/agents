/**
 * Hash-based SPA router using Svelte 5 runes.
 *
 * Routes are simple hash fragments: #/chat, #/models, #/tools, #/prompts.
 * Default route is #/chat.
 * Setup page supports query param: #/setup?tab=models
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
 * @param {string} hash  e.g. "#/models" or "#/setup?tab=models"
 */
export function navigate(hash) {
  // Keep the reactive router state and the browser URL in sync.
  // Assigning the same hash to window.location.hash does not fire `hashchange`,
  // so update router.current explicitly as well. This also protects against
  // callers that previously changed router.current without changing the URL.
  if (router.current !== hash) {
    router.current = hash
  }
  if (window.location.hash !== hash) {
    window.location.hash = hash
  }
}

/**
 * Parse current hash and query params.
 * @returns {{ path: string, params: Record<string, string> }}
 */
export function parseRoute() {
  const hash = router.current || '#/chat'
  const [base, query] = hash.slice(1).split('?')
  const params = {}
  if (query) {
    for (const pair of query.split('&')) {
      const [key, value] = pair.split('=')
      if (key) params[decodeURIComponent(key)] = decodeURIComponent(value || '')
    }
  }
  return { path: '#' + base, params }
}

/**
 * Get query param value.
 * @param {string} key
 * @returns {string}
 */
export function getQueryParam(key) {
  const { params } = parseRoute()
  return params[key] || ''
}
