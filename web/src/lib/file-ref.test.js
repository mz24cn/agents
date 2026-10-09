/**
 * Tests for the workspace file-reference parser (lib/file-ref.js).
 *
 * Fixtures are the exact strings the backend writes into conversation.json
 * (runtime/workspace_manager.py `expand_workspace_file_refs_in_message` and
 * Runtime._apply_vlm_image_fallback), because the bug this guards was a
 * representation mismatch: the frontend only understood the `<file>` tag form,
 * so a restored session showed `[Image file attached: …]` as literal text and
 * fed the image *path* into `data:image/png;base64,`.
 */
import { describe, it, expect } from 'vitest'
import {
  splitLeadingFileAttachments,
  parseFileRefParts,
  restoreFileRefTags,
  resolveImageSource,
  isRawBase64,
  imageMimeFor,
} from './file-ref.js'

describe('parseFileRefParts — editor form', () => {
  it('splits <file> tags out of the surrounding text', () => {
    expect(parseFileRefParts('<file>bobo.jpg</file>描述图片内容')).toEqual([
      { type: 'file', ref: 'bobo.jpg', kind: 'tag', content: null },
      { type: 'text', value: '描述图片内容' },
    ])
  })

  it('returns one text part when there is no reference', () => {
    expect(parseFileRefParts('普通消息')).toEqual([
      { type: 'text', value: '普通消息' },
    ])
  })
})

describe('parseFileRefParts — persisted (expanded) form', () => {
  it('turns an image placeholder into a chip instead of literal text', () => {
    expect(parseFileRefParts('[Image file attached: /mnt/e/bobo.jpg]用几句古诗描述图片内容')).toEqual([
      { type: 'file', ref: '/mnt/e/bobo.jpg', kind: 'image', content: null },
      { type: 'text', value: '用几句古诗描述图片内容' },
    ])
  })

  it('keeps bare path references (neither image nor text) as chips', () => {
    expect(parseFileRefParts('看看 [file attached: /tmp/doc.pdf] 的结论')).toEqual([
      { type: 'text', value: '看看 ' },
      { type: 'file', ref: '/tmp/doc.pdf', kind: 'other', content: null },
      { type: 'text', value: ' 的结论' },
    ])
  })

  it('folds the prepended text block into the chip at the user position', () => {
    const content = '[Text file attached: /w/a.py]\n```\nprint(1)\n```\n\n看 [Text file attached: /w/a.py] 有什么问题'
    expect(parseFileRefParts(content)).toEqual([
      { type: 'text', value: '看 ' },
      { type: 'file', ref: '/w/a.py', kind: 'text', content: 'print(1)' },
      { type: 'text', value: ' 有什么问题' },
    ])
  })

  it('pairs each of several prepended blocks with its own chip', () => {
    const content = '[Text file attached: /w/a.py]\n```\nA\n```\n\n'
      + '[Text file attached: /w/b.py]\n```\nB\n```\n\n'
      + '比较 [Text file attached: /w/b.py] 和 [Text file attached: /w/a.py]'
    const parts = parseFileRefParts(content)
    expect(parts.filter(p => p.type === 'file')).toEqual([
      { type: 'file', ref: '/w/b.py', kind: 'text', content: 'B' },
      { type: 'file', ref: '/w/a.py', kind: 'text', content: 'A' },
    ])
    expect(parts[0]).toEqual({ type: 'text', value: '比较 ' })
  })

  it('keeps an image transcription block that has no inline counterpart', () => {
    // Non-VLM fallback labels base64 uploads "(inline image)", so there is no
    // path to match — the block must still be rendered (collapsed).
    const parts = parseFileRefParts('[Image file attached: (inline image)]\n```\n一只猫\n```\n\n描述这张图')
    expect(parts).toEqual([
      { type: 'file', ref: '(inline image)', kind: 'image', content: '一只猫' },
      { type: 'text', value: '描述这张图' },
    ])
  })

  it('leaves a fenced block alone when it does not follow a placeholder', () => {
    const parts = parseFileRefParts('我的代码：\n```\nlet x = 1\n```')
    expect(parts).toEqual([{ type: 'text', value: '我的代码：\n```\nlet x = 1\n```' }])
  })
})

