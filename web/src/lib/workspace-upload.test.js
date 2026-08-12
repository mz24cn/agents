/**
 * Tests for the shared workspace upload helpers used by the ChatInput paste
 * feature. These exercise the chunked upload pipeline (uploadInit ->
 * uploadChunk -> uploadComplete) against a mocked API client.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  joinPath,
  uploadFileToDir,
  uploadFilesToPasteDir,
  stampPastedFileNames,
  pasteTimestamp,
  resetPasteDirCache,
  resetPasteStamp,
} from './workspace-upload.js'

vi.mock('./api.js', () => ({
  workspace: {
    uploadInit: vi.fn(),
    uploadChunk: vi.fn(),
    uploadComplete: vi.fn(),
    uploadCancel: vi.fn(),
    list: vi.fn(),
    pasteDir: vi.fn(),
  },
}))

import { workspace } from './api.js'

function makeFile(name, type, size = 10) {
  const file = new File([new Uint8Array(size)], name, { type })
  return file
}

function mockUploadInit({ uploadId = 'upload-1', chunks = [{ parallel_id: 0, offset: 0, size: 10 }] } = {}) {
  workspace.uploadInit.mockResolvedValue({ upload_id: uploadId, chunks })
}

function mockUploadChunk() {
  workspace.uploadChunk.mockReturnValue({ promise: Promise.resolve({ status: 'uploaded' }) })
}

/** Matches a timestamped pasted-file name like `image_143025_123.png`. */
const TIMESTAMPED_NAME = /^(.+)_(\d{6})_(\d{3})(\.[^.]+)?$/

/** Fixed local-time instant (14:30:25.123) so assertions are timezone-independent. */
const FIXED_MS = new Date(2024, 0, 1, 14, 30, 25, 123).getTime()

describe('joinPath', () => {
  it('joins unix paths', () => {
    expect(joinPath('/tmp', 'a.png')).toBe('/tmp/a.png')
    expect(joinPath('/tmp/', 'a.png')).toBe('/tmp/a.png')
  })

  it('joins windows paths preserving backslashes', () => {
    expect(joinPath('C:\\Temp', 'a.pdf')).toBe('C:\\Temp\\a.pdf')
    expect(joinPath('C:\\Temp\\', 'a.pdf')).toBe('C:\\Temp\\a.pdf')
  })

  it('returns name when dir is empty', () => {
    expect(joinPath('', 'a.png')).toBe('a.png')
  })
})

describe('uploadFileToDir', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetPasteDirCache()
  })

  it('uploads all chunks then completes and returns the absolute path', async () => {
    mockUploadInit({ chunks: [
      { parallel_id: 0, offset: 0, size: 5 },
      { parallel_id: 1, offset: 5, size: 5 },
    ] })
    mockUploadChunk()
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const file = makeFile('report.pdf', 'application/pdf', 10)
    const path = await uploadFileToDir(file, '/tmp')

    expect(workspace.uploadInit).toHaveBeenCalledWith({
      workspace_id: 'default',
      file_name: 'report.pdf',
      file_size: 10,
      target_dir_path: '/tmp',
      target_path: 'report.pdf',
    })
    expect(workspace.uploadChunk).toHaveBeenCalledTimes(2)
    // Each chunk must carry file_size so api.js can send a valid X-File-Size
    // header (the backend rejects "undefined"; regression guard for the
    // CHUNK_SIZE_MISMATCH: invalid X-File-Size paste failure).
    expect(workspace.uploadChunk.mock.calls[0][1]).toMatchObject({ parallel_id: 0, offset: 0, size: 5, file_size: 10 })
    expect(workspace.uploadChunk.mock.calls[1][1]).toMatchObject({ parallel_id: 1, offset: 5, size: 5, file_size: 10 })
    expect(workspace.uploadComplete).toHaveBeenCalledWith('upload-1')
    expect(path).toBe('/tmp/report.pdf')
  })

  it('handles a zero-size file (no chunks) and completes immediately', async () => {
    mockUploadInit({ chunks: [] })
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const file = makeFile('empty.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 0)
    const path = await uploadFileToDir(file, '/tmp')

    expect(workspace.uploadChunk).not.toHaveBeenCalled()
    expect(workspace.uploadComplete).toHaveBeenCalledWith('upload-1')
    expect(path).toBe('/tmp/empty.docx')
  })

  it('cancels the upload when a chunk fails', async () => {
    mockUploadInit({ chunks: [{ parallel_id: 0, offset: 0, size: 5 }] })
    workspace.uploadChunk.mockReturnValue({ promise: Promise.reject(new Error('boom')) })
    workspace.uploadCancel.mockResolvedValue({ status: 'cancelled' })

    const file = makeFile('x.png', 'image/png', 5)
    await expect(uploadFileToDir(file, '/tmp')).rejects.toThrow('boom')
    expect(workspace.uploadCancel).toHaveBeenCalledWith('upload-1')
    expect(workspace.uploadComplete).not.toHaveBeenCalled()
  })
})

describe('pasteTimestamp', () => {
  it('formats HHMMSS_ms from local time', () => {
    const d = new Date(2024, 0, 1, 14, 30, 25, 123)
    expect(pasteTimestamp(d)).toBe('143025_123')
  })

  it('pads single-digit components', () => {
    const d = new Date(2024, 0, 1, 3, 5, 7, 9)
    expect(pasteTimestamp(d)).toBe('030507_009')
  })
})

