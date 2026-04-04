import { describe, it, expect } from 'vitest'
import { extractPlaceholders, replacePlaceholders } from './placeholder.js'

describe('extractPlaceholders', () => {
  it('extracts variable names from template content', () => {
    expect(extractPlaceholders('Hello {name}, welcome to {city}'))
      .toEqual(['name', 'city'])
  })

  it('returns empty array when no placeholders', () => {
    expect(extractPlaceholders('No placeholders here')).toEqual([])
  })

  it('deduplicates repeated placeholders', () => {
    expect(extractPlaceholders('{x} and {x} and {y}'))
      .toEqual(['x', 'y'])
  })

  it('handles empty string', () => {
    expect(extractPlaceholders('')).toEqual([])
  })
})

describe('replacePlaceholders', () => {
  it('replaces placeholders with provided values', () => {
    expect(replacePlaceholders('Hello {name}!', { name: 'Alice' }))
      .toBe('Hello Alice!')
  })

  it('replaces missing values with empty string', () => {
    expect(replacePlaceholders('{a} and {b}', { a: 'yes' }))
      .toBe('yes and ')
  })

  it('returns original text when no placeholders', () => {
    expect(replacePlaceholders('plain text', {})).toBe('plain text')
  })

  it('handles multiple occurrences of same placeholder', () => {
    expect(replacePlaceholders('{x}+{x}', { x: '1' })).toBe('1+1')
  })
})
