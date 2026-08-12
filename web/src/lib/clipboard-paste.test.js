/**
 * Tests for clipboard-paste helpers used by the ChatInput box.
 *
 * Focus: pasted image / PDF / DOCX files are extracted from the clipboard and
 * converted into <file> references (equivalent to the workspace file manager's
 * paste-upload + select-file operations).
 */
import { describe, it, expect, vi } from 'vitest'
import {
  extractPastedFiles,
  buildFileRefs,
  handleClipboardPaste,
} from './clipboard-paste.js'

function makeItem(kind, type = '', file = null) {
  return {
    kind,
    type,
    getAsFile: () => file,
  }
}

function makeFile(name, type, content = 'x') {
  return new File([content], name, { type })
}

describe('extractPastedFiles', () => {
  it('extracts an image file pasted from clipboard items', () => {
    const img = makeFile('screenshot.png', 'image/png')
    const dt = { items: [makeItem('file', 'image/png', img)] }
    const files = extractPastedFiles(dt)
    expect(files).toHaveLength(1)
    expect(files[0].name).toBe('screenshot.png')
    expect(files[0].type).toBe('image/png')
  })

  it('extracts a PDF file', () => {
    const pdf = makeFile('report.pdf', 'application/pdf')
    const dt = { items: [makeItem('file', 'application/pdf', pdf)] }
    expect(extractPastedFiles(dt)).toEqual([pdf])
  })

  it('extracts a DOCX file', () => {
    const docx = makeFile('doc.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    const dt = { items: [makeItem('file', docx.type, docx)] }
    expect(extractPastedFiles(dt)).toEqual([docx])
  })

  it('extracts multiple pasted files', () => {
    const a = makeFile('a.png', 'image/png')
    const b = makeFile('b.pdf', 'application/pdf')
    const c = makeFile('c.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    const dt = { items: [makeItem('file', a.type, a), makeItem('file', b.type, b), makeItem('file', c.type, c)] }
    const files = extractPastedFiles(dt)
    expect(files.map((f) => f.name)).toEqual(['a.png', 'b.pdf', 'c.docx'])
  })

  it('ignores non-file clipboard items (text) and returns no files', () => {
    const dt = { items: [makeItem('string', 'text/plain')] }
    expect(extractPastedFiles(dt)).toEqual([])
  })

  it('falls back to clipboardData.files when no file items are present', () => {
    const img = makeFile('pasted.jpg', 'image/jpeg')
    const dt = { items: [], files: [img] }
    expect(extractPastedFiles(dt)).toEqual([img])
  })

  it('returns [] for null/empty clipboard data', () => {
    expect(extractPastedFiles(null)).toEqual([])
    expect(extractPastedFiles({ items: [] })).toEqual([])
    expect(extractPastedFiles(undefined)).toEqual([])
  })
})

describe('buildFileRefs', () => {
  it('builds a <file> tag for a single absolute path', () => {
    expect(buildFileRefs(['/tmp/screenshot.png'])).toBe('<file>/tmp/screenshot.png</file>')
  })

  it('builds space-separated <file> tags for multiple paths', () => {
    expect(buildFileRefs(['/tmp/a.png', '/tmp/b.pdf', '/tmp/c.docx']))
      .toBe('<file>/tmp/a.png</file> <file>/tmp/b.pdf</file> <file>/tmp/c.docx</file>')
  })

  it('returns empty string for no paths', () => {
    expect(buildFileRefs([])).toBe('')
    expect(buildFileRefs()).toBe('')
  })

  it('handles Windows-style paste paths', () => {
    expect(buildFileRefs(['C:\\Temp\\paste\\a.pdf']))
      .toBe('<file>C:\\Temp\\paste\\a.pdf</file>')
  })
})

describe('handleClipboardPaste', () => {
  it('returns null when the clipboard only carries text', async () => {
    const uploadFiles = vi.fn()
    const dt = { items: [makeItem('string', 'text/plain')], getData: () => 'hello' }
    const result = await handleClipboardPaste(dt, { uploadFiles })
    expect(result).toBeNull()
    expect(uploadFiles).not.toHaveBeenCalled()
  })

  it('uploads pasted files and returns their paths', async () => {
    const img = makeFile('shot.png', 'image/png')
    const dt = { items: [makeItem('file', 'image/png', img)] }
    const uploadFiles = vi.fn().mockResolvedValue(['/tmp/shot.png'])
    const result = await handleClipboardPaste(dt, { uploadFiles })
    expect(uploadFiles).toHaveBeenCalledWith([img])
    expect(result).toEqual(['/tmp/shot.png'])
  })
})
