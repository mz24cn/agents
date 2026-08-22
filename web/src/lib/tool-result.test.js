import { describe, expect, it } from 'vitest'
import { isToolErrorContent } from './tool-result.js'

describe('isToolErrorContent', () => {
  it('detects a top-level JSON error field', () => {
    expect(isToolErrorContent('{"error":"failed"}')).toBe(true)
  })

  it('detects Error: within the first 50 characters', () => {
    expect(isToolErrorContent(`${'a'.repeat(43)}Error: failed`)).toBe(true)
  })

  it('does not scan past the first 50 characters unless the marker is near the end', () => {
    expect(isToolErrorContent(`${'a'.repeat(50)}Error:${'b'.repeat(101)}`)).toBe(false)
  })

  it('detects Error: within the last 100 characters for wrapped talk_to results', () => {
    expect(isToolErrorContent(`${'a'.repeat(101)}Error: sub-agent failed${'b'.repeat(70)}`)).toBe(true)
  })

  it('is case-insensitive', () => {
    expect(isToolErrorContent('prefix error: failed')).toBe(true)
  })

  it('does not flag ordinary successful text', () => {
    expect(isToolErrorContent('Task completed successfully')).toBe(false)
  })
})
