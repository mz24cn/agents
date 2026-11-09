// @vitest-environment jsdom
/**
 * Tests for the stop-menu idempotency lock while a generation is streaming.
 *
 * `actionPending` disables the ⏹ control (and with it 暂停 / 停止 / 强制停止) from
 * the moment an option is picked until the session state actually flips — that
 * is right for actions which *cause* a flip, and fatal for one which does not.
 * Inserting a message mid-generation (插话 / 附加新的消息) continues the same
 * generation: `isStreaming` stays true, `isPaused` / `pauseRequested` stay false,
 * so the state-driven unlock never runs. A lock that is set there and not
 * released by the request kills every stop control for the rest of that
 * generation — the user can neither stop the run they just interrupted nor add a
 * second message — and the menu path and a synchronous failure reach exactly
 * that state.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import ChatInput from './ChatInput.svelte'
import { setLang } from '../../lib/i18n.svelte.js'

let host = null
let instance = null

function open(props) {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(ChatInput, { target: host, props })
  flushSync()
}

function tearDown() {
  instance && unmount(instance)
  instance = null
  host?.remove()
  host = null
}

const editor = () => document.querySelector('.input-box')
const stopBtn = () => document.querySelector('.send-btn.stop')
const menuItems = () => Array.from(document.querySelectorAll('.stop-menu-item'))
const itemWith = (glyph) => menuItems().find(b => b.textContent.includes(glyph))

function pressEnter() {
  editor().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
  flushSync()
}

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

// The lock is released in a .then(), i.e. a microtask; flushSync afterwards is
// what makes the re-enabled button observable to the next assertion.
async function settle() {
  await Promise.resolve()
  await Promise.resolve()
  flushSync()
}

describe('ChatInput stop-menu lock', () => {
  beforeEach(() => { setLang('zh') })
  afterEach(() => { tearDown() })

  it('keeps the stop controls alive after an interjection sent with Enter', async () => {
    const d = deferred()
    const seen = []
    open({
      isStreaming: true,
      text: '插一条',
      onPause: () => {}, onStop: () => {}, onStopForce: () => {},
      onInterject: (val) => { seen.push(val); return d.promise },
    })

    pressEnter()
    expect(seen).toEqual(['插一条'])

    // In flight: a second Enter must not enqueue the same draft twice.
    pressEnter()
    expect(seen).toHaveLength(1)

    d.resolve({ status: 'queued' })
    await settle()

    // The generation is still streaming, yet its controls must be usable again:
    // nothing will flip isStreaming/isPaused/pauseRequested to unlock for us.
    expect(stopBtn()).toBeTruthy()
    expect(stopBtn().disabled).toBe(false)

    pressEnter()
    expect(seen).toHaveLength(2)
  })

  it('keeps the stop controls alive after the 附加新的消息 menu item is clicked', async () => {
    const d = deferred()
    const stopped = []
    open({
      isStreaming: true,
      text: '插一条',
      canPause: true,
      onPause: () => {}, onStop: (v) => stopped.push(v), onStopForce: () => {},
      onInterject: () => d.promise,
    })

    stopBtn().click()          // open the menu
    flushSync()
    const interject = itemWith('💬')
    expect(interject).toBeTruthy()
    interject.click()
    flushSync()

    d.resolve({ status: 'queued' })
    await settle()

    expect(stopBtn().disabled).toBe(false)
    stopBtn().click()
    flushSync()
    itemWith('⏹️').click()
    flushSync()
    expect(stopped).toHaveLength(1)
  })

  it('does not stick the lock when the interjection fails synchronously', async () => {
    const errors = []
    const seen = []
    open({
      isStreaming: true,
      text: '插一条',
      onPause: () => {}, onStop: () => {}, onStopForce: () => {},
      onError: (m) => errors.push(m),
      onInterject: (val) => {
        seen.push(val)
        if (seen.length === 1) throw new Error('boom')
      },
    })

    pressEnter()
    expect(errors).toHaveLength(1)
    expect(stopBtn().disabled).toBe(false)

    pressEnter()
    expect(seen).toHaveLength(2)
  })
})
