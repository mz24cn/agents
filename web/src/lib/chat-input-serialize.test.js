/**
 * Tests for ChatInput's contenteditable serializer.
 *
 * Focus: Shift+Enter empty lines. Chromium represents an empty line as
 * `<div><br></div>`; it must serialize to ONE '\n'. Before the fix it produced
 * two (the `<br>` plus the wrapping `<div>`), so a single Shift+Enter showed up
 * as an extra blank line in the submitted user message.
 *
 * Also guards that file-ref chips (`<span data-file-ref>`) still round-trip as
 * `<file>...</file>` — those insertions are what create block structures in the
 * editor in the first place.
 *
 * The serializer only reads nodeType / tagName / childNodes / dataset, so the
 * DOM is stubbed with plain objects and no jsdom is needed (consistent with the
 * other lib tests, which run under the default `node` environment).
 */
import { describe, it, expect } from 'vitest'
import { serializeEditor, serializeNode } from './chat-input-serialize.js'

const TEXT_NODE = 3
const ELEMENT_NODE = 1

function text(value) { return { nodeType: TEXT_NODE, nodeValue: value } }
function el(tagName, children = [], extra = {}) {
  const node = { nodeType: ELEMENT_NODE, tagName, childNodes: children, dataset: {}, ...extra }
  node.firstChild = children[0] ?? null
  return node
}
function div(...children) { return el('DIV', children) }
function br() { return el('BR', []) }
function chip(ref) { return el('SPAN', [], { dataset: { fileRef: ref } }) }
function editor(...children) { return el('DIV', children) }

describe('serializeEditor — line breaks from Shift+Enter', () => {
  it('keeps a newline between two non-empty lines', () => {
    expect(serializeEditor(editor(text('a'), div(text('b'))))).toBe('a\nb')
  })

  it('keeps newlines between several non-empty lines', () => {
    expect(serializeEditor(editor(text('a'), div(text('b')), div(text('c')))))
      .toBe('a\nb\nc')
  })

  it('counts one empty line as a single newline (the regression)', () => {
    // a / Shift+Enter / Shift+Enter / b  ->  "a", one empty line, "b"
    const el = editor(text('a'), div(br()), div(text('b')))
    expect(serializeEditor(el)).toBe('a\n\nb')
  })

  it('counts two empty lines as two newlines', () => {
    const el = editor(text('a'), div(br()), div(br()), div(text('b')))
    expect(serializeEditor(el)).toBe('a\n\n\nb')
  })

  it('collapses a trailing empty line to a single newline (the component trims it on send)', () => {
    // "a" + one trailing empty line is two lines: "a\n". The pre-fix code
    // produced "a\n\n" (an extra blank line); handleSend() then .trim()s it.
    expect(serializeEditor(editor(text('a'), div(br())))).toBe('a\n')
  })

  it('handles an empty line inserted at the start of an existing line', () => {
    const el = editor(text('a'), div(br()), div(text('ztext')))
    expect(serializeEditor(el)).toBe('a\n\nztext')
  })

  it('does not add a spurious newline for a literal-newline text node before a <div>', () => {
    // The editor is also populated from plain text (drafts / file attach),
    // which yields literal '\n' inside a text node.
    expect(serializeEditor(editor(text('a\n'), div(text('b'))))).toBe('a\nb')
  })

  it('does not double-count a literal newline before an empty-line placeholder', () => {
    const el = editor(text('a\n'), div(br()), div(text('b')))
    expect(serializeEditor(el)).toBe('a\n\nb')
  })

  it('normalizes CRLF / CR to LF and strips one trailing newline', () => {
    expect(serializeEditor(editor(text('a\r\nb\rc')))).toBe('a\nb\nc')
    expect(serializeEditor(editor(text('a\n')))).toBe('a')
  })

  it('returns the fallback when the editor is not mounted', () => {
    expect(serializeEditor(null, 'fallback')).toBe('fallback')
    expect(serializeEditor(null)).toBe('')
  })
})

describe('serializeEditor — file references', () => {
  it('serializes a lone chip', () => {
    expect(serializeEditor(editor(chip('a.png')))).toBe('<file>a.png</file>')
  })

  it('serializes text followed by a chip', () => {
    expect(serializeEditor(editor(text('hello '), chip('a.png'))))
      .toBe('hello <file>a.png</file>')
  })

  it('serializes several chips', () => {
    expect(serializeEditor(editor(chip('a.png'), text(' '), chip('b.pdf'))))
      .toBe('<file>a.png</file> <file>b.pdf</file>')
  })

  it('keeps a newline between a chip and a following line', () => {
    expect(serializeEditor(editor(chip('a.png'), div(text('b')))))
      .toBe('<file>a.png</file>\nb')
  })

  it('keeps a chip and an empty line below it', () => {
    const el = editor(chip('a.png'), div(br()), div(text('b')))
    expect(serializeEditor(el)).toBe('<file>a.png</file>\n\nb')
  })

  it('collapses a trailing empty line after a chip to a single newline', () => {
    expect(serializeEditor(editor(chip('a.png'), div(br()))))
      .toBe('<file>a.png</file>\n')
  })
})

describe('serializeNode', () => {
  it('returns text node values verbatim', () => {
    expect(serializeNode(text('x\ny'))).toBe('x\ny')
  })

  it('turns <br> into a newline', () => {
    expect(serializeNode(br())).toBe('\n')
  })

  it('turns a file chip into a <file> marker', () => {
    expect(serializeNode(chip('/tmp/a.png'))).toBe('<file>/tmp/a.png</file>')
  })
})
