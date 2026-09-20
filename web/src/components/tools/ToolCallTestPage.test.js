// @vitest-environment jsdom
/**
 * Component regression test for the "base64 自动编码/解码" opt-in on the tool
 * call test page.
 *
 * The failure mode this guards against: the checkbox markup and the
 * ``tools.call(..., { base64Auto })``透传 were missing from the component, so
 * the feature existed in ``api.js``/``i18n.svelte.js``/backend but was
 * completely invisible in the UI.  Asserting the rendered checkbox keeps that
 * from silently regressing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import ToolCallTestPage from './ToolCallTestPage.svelte'
import { t, setLang } from '../../lib/i18n.svelte.js'

const TOOL = {
  tool_id: 'mcp_shot',
  tool_type: 'mcp',
  name: 'shot',
  tool_name: 'shot',
  mcp_server_name: 'srv',
  parameters: { type: 'object', properties: { base64_content: { type: 'string' } } },
}

let mounted = []

function render() {
  const target = document.createElement('div')
  document.body.appendChild(target)
  const app = mount(ToolCallTestPage, { target, props: { tool: TOOL, onCancel: () => {} } })
  flushSync()
  mounted.push({ app, target })
  return target
}

async function flushPromises() {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
}

function okResponse(text) {
  return { ok: true, status: 200, text: async () => text, json: async () => null }
}

function checkboxOf(target) {
  return target.querySelector('input[type="checkbox"]')
}

function submitButtonOf(target) {
  return target.querySelector('.btn-primary')
}

function setChecked(box, checked) {
  box.checked = checked
  box.dispatchEvent(new Event('change', { bubbles: true }))
  flushSync()
}

beforeEach(() => {
  setLang('zh')
  mounted = []
})

afterEach(() => {
  for (const { app, target } of mounted) {
    unmount(app)
    target.remove()
  }
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ToolCallTestPage base64 auto toggle', () => {
  it('renders the checkbox with its tooltip, right-aligned with the args label', () => {
    const target = render()

    const box = checkboxOf(target)
    expect(box).toBeTruthy()
    expect(box.closest('label').className).toContain('b64-toggle')
    expect(box.closest('.field-label-row')).toBeTruthy()

    const label = box.closest('label')
    expect(label.textContent).toContain(t('toolTestBase64Auto'))
    expect(label.getAttribute('title')).toBe(t('toolTestBase64AutoHint'))
    expect(box.disabled).toBe(false)
    expect(box.checked).toBe(false)
  })

  it('forwards the opt-in as X-Agents-Request-Context only when checked', async () => {
    const calls = []
    vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
      calls.push({ url, opts })
      return okResponse('{"data":"ok"}')
    }))

    const target = render()

    // Unchecked -> raw executor: no transport-context header.
    submitButtonOf(target).click()
    await flushPromises()
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe('/v1/tools/call')
    expect(calls[0].opts.headers['X-Agents-Request-Context']).toBeUndefined()

    // Checked -> header carries {"base64":"auto"} (padded base64url).
    setChecked(checkboxOf(target), true)
    submitButtonOf(target).click()
    await flushPromises()
    expect(calls).toHaveLength(2)
    const header = calls[1].opts.headers['X-Agents-Request-Context']
    expect(header).toMatch(/^[A-Za-z0-9_-]+={0,2}$/)
    expect(JSON.parse(Buffer.from(header, 'base64url').toString('utf-8')))
      .toEqual({ base64: 'auto' })
  })

  it('disables the checkbox while the call is running', async () => {
    let release
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => {
      release = () => resolve(okResponse('ok'))
    })))

    const target = render()
    const box = checkboxOf(target)

    setChecked(box, true)
    expect(box.disabled).toBe(false)

    submitButtonOf(target).click()
    await flushPromises()
    expect(box.disabled).toBe(true)

    release()
    await flushPromises()
    expect(box.disabled).toBe(false)
  })
})
