import { models as modelsApi, tools as toolsApi, promptTemplates as promptTemplatesApi, agents as agentsApi } from './api.js'

// Shared SPA-side catalog state for resources that are selected/edited from
// multiple pages. ChatPage stays mounted while setup pages are shown, so local
// component-only fetches can easily become stale. Keeping these lists here gives
// all mounted selectors/list pages the same live data source.
export const catalog = $state({
  models: { items: [], loading: false, error: '', loaded: false, version: 0 },
  tools: { items: [], loading: false, error: '', loaded: false, version: 0 },
  promptTemplates: { items: [], loading: false, error: '', loaded: false, version: 0 },
  agents: { items: [], loading: false, error: '', loaded: false, version: 0 },
})

const pending = {}
const sequence = { models: 0, tools: 0, promptTemplates: 0, agents: 0 }

async function loadResource(key, request, pickItems, { force = false } = {}) {
  const state = catalog[key]
  if (state.loaded && !force) return state.items
  if (pending[key] && !force) return pending[key]

  const seq = ++sequence[key]
  state.loading = true
  state.error = ''

  const promise = request()
    .then((data) => {
      const items = pickItems(data) ?? []
      // If a newer refresh has already started, do not let this older response
      // overwrite the shared list.
      if (seq === sequence[key]) {
        state.items = items
        state.loaded = true
        state.version += 1
      }
      return items
    })
    .catch((err) => {
      if (seq === sequence[key]) {
        state.error = err?.message || 'Request failed'
      }
      throw err
    })
    .finally(() => {
      if (seq === sequence[key]) {
        state.loading = false
      }
      if (pending[key] === promise) {
        pending[key] = null
      }
    })

  pending[key] = promise
  return promise
}

export function loadModels(options) {
  return loadResource('models', modelsApi.list, (data) => data.models, options)
}

export function refreshModels() {
  return loadModels({ force: true })
}

export function loadTools(options) {
  return loadResource('tools', toolsApi.list, (data) => data.tools, options)
}

export function refreshTools() {
  return loadTools({ force: true })
}

export function loadPromptTemplates(options) {
  return loadResource('promptTemplates', promptTemplatesApi.list, (data) => data.templates, options)
}

export function refreshPromptTemplates() {
  return loadPromptTemplates({ force: true })
}

export function loadAgents(options) {
  return loadResource('agents', agentsApi.list, (data) => data.agents, options)
}

export function refreshAgents() {
  return loadAgents({ force: true })
}