describe('splitLeadingFileAttachments', () => {
  it('consumes only the prepended blocks', () => {
    const { attachments, rest } = splitLeadingFileAttachments(
      '[Text file attached: /w/a.py]\n```\nA\n```\n\n正文 [Text file attached: /w/a.py]')
    expect(attachments).toHaveLength(1)
    expect(attachments[0]).toMatchObject({ kind: 'text', ref: '/w/a.py', content: 'A' })
    expect(rest).toBe('正文 [Text file attached: /w/a.py]')
  })

  it('returns the input untouched when there is no block', () => {
    expect(splitLeadingFileAttachments('正文')).toEqual({ attachments: [], rest: '正文' })
  })
})

describe('restoreFileRefTags — back to the editor form', () => {
  it('restores an image placeholder to a <file> tag', () => {
    expect(restoreFileRefTags('[Image file attached: /mnt/e/bobo.jpg]用几句古诗描述图片内容'))
      .toBe('<file>/mnt/e/bobo.jpg</file>用几句古诗描述图片内容')
  })

  it('drops the inlined file content and keeps one reference', () => {
    const content = '[Text file attached: /w/a.py]\n```\nprint(1)\n```\n\n看 [Text file attached: /w/a.py] 有什么问题'
    expect(restoreFileRefTags(content)).toBe('看 <file>/w/a.py</file> 有什么问题')
  })

  it('is idempotent on the editor form', () => {
    expect(restoreFileRefTags('<file>a.py</file> hi')).toBe('<file>a.py</file> hi')
  })

  it('drops a transcription block it cannot turn into a path', () => {
    // Nothing to reference: the upload was inline base64, and the transcription
    // is regenerated on the next send.
    expect(restoreFileRefTags('[Image file attached: (inline image)]\n```\n一只猫\n```\n\n描述这张图'))
      .toBe('描述这张图')
  })

  it('round-trips: the restored text parses back to the same references', () => {
    const persisted = '[Text file attached: /w/a.py]\n```\nA\n```\n\n比较 [Text file attached: /w/a.py] 与 [Image file attached: /w/i.png]'
    const restored = restoreFileRefTags(persisted)
    expect(restored).toBe('比较 <file>/w/a.py</file> 与 <file>/w/i.png</file>')
    expect(parseFileRefParts(restored).filter(p => p.type === 'file').map(p => p.ref))
      .toEqual(['/w/a.py', '/w/i.png'])
  })
})

describe('resolveImageSource', () => {
  const calls = []
  const api = {
    content: (path, restrict) => {
      calls.push({ path, restrict })
      return `/v1/workspace/content?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`
    },
  }

  it('passes data URIs and http(s) URLs through', () => {
    expect(resolveImageSource('data:image/png;base64,AAA', api)).toBe('data:image/png;base64,AAA')
    expect(resolveImageSource('https://x.test/a.png', api)).toBe('https://x.test/a.png')
  })

  it('serves an absolute workspace path through the content endpoint, unrestricted', () => {
    expect(resolveImageSource('/mnt/e/bobo.jpg', api))
      .toBe('/v1/workspace/content?path=%2Fmnt%2Fe%2Fbobo.jpg&restrict=0')
    expect(calls.at(-1)).toEqual({ path: '/mnt/e/bobo.jpg', restrict: false })
  })

  it('treats a bare filename as a path, not as base64', () => {
    expect(resolveImageSource('bobo.jpg', api))
      .toBe('/v1/workspace/content?path=bobo.jpg&restrict=0')
  })

  it('wraps a long raw base64 payload in a data URI', () => {
    const b64 = 'A'.repeat(200)
    expect(resolveImageSource(b64, api)).toBe(`data:image/png;base64,${b64}`)
  })

  it('returns null for empty entries', () => {
    expect(resolveImageSource('', api)).toBeNull()
    expect(resolveImageSource(null, api)).toBeNull()
  })

  it('classifies base64 the same way the backend does', () => {
    expect(isRawBase64('A'.repeat(200))).toBe(true)
    expect(isRawBase64('/mnt/e/bobo.jpg')).toBe(false)
    expect(isRawBase64('C:\\pics\\a.png')).toBe(false)
    expect(isRawBase64('short')).toBe(false)
  })

  it('guesses the MIME type from the extension', () => {
    expect(imageMimeFor('/x/y.jpeg')).toBe('image/jpeg')
    expect(imageMimeFor('/x/y.SVG')).toBe('image/svg+xml')
    expect(imageMimeFor('/x/y')).toBe('image/png')
  })
})
