// @vitest-environment jsdom
/**
 * Component tests for the AskUser question dialog (AskUserDialog.svelte).
 *
 * The library tests cover what a choice means; these cover what the user can
 * actually do with it: see the question, pick an option by mouse or keyboard,
 * watch a countdown pick the default it was given, refuse to answer, or answer
 * in their own words when none of the options fit.  Those are the only
 * behaviours the model was promised, and a modal that quietly loses one of them
 * is a tool the model is calling for nothing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import AskUserDialog from './AskUserDialog.svelte'
import { setLang } from '../../lib/i18n.svelte.js'

let host = null
let instance = null

function open(props) {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(AskUserDialog, { target: host, props })
  flushSync()
}

function tearDown() {
  instance && unmount(instance)
  instance = null
  host?.remove()
  host = null
}

const optionButtons = () => Array.from(document.querySelectorAll('.ask-option'))
const timerText = () => document.querySelector('.ask-timer')?.textContent ?? ''
// flushSync: Svelte 5 updates the DOM in a microtask, which is invisible to a
// synchronous assertion; keyboard state that never reached the DOM would make
// these tests pass on things the user cannot actually see.
const press = (key, init = {}) => {
  window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }))
  flushSync()
}

describe('AskUserDialog', () => {
  beforeEach(() => {
    setLang('zh')
    vi.useFakeTimers()
  })
  afterEach(() => {
    tearDown()
    vi.useRealTimers()
  })

  it('shows the question and every option in order', () => {
    open({
      open: true,
      request: { question: '用哪种部署方式？', options: ['蓝绿发布', '金丝雀发布', '直接替换'], timeout: 0, defaultOption: '' },
      onChoose: () => {},
      onCancel: () => {},
    })
    expect(document.querySelector('.ask-question').textContent).toBe('用哪种部署方式？')
    expect(Array.from(document.querySelectorAll('.ask-text')).map((node) => node.textContent)).toEqual([
      '蓝绿发布',
      '金丝雀发布',
      '直接替换',
    ])
    expect(Array.from(document.querySelectorAll('.ask-key')).map((node) => node.textContent)).toEqual(['1', '2', '3'])
    // timeout 0 is the "we will wait" case, not an unset one.
    expect(timerText()).toBe('等待你的选择（不会超时）')
    expect(document.querySelector('.ask-default')).toBeNull()
  })

  it('returns the chosen option when it is clicked', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: '先发布吗？', options: ['发布', '再等等'], timeout: 0, defaultOption: '' },
      onChoose,
      onCancel: () => {},
    })
    optionButtons()[1].click()
    expect(onChoose).toHaveBeenCalledTimes(1)
    expect(onChoose.mock.calls[0][0]).toEqual({ value: '再等等' })
  })

  it('moves with the arrow keys and answers on Enter', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙', '丙'], timeout: 0, defaultOption: '' },
      onChoose,
      onCancel: () => {},
    })
    press('ArrowDown')
    press('Enter')
    expect(onChoose).toHaveBeenCalledWith({ value: '乙' })
  })

  it('wraps around and lets a number key answer directly', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙', '丙'], timeout: 0, defaultOption: '' },
      onChoose,
      onCancel: () => {},
    })
    press('ArrowUp')            // from the first option back to the last
    expect(optionButtons().findIndex((button) => button.classList.contains('selected'))).toBe(2)
    press('2')
    expect(onChoose).toHaveBeenCalledWith({ value: '乙' })
  })

  it('preselects the default and applies it by itself when the countdown ends', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['继续', '取消'], timeout: 3, defaultOption: '取消' },
      onChoose,
      onCancel: () => {},
    })
    expect(optionButtons().findIndex((button) => button.classList.contains('selected'))).toBe(1)
    expect(document.querySelector('.ask-default')).not.toBeNull()
    expect(timerText()).toContain('3')

    vi.advanceTimersByTime(1000)
    flushSync()
    expect(timerText()).toContain('2')
    expect(onChoose).not.toHaveBeenCalled()

    vi.advanceTimersByTime(2500)
    flushSync()
    expect(onChoose).toHaveBeenCalledTimes(1)
    expect(onChoose.mock.calls[0][0]).toMatchObject({ value: '取消', timedOut: true, auto: true })
  })

  it('does not invent a choice when the countdown ends without a default', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['继续', '取消'], timeout: 2, defaultOption: '' },
      onChoose,
      onCancel: () => {},
    })
    vi.advanceTimersByTime(2500)
    flushSync()
    expect(onChoose).toHaveBeenCalledTimes(1)
    expect(onChoose.mock.calls[0][0]).toMatchObject({ value: null, timedOut: true })
  })

  it('lets the user refuse to answer', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙'], timeout: 0, defaultOption: '' },
      onChoose,
      onCancel: () => {},
    })
    document.querySelector('.ask-foot-actions .btn-cancel').click()
    expect(onChoose).toHaveBeenCalledTimes(1)
    expect(onChoose.mock.calls[0][0]).toMatchObject({ value: null, dismissed: true })
  })

  it('answers in the user\'s own words when none of the options fit', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙'], timeout: 0, defaultOption: '' },
      onChoose,
      onCancel: () => {},
    })
    press('Tab')
    const box = document.querySelector('.ask-free')
    expect(box).not.toBeNull()
    box.value = '先让我把测试跑完'
    box.dispatchEvent(new Event('input', { bubbles: true }))
    flushSync()
    expect(document.querySelector('.ask-free-actions .btn-send')).not.toBeNull()
    document.querySelector('.ask-free-actions .btn-send').click()
    expect(onChoose).toHaveBeenCalledWith({ value: '先让我把测试跑完', freeText: true })
  })

  it('settles only once, however it was answered', () => {
    const onChoose = vi.fn()
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙'], timeout: 2, defaultOption: '乙' },
      onChoose,
      onCancel: () => {},
    })
    optionButtons()[0].click()
    vi.advanceTimersByTime(3000)
    flushSync()
    press('Enter')
    expect(onChoose).toHaveBeenCalledTimes(1)
    expect(onChoose).toHaveBeenCalledWith({ value: '甲' })
  })
  it('does not answer a question the user cannot see', () => {
    // 会话切走 / 换了页面时对话框不在屏幕上：倒计时不能替用户做决定，
    // 问题也不能被自动"回答"掉——它得等用户回来。
    const onChoose = vi.fn()
    open({
      open: false,
      request: { question: 'q', options: ['甲', '乙'], timeout: 2, defaultOption: '乙' },
      onChoose,
      onCancel: () => {},
    })
    expect(document.querySelector('.ask-option')).toBeNull()
    vi.advanceTimersByTime(5000)
    flushSync()
    expect(onChoose).not.toHaveBeenCalled()
  })

  it('says how many other conversations are waiting too', () => {
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙'], timeout: 0, defaultOption: '' },
      onChoose: () => {},
      onCancel: () => {},
      otherWaiting: 2,
    })
    expect(document.querySelector('.ask-queued').textContent).toContain('2')
    tearDown()
    open({
      open: true,
      request: { question: 'q', options: ['甲', '乙'], timeout: 0, defaultOption: '' },
      onChoose: () => {},
      onCancel: () => {},
      otherWaiting: 0,
    })
    expect(document.querySelector('.ask-queued')).toBeNull()
  })
})
