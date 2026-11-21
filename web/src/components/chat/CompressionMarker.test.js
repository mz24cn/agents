// @vitest-environment jsdom
/**
 * Component tests for the in-conversation context-compression marker.
 *
 * The requirement these pin down: a compression shows up inside the conversation
 * the way a tool call does (icon + label capsule, outcome capsule, click for the
 * details), and it is there when the session is reopened -- read straight from
 * conversation.json's `messages[i].meta.compressions`, with no stream involved.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import CompressionMarker from './CompressionMarker.svelte'
import MessageBubble from './MessageBubble.svelte'
import { setLang } from '../../lib/i18n.svelte.js'

let host = null
let instance = null

function render(Component, props) {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Component, { target: host, props })
  flushSync()
}

afterEach(() => {
  instance && unmount(instance)
  instance = null
  host?.remove()
  host = null
  document.body.innerHTML = ''
})

/** What the backend writes into messages[i].meta.compressions. */
const finishedEntry = {
  attempt: 'a1',
  trigger: 'in_loop',
  model_id: 'summary',
  tokens: 92000,
  delta_start: 12,
  delta_turns: 39,
  summary_version: 19,
  summary_chars: 1194,
  memory_entries: 73,
  duration_ms: 76700,
  success: true,
  error: '',
  at: '2026-01-01T00:00:00',
}

const pendingEntry = {
  attempt: 'a2',
  trigger: 'post_turn',
  model_id: 'summary',
  tokens: 70000,
  delta_turns: 2,
  summary_version: 19,
  phase: 'started',
}

const capsules = () => Array.from(document.querySelectorAll('.compression-call'))
const outcomes = () => Array.from(document.querySelectorAll('.compression-result'))

describe('CompressionMarker (compact mode, like a tool call)', () => {
  it('renders one capsule pair per recorded attempt', () => {
    render(CompressionMarker, { entries: [finishedEntry, finishedEntry], compact: true })
    expect(capsules()).toHaveLength(2)
    expect(outcomes()).toHaveLength(2)
    expect(capsules()[0].textContent).toContain('\u{1F5DC}\uFE0F')
  })

  it('states a finished attempt by what it produced, and ends on a tick', () => {
    setLang('zh')
    render(CompressionMarker, { entries: [finishedEntry], compact: true })
    expect(capsules()[0].textContent).toContain('上下文已压缩')
    // v2:1194字符,73条目 -- summary version, summary size, memory entries.
    expect(capsules()[0].textContent).toContain('v19:1194字符,73条目')
    // What is not shown stays reachable: the detail block has it all.
    expect(capsules()[0].textContent).not.toContain('trigger=')
    expect(outcomes()[0].textContent.trim()).toBe('\u2714\uFE0F')
  })

  it('a running attempt pulses and says so', () => {
    setLang('zh')
    render(CompressionMarker, { entries: [pendingEntry], compact: true })
    expect(capsules()[0].textContent).toContain('正在压缩会话上下文')
    expect(capsules()[0].className).toContain('pending')
    expect(outcomes()[0].textContent.trim()).toBe('⏳')
    expect(outcomes()[0].className).toContain('pending')
  })

  it('a failed attempt shows the failure, not a success', () => {
    setLang('zh')
    render(CompressionMarker, {
      entries: [{ ...finishedEntry, success: false, error: 'HTTP 400: prompt too large' }],
      compact: true,
    })
    expect(capsules()[0].textContent).toContain('上下文压缩失败')
    expect(outcomes()[0].textContent.trim()).toBe('\u2716\uFE0F')
    expect(outcomes()[0].className).toContain('failed')
  })

  it('clicking the tick opens the full list of what that compression did', () => {
    render(CompressionMarker, { entries: [finishedEntry], compact: true })
    expect(document.querySelector('.compression-detail')).toBeNull()
    outcomes()[0].click()
    flushSync()
    const detail = document.querySelector('.compression-detail')
    expect(detail).not.toBeNull()
    // The concise capsule hides these; the list behind ✔️ shows every one, with
    // the same keys conversation.json was written with.
    for (const key of [
      'trigger=in_loop', 'model=summary', 'tokens=92000', 'summary_version=19',
      'summary_chars=1194', 'memory_entries=73', 'duration_ms=76700', 'status=ok',
    ]) {
      expect(detail.textContent).toContain(key)
    }
    capsules()[0].click()
    flushSync()
    expect(document.querySelector('.compression-detail')).toBeNull()
  })

  it('renders nothing when the message has no compression', () => {
    render(CompressionMarker, { entries: [], compact: true })
    expect(capsules()).toHaveLength(0)
  })
})

describe('CompressionMarker (detailed mode)', () => {
  it('renders the same attempt as a block with its detail', () => {
    setLang('zh')
    render(CompressionMarker, { entries: [finishedEntry], compact: false })
    const block = document.querySelector('.compression-block')
    expect(block).not.toBeNull()
    expect(block.textContent).toContain('\u{1F5DC}\uFE0F')
    expect(block.textContent).toContain('上下文已压缩')
    expect(block.textContent).toContain('v19:1194字符,73条目')
    expect(block.querySelector('.compression-result-icon').textContent.trim()).toBe('\u2714\uFE0F')
    block.querySelector('.compression-head').click()
    flushSync()
    expect(block.querySelector('pre').textContent).toContain('summary_version=19')
  })
})

describe('MessageBubble restores the marker from the persisted conversation', () => {
  it('shows the marker on the message that carries meta.compressions', () => {
    setLang('zh')
    // Exactly what GET /v1/sessions/{id} returns for a session that was
    // compressed: no stream, no extra frame -- the audit entry is on the turn.
    const msg = {
      role: 'assistant',
      content: 'done',
      timestamp: '2026-01-01T00:00:00',
      meta: { compressions: [finishedEntry] },
    }
    render(MessageBubble, { msg, compact: true })
    expect(capsules()).toHaveLength(1)
    expect(capsules()[0].textContent).toContain('上下文已压缩')
  })

  it('shows it in detailed mode too (the message-details reply view)', () => {
    setLang('zh')
    const msg = {
      role: 'assistant',
      content: 'done',
      timestamp: '2026-01-01T00:00:00',
      meta: { compressions: [finishedEntry] },
    }
    render(MessageBubble, { msg, compact: false })
    const block = document.querySelector('.compression-block')
    expect(block).not.toBeNull()
    expect(block.textContent).toContain('\u{1F5DC}\uFE0F')
    expect(block.textContent).toContain('上下文已压缩')
  })

  it('leaves a message without a compression alone', () => {
    render(MessageBubble, {
      msg: { role: 'assistant', content: 'plain', timestamp: '2026-01-01T00:00:00' },
      compact: true,
    })
    expect(capsules()).toHaveLength(0)
  })
})