describe('stampPastedFileNames', () => {
  beforeEach(() => {
    resetPasteStamp()
  })

  it('keeps the base name and extension and appends a timestamp', () => {
    const [file] = stampPastedFileNames([makeFile('image.png', 'image/png')], () => FIXED_MS)
    expect(file.name).toMatch(TIMESTAMPED_NAME)
    expect(file.name).toBe('image_143025_123.png')
  })

  it('handles files without an extension', () => {
    const [file] = stampPastedFileNames([makeFile('README', 'text/plain')], () => FIXED_MS)
    expect(file.name).toBe('README_143025_123')
  })

  it('gives distinct names to same-named files within one paste batch', () => {
    // Same clock tick for every file in the batch — the monotonic guard still
    // forces strictly increasing stamps so nothing collides.
    const files = [
      makeFile('shot.png', 'image/png'),
      makeFile('shot.png', 'image/png'),
      makeFile('shot.png', 'image/png'),
    ]
    const named = stampPastedFileNames(files, () => FIXED_MS)
    const names = named.map((f) => f.name)
    expect(new Set(names).size).toBe(3)
    expect(names[0]).toBe('shot_143025_123.png')
    // 123ms -> 124ms -> 125ms: same readable second, unique names.
    expect(names[1]).toBe('shot_143025_124.png')
    expect(names[2]).toBe('shot_143025_125.png')
  })

  it('gives distinct names across consecutive paste batches', () => {
    const stampA = stampPastedFileNames([makeFile('image.png', 'image/png')], () => FIXED_MS)[0].name
    const stampB = stampPastedFileNames([makeFile('image.png', 'image/png')], () => FIXED_MS)[0].name
    expect(stampA).not.toBe(stampB)
  })

  it('resets the monotonic counter', () => {
    const a = stampPastedFileNames([makeFile('a.png', 'image/png')], () => FIXED_MS)[0].name
    resetPasteStamp()
    const b = stampPastedFileNames([makeFile('a.png', 'image/png')], () => FIXED_MS)[0].name
    expect(a).toBe(b)
  })
})

describe('uploadFilesToPasteDir', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetPasteDirCache()
    resetPasteStamp()
    workspace.pasteDir.mockResolvedValue({ path: '/tmp' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('uploads each pasted file into the paste dir with a timestamped name', async () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(FIXED_MS)
    mockUploadInit({ uploadId: 'u1', chunks: [{ parallel_id: 0, offset: 0, size: 1 }] })
    mockUploadChunk()
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const files = [
      makeFile('img.png', 'image/png'),
      makeFile('doc.pdf', 'application/pdf'),
      makeFile('docx.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    ]
    const paths = await uploadFilesToPasteDir(files)

    expect(workspace.pasteDir).toHaveBeenCalled()
    expect(workspace.uploadInit).toHaveBeenCalledTimes(3)
    expect(workspace.uploadInit.mock.calls.map((c) => c[0].file_name)).toEqual([
      'img_143025_123.png',
      'doc_143025_124.pdf',
      'docx_143025_125.docx',
    ])
    expect(paths).toEqual(['/tmp/img_143025_123.png', '/tmp/doc_143025_124.pdf', '/tmp/docx_143025_125.docx'])
    nowSpy.mockRestore()
  })

  it('never reuses a name across pastes, even slow ones with the same base name', async () => {
    // Two sequential paste events, each uploading `image.png`. No directory
    // listing is consulted (and none can help reliably in the shared /tmp
    // paste dir) — timestamps alone must keep them distinct.
    const nowSpy = vi.spyOn(Date, 'now')
      .mockReturnValueOnce(FIXED_MS) // paste 1: 14:30:25.123
      .mockReturnValueOnce(FIXED_MS + 1000) // paste 2: 14:30:26.123 (a second later)
    mockUploadInit()
    mockUploadChunk()
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const first = await uploadFilesToPasteDir([makeFile('image.png', 'image/png')])
    const second = await uploadFilesToPasteDir([makeFile('image.png', 'image/png')])

    expect(workspace.list).not.toHaveBeenCalled()
    expect(first).toEqual(['/tmp/image_143025_123.png'])
    expect(second).toEqual(['/tmp/image_143026_123.png'])
    // The second screenshot uploads under its own name, not overwriting the first.
    expect(workspace.uploadInit.mock.calls[1][0].file_name).toBe('image_143026_123.png')
    nowSpy.mockRestore()
  })

  it('keeps names distinct even for two pastes within the same second', async () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(FIXED_MS) // same tick both times
    mockUploadInit()
    mockUploadChunk()
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const first = await uploadFilesToPasteDir([makeFile('image.png', 'image/png')])
    const second = await uploadFilesToPasteDir([makeFile('image.png', 'image/png')])

    expect(first).toEqual(['/tmp/image_143025_123.png'])
    expect(second).toEqual(['/tmp/image_143025_124.png'])
    nowSpy.mockRestore()
  })

  it('keeps names distinct across overlapping paste batches (no race)', async () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(FIXED_MS)
    mockUploadInit()
    mockUploadChunk()
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const pathsA = uploadFilesToPasteDir([makeFile('image.png', 'image/png')])
    const pathsB = uploadFilesToPasteDir([makeFile('image.png', 'image/png')])
    const [a, b] = await Promise.all([pathsA, pathsB])

    expect(a).toEqual(['/tmp/image_143025_123.png'])
    expect(b).toEqual(['/tmp/image_143025_124.png'])
    expect(workspace.uploadInit.mock.calls[1][0].file_name).toBe('image_143025_124.png')
    nowSpy.mockRestore()
  })
})
