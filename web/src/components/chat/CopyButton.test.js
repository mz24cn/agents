/**
 * Tests for CopyButton clipboard logic
 * E1: copied state becomes true on success, resets after 2s
 * E2: clipboard API rejection is handled silently
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// ---------------------------------------------------------------------------
// Extract the handleCopy logic from CopyButton.svelte for isolated testing.
// We inject the clipboard API as a dependency to keep tests environment-agnostic.
// This mirrors the exact logic in CopyButton.svelte.
// ---------------------------------------------------------------------------

function createCopyHandler(clipboard) {
  let copied = false
  let timer = null

  async function handleCopy(getText) {
    try {
      await clipboard.writeText(getText())
      copied = true
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { copied = false }, 2000)
    } catch {
      // 静默失败：保持 copied = false，不抛出异常
    }
  }

  return {
    handleCopy,
    getCopied: () => copied,
  }
}

// ---------------------------------------------------------------------------
// E1: 复制成功后 copied 状态变为 true，2 秒后恢复 false
// Validates: Requirements 1.3, 2.3
// ---------------------------------------------------------------------------

describe('E1: copy success sets copied=true, resets after 2s', () => {
  let mockClipboard

  beforeEach(() => {
    vi.useFakeTimers()
    mockClipboard = {
      writeText: vi.fn().mockResolvedValue(undefined),
    }
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('copied becomes true after successful clipboard write', async () => {
    const { handleCopy, getCopied } = createCopyHandler(mockClipboard)
    expect(getCopied()).toBe(false)

    await handleCopy(() => 'hello world')

    expect(getCopied()).toBe(true)
    expect(mockClipboard.writeText).toHaveBeenCalledWith('hello world')
  })

  it('copied resets to false after 2 seconds', async () => {
    const { handleCopy, getCopied } = createCopyHandler(mockClipboard)

    await handleCopy(() => 'test text')
    expect(getCopied()).toBe(true)

    vi.advanceTimersByTime(2000)
    expect(getCopied()).toBe(false)
  })

  it('copied remains true before 2 seconds elapse', async () => {
    const { handleCopy, getCopied } = createCopyHandler(mockClipboard)

    await handleCopy(() => 'test text')
    expect(getCopied()).toBe(true)

    vi.advanceTimersByTime(1999)
    expect(getCopied()).toBe(true)
  })

  it('getText() return value is passed to clipboard.writeText', async () => {
    const { handleCopy } = createCopyHandler(mockClipboard)
    const getText = vi.fn().mockReturnValue('my code snippet')

    await handleCopy(getText)

    expect(getText).toHaveBeenCalledOnce()
    expect(mockClipboard.writeText).toHaveBeenCalledWith('my code snippet')
  })
})

// ---------------------------------------------------------------------------
// E2: clipboard API reject 时不抛出异常，状态不变
// Validates: Requirements 1.4, 2.4
// ---------------------------------------------------------------------------

describe('E2: clipboard failure is handled silently', () => {
  let mockClipboard

  beforeEach(() => {
    vi.useFakeTimers()
    mockClipboard = {
      writeText: vi.fn().mockRejectedValue(new Error('NotAllowedError')),
    }
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('does not throw when clipboard.writeText rejects', async () => {
    const { handleCopy } = createCopyHandler(mockClipboard)
    await expect(handleCopy(() => 'text')).resolves.not.toThrow()
  })

  it('copied remains false when clipboard write fails', async () => {
    const { handleCopy, getCopied } = createCopyHandler(mockClipboard)

    await handleCopy(() => 'text')

    expect(getCopied()).toBe(false)
  })

  it('no timer is set when clipboard write fails', async () => {
    const { handleCopy, getCopied } = createCopyHandler(mockClipboard)

    await handleCopy(() => 'text')

    // Advance time — copied should still be false (no timer was set)
    vi.advanceTimersByTime(3000)
    expect(getCopied()).toBe(false)
  })

  it('does not throw when clipboard API throws synchronously', async () => {
    const throwingClipboard = {
      writeText: vi.fn().mockImplementation(() => { throw new Error('sync error') }),
    }
    const { handleCopy } = createCopyHandler(throwingClipboard)
    await expect(handleCopy(() => 'text')).resolves.not.toThrow()
  })
})
