// @vitest-environment jsdom
/**
 * Component tests for the chat voice-input button (VoiceInput.svelte).
 *
 * What is pinned down here is the *wiring*, which the pure asr-client tests
 * cannot cover:
 *   - the socket is opened against the same-origin bridge (/v1/asr/ws), never
 *     directly at the ASR service port;
 *   - a `final` event inserts text into the message box, a `partial` only
 *     updates the live strip;
 *   - the button state machine (start -> recording -> flush) and the
 *     instance-level stop() that ChatInput calls when a message is sent;
 *   - when the browser cannot capture audio, no socket/mic call is made at all
 *     and the user gets the explanatory message instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import VoiceInput from './VoiceInput.svelte'
import { setLang } from '../../lib/i18n.svelte.js'

let mounted = []
const sockets = []

class FakeWebSocket {
  constructor(url) {
    this.url = url
    this.readyState = 0 // CONNECTING
    this.sent = []
    this.binaryType = ''
    sockets.push(this)
    setTimeout(() => {
      this.readyState = 1
      this.onopen?.()
    }, 0)
  }
  send(data) { this.sent.push(data) }
  close() {
    if (this.readyState === 3) return
    this.readyState = 3
    this.onclose?.({ code: 1000 })
  }
  emit(event) { this.onmessage?.({ data: JSON.stringify(event) }) }
  textFrames() { return this.sent.filter((f) => typeof f === 'string') }
}

function makeAudioMocks() {
  const stream = { getTracks: () => [{ stop: vi.fn() }] }
  const node = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null }
  const ctx = {
    state: 'running',
    sampleRate: 48000,
    destination: {},
    resume: vi.fn(async () => {}),
    createMediaStreamSource: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
    createScriptProcessor: vi.fn(() => node),
    close: vi.fn(),
  }
  class FakeAudioContext { constructor() { return ctx } }
  return { stream, node, ctx, FakeAudioContext }
}

function render(props = {}) {
  const target = document.createElement('div')
  document.body.appendChild(target)
  const app = mount(VoiceInput, { target, props })
  flushSync()
  mounted.push({ app, target })
  return { app, target }
}

async function flushPromises() {
  await new Promise((resolve) => setTimeout(resolve, 0))
  flushSync()
}

function micButton(target) { return target.querySelector('button.voice-btn') }

beforeEach(() => {
  setLang('en')
  sockets.length = 0
  vi.stubGlobal('WebSocket', FakeWebSocket)
  const { stream, FakeAudioContext } = makeAudioMocks()
  vi.stubGlobal('AudioContext', FakeAudioContext)
  vi.stubGlobal('webkitAudioContext', FakeAudioContext)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(async () => stream) },
    configurable: true,
  })
  vi.stubGlobal('fetch', vi.fn(async () => ({
    json: async () => ({ available: true, configured: true, engine: 'paraformer-streaming' }),
  })))
})

afterEach(() => {
  for (const { app, target } of mounted) {
    unmount(app)
    target.remove()
  }
  mounted = []
  vi.unstubAllGlobals()
})

describe('VoiceInput button', () => {
  it('renders a mic button in the input toolbar', async () => {
    const { target } = render()
    const btn = micButton(target)
    expect(btn).toBeTruthy()
    expect(btn.textContent).toContain('\u{1F3A4}')
    expect(btn.className).not.toContain('off')
  })

  it('hides the button entirely when voice input is not configured', async () => {
    // AGENTS_ASR_URL unset: there is nothing for the user to fix, so no button.
    fetch.mockResolvedValue({ json: async () => ({ available: false, configured: false, error: 'not_configured' }) })
    const { target } = render()
    await flushPromises()
    expect(micButton(target)).toBe(null)
  })

  it('dims the button and explains when the ASR service is not reachable', async () => {
    fetch.mockResolvedValue({ json: async () => ({ available: false, configured: true, error: 'asr_unreachable' }) })
    const { target } = render()
    await flushPromises()
    const btn = micButton(target)
    expect(btn.className).toContain('off')
    expect(btn.getAttribute('title')).toContain('accessories/ASR/ASR_server.py')
  })

  it('opens the same-origin bridge socket and sends the config frame', async () => {
    const { target } = render()
    await flushPromises()
    micButton(target).click()
    await flushPromises()

    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
    expect(sockets).toHaveLength(1)
    // Same origin (the runtime bridges it), not ws://host:7996.
    expect(sockets[0].url).toBe('ws://localhost:3000/v1/asr/ws?sample_rate=16000')
    const config = JSON.parse(sockets[0].textFrames()[0])
    expect(config).toMatchObject({ type: 'config', sample_rate: 16000 })
    expect(micButton(target).className).toContain('recording')
  })

  it('reports partials as live text and finalized text as committed', async () => {
    const onCommit = vi.fn()
    const onPartial = vi.fn()
    const { target } = render({ onCommit, onPartial })
    await flushPromises()
    micButton(target).click()
    await flushPromises()
    const ws = sockets[0]

    ws.emit({ type: 'partial', seg_id: 1, text: '你好' })
    expect(onCommit).not.toHaveBeenCalled()
    expect(onPartial).toHaveBeenLastCalledWith('你好')

    ws.emit({ type: 'final', seg_id: 1, text: '你好。' })
    expect(onCommit).toHaveBeenCalledWith('你好。')
    expect(onPartial).toHaveBeenLastCalledWith('')
  })

  it('drops the live text when the service cancels a noisy segment', async () => {
    const onCommit = vi.fn()
    const onPartial = vi.fn()
    const { target } = render({ onCommit, onPartial })
    await flushPromises()
    micButton(target).click()
    await flushPromises()
    const ws = sockets[0]
    ws.emit({ type: 'partial', seg_id: 1, text: '咳' })
    ws.emit({ type: 'segment_cancel', seg_id: 1 })
    expect(onCommit).not.toHaveBeenCalled()
    expect(onPartial).toHaveBeenLastCalledWith('')
  })

  it('second click flushes the last segment instead of cutting it off', async () => {
    const { target } = render()
    await flushPromises()
    micButton(target).click()
    await flushPromises()
    micButton(target).click()
    flushSync()
    const frames = sockets[0].textFrames().map((f) => JSON.parse(f).type)
    expect(frames).toContain('flush')
    expect(micButton(target).className).toContain('busy')  // finalizing
  })

  it('stop() (used by ChatInput when sending) ends the session', async () => {
    const { app, target } = render()
    await flushPromises()
    micButton(target).click()
    await flushPromises()
    expect(app.recording()).toBe(true)
    app.stop()
    expect(sockets[0].textFrames().map((f) => JSON.parse(f).type)).toContain('flush')
  })

  it('explains a clean recording that produced no text', async () => {
    // The service answers a flush with flush_ack even when it never opened a
    // speech segment, so without this the failure is silent: the button worked,
    // the socket worked, the box stayed empty.
    const errors = []
    const { target } = render({ onError: (m) => errors.push(m) })
    await flushPromises()
    micButton(target).click()
    await flushPromises()
    sockets[0].emit({ type: 'flush_ack' })
    await flushPromises()
    // This harness never runs onaudioprocess, so no audio was counted.
    expect(errors.join(' ')).toContain('captured no audio')
  })

  it('resumes a suspended AudioContext, and says so when it will not run', async () => {
    // Chrome/Edge suspend the audio graph (autoplay policy, backgrounded tab).
    // A suspended context never fires onaudioprocess: the session looks live
    // while not one sample is captured -- the classic "no text on this machine".
    const errors = []
    const suspended = {
      state: 'suspended',
      sampleRate: 48000,
      destination: {},
      resume: vi.fn(async () => {}),  // stays suspended: the policy blocks it
      createMediaStreamSource: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
      createScriptProcessor: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
      close: vi.fn(),
    }
    vi.stubGlobal('AudioContext', class { constructor() { return suspended } })
    vi.stubGlobal('webkitAudioContext', class { constructor() { return suspended } })

    const { target } = render({ onError: (m) => errors.push(m) })
    await flushPromises()
    micButton(target).click()
    await flushPromises()

    expect(suspended.resume).toHaveBeenCalledOnce()
    expect(errors.join(' ')).toContain('suspended audio capture')
    expect(micButton(target).className).not.toContain('recording')
  })

  it('reports a blocked microphone instead of calling getUserMedia', async () => {
    // jsdom serves the page from http://localhost, so the secure-context rule
    // itself is covered in asr-client.test.js; here the browser simply has no
    // mediaDevices entry point (same code path: no mic call, message instead).
    Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true })
    const onError = vi.fn()
    const { target } = render({ onError })
    await flushPromises()
    expect(micButton(target).className).toContain('off')
    micButton(target).click()
    await flushPromises()
    expect(sockets).toHaveLength(0)
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('microphone'))
  })

  it('surfaces an asr_unreachable error frame from the bridge', async () => {
    const onError = vi.fn()
    const { target } = render({ onError })
    await flushPromises()
    micButton(target).click()
    await flushPromises()
    sockets[0].emit({ type: 'error', code: 'asr_unreachable', message: 'ASR 服务不可达' })
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('ASR service is not running'))
    await flushPromises()
    expect(micButton(target).className).toContain('off')
  })
})
