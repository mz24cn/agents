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
  getPasteDir,
  stampPastedFileNames,
  pasteTimestamp,
  resetPasteDirCache,
  resetPasteStamp,
  isMissingChunksError,
  isTransientUploadError,
  fileLanded,
  missingChunkIds,
  chunkRequestPromise,
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

/**
 * A standalone mock of a remote workspace API (mirrors `remoteWorkspace`'s
 * shape). Passed explicitly via `{ api }`, so no module mocking is needed —
 * this is exactly how the remote target reaches the shared upload pipeline.
 */
function makeRemoteApi({ pasteDir = '/child/tmp' } = {}) {
  return {
    pasteDir: vi.fn().mockResolvedValue({ path: pasteDir }),
    uploadInit: vi.fn().mockResolvedValue({ upload_id: 'remote-1', chunks: [{ parallel_id: 0, offset: 0, size: 10 }] }),
    uploadChunk: vi.fn().mockReturnValue({ promise: Promise.resolve({ status: 'uploaded' }) }),
    uploadComplete: vi.fn().mockResolvedValue({ status: 'completed' }),
    uploadCancel: vi.fn().mockResolvedValue({ status: 'cancelled' }),
  }
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

  it('re-uploads the reported missing chunks and retries complete (flaky-link self-heal)', async () => {
    mockUploadInit({ chunks: [
      { parallel_id: 0, offset: 0, size: 5 },
      { parallel_id: 1, offset: 5, size: 5 },
    ] })
    mockUploadChunk()
    const missingErr = {
      status: 409,
      code: 'UPLOAD_NOT_READY: some chunks are missing',
      data: { error: 'UPLOAD_NOT_READY: some chunks are missing', missing_chunks: [1] },
    }
    workspace.uploadComplete
      .mockRejectedValueOnce(missingErr)
      .mockResolvedValue({ status: 'completed' })

    const file = makeFile('photo.png', 'image/png', 10)
    const path = await uploadFileToDir(file, '/tmp')

    // 2 initial chunks + exactly 1 re-upload (only the reported chunk).
    expect(workspace.uploadChunk).toHaveBeenCalledTimes(3)
    const reup = workspace.uploadChunk.mock.calls[2][1]
    expect(reup).toMatchObject({ parallel_id: 1, offset: 5, size: 5, file_size: 10 })
    expect(workspace.uploadComplete).toHaveBeenCalledTimes(2)
    expect(workspace.uploadCancel).not.toHaveBeenCalled()
    expect(path).toBe('/tmp/photo.png')
  })

  it('re-uploads every chunk when the backend reports no missing_chunks list', async () => {
    mockUploadInit({ chunks: [
      { parallel_id: 0, offset: 0, size: 5 },
      { parallel_id: 1, offset: 5, size: 5 },
    ] })
    mockUploadChunk()
    workspace.uploadComplete
      .mockRejectedValueOnce({ status: 409, code: 'UPLOAD_NOT_READY: some chunks are missing' })
      .mockResolvedValue({ status: 'completed' })

    const file = makeFile('photo.png', 'image/png', 10)
    const path = await uploadFileToDir(file, '/tmp')

    // 2 initial chunks + 2 re-uploads (no list -> re-send everything).
    expect(workspace.uploadChunk).toHaveBeenCalledTimes(4)
    expect(workspace.uploadComplete).toHaveBeenCalledTimes(2)
    expect(path).toBe('/tmp/photo.png')
  })

  it('fails loudly (never completes) when uploadChunk returns a non-awaitable handle', async () => {
    // Regression guard for the remote-upload bug where uploadChunk returned
    // a bare promise: `await request.promise` resolved on undefined, the
    // chunk was marked done while still in flight, and complete ran against
    // a child that had not received the chunk yet.
    mockUploadInit({ chunks: [{ parallel_id: 0, offset: 0, size: 5 }] })
    workspace.uploadChunk.mockReturnValue({ status: 'uploaded' })
    workspace.uploadCancel.mockResolvedValue({ status: 'cancelled' })

    const file = makeFile('x.png', 'image/png', 5)
    await expect(uploadFileToDir(file, '/tmp')).rejects.toThrow(/awaitable handle/)
    expect(workspace.uploadComplete).not.toHaveBeenCalled()
  })

  it('does not heal a non-missing complete error (still cancels and throws)', async () => {
    mockUploadInit({ chunks: [{ parallel_id: 0, offset: 0, size: 5 }] })
    mockUploadChunk()
    workspace.uploadComplete.mockRejectedValue({ status: 500, code: 'SERVER_ERROR: internal server error' })
    workspace.uploadCancel.mockResolvedValue({ status: 'cancelled' })

    const file = makeFile('x.png', 'image/png', 5)
    await expect(uploadFileToDir(file, '/tmp')).rejects.toThrow()
    // No re-upload on a non-missing failure.
    expect(workspace.uploadChunk).toHaveBeenCalledTimes(1)
    expect(workspace.uploadComplete).toHaveBeenCalledTimes(1)
    expect(workspace.uploadCancel).toHaveBeenCalledWith('upload-1')
  })
})

describe('isMissingChunksError / missingChunkIds', () => {
  it('detects the 409 missing-chunks error by code or message', () => {
    expect(isMissingChunksError({ status: 409, code: 'UPLOAD_NOT_READY: some chunks are missing' })).toBe(true)
    expect(isMissingChunksError({ status: 409, message: 'UPLOAD_NOT_READY: some chunks are missing' })).toBe(true)
    expect(isMissingChunksError({ status: 409, code: 'UPLOAD_CANCELLED: upload has been cancelled' })).toBe(false)
    expect(isMissingChunksError({ status: 500, code: 'some chunks are missing' })).toBe(false)
    expect(isMissingChunksError(null)).toBe(false)
  })

  it('extracts the missing parallel_ids and falls back to null', () => {
    expect(missingChunkIds({ data: { missing_chunks: [1, 2] } })).toEqual([1, 2])
    expect(missingChunkIds({ data: {} })).toBeNull()
    expect(missingChunkIds({})).toBeNull()
  })
})

describe('chunkRequestPromise', () => {
  it('awaits the inner promise of a { promise, abort } handle', async () => {
    const p = Promise.resolve({ status: 'uploaded' })
    await expect(chunkRequestPromise({ promise: p, abort: () => {} })).resolves.toEqual({ status: 'uploaded' })
  })

  it('accepts a bare promise handle', async () => {
    await expect(chunkRequestPromise(Promise.resolve('ok'))).resolves.toBe('ok')
  })

  it('throws (instead of resolving on undefined) for a non-awaitable handle', () => {
    // `await handle.promise` on a handle without a promise resolves on
    // undefined: the chunk is marked completed while its body is still in
    // flight, and the next complete then reports it missing server-side.
    expect(() => chunkRequestPromise(undefined)).toThrow(/awaitable handle/)
    expect(() => chunkRequestPromise(null)).toThrow(/awaitable handle/)
    expect(() => chunkRequestPromise({})).toThrow(/awaitable handle/)
    expect(() => chunkRequestPromise({ status: 'uploaded' })).toThrow(/awaitable handle/)
  })
})

describe('isTransientUploadError', () => {
  it('treats network errors, 5xx and timeouts as transient', () => {
    expect(isTransientUploadError(new Error('Upload network error'))).toBe(true)
    expect(isTransientUploadError({ status: 502, message: 'child_unreachable' })).toBe(true)
    expect(isTransientUploadError({ status: 500, message: 'boom' })).toBe(true)
    expect(isTransientUploadError({ status: 504, message: 'gateway timeout' })).toBe(true)
  })

  it('treats deterministic 4xx verdicts as final (no retry)', () => {
    expect(isTransientUploadError({ status: 400, message: 'CHUNK_SIZE_MISMATCH' })).toBe(false)
    expect(isTransientUploadError({ status: 404, message: 'UPLOAD_NOT_FOUND' })).toBe(false)
    expect(isTransientUploadError({ status: 409, message: 'UPLOAD_CANCELLED' })).toBe(false)
    expect(isTransientUploadError({ name: 'AbortError' })).toBe(false)
    expect(isTransientUploadError(null)).toBe(false)
  })
})

describe('fileLanded', () => {
  const items = [
    { name: 'a.txt', is_dir: false, size: 12 },
    { name: 'sub', is_dir: true, size: 0 },
    { name: 'big.bin', is_dir: false, size: 10485760 },
  ]

  it('matches on exact name and size', () => {
    expect(fileLanded(items, 'big.bin', 10485760)).toBe(true)
    expect(fileLanded(items, 'big.bin', 999)).toBe(false)
    expect(fileLanded(items, 'missing.bin', 10485760)).toBe(false)
  })

  it('ignores directories and handles empty input', () => {
    expect(fileLanded(items, 'sub', 0)).toBe(false)
    expect(fileLanded(null, 'a.txt', 12)).toBe(false)
    expect(fileLanded([], 'a.txt', 12)).toBe(false)
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

// ---------------------------------------------------------------------------
// Remote paste target (ChatInput in a remote-bound session)
// ---------------------------------------------------------------------------

describe('paste upload target selection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetPasteDirCache()
    resetPasteStamp()
    workspace.pasteDir.mockResolvedValue({ path: '/tmp' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('uploadFileToDir drives the passed api (remote) and returns the child path', async () => {
    const remote = makeRemoteApi()

    const file = makeFile('a.png', 'image/png', 10)
    const path = await uploadFileToDir(file, '/child/tmp', { api: remote })

    expect(remote.uploadInit).toHaveBeenCalledWith({
      workspace_id: 'default',
      file_name: 'a.png',
      file_size: 10,
      target_dir_path: '/child/tmp',
      target_path: 'a.png',
    })
    expect(remote.uploadChunk).toHaveBeenCalledTimes(1)
    expect(remote.uploadComplete).toHaveBeenCalledWith('remote-1')
    // The parent workspace must not be touched when a remote api is passed.
    expect(workspace.uploadInit).not.toHaveBeenCalled()
    expect(workspace.uploadChunk).not.toHaveBeenCalled()
    expect(path).toBe('/child/tmp/a.png')
  })

  it('uploadFileToDir falls back to the parent workspace when no api is passed', async () => {
    mockUploadInit({ uploadId: 'p1', chunks: [{ parallel_id: 0, offset: 0, size: 10 }] })
    mockUploadChunk()
    workspace.uploadComplete.mockResolvedValue({ status: 'completed' })

    const path = await uploadFileToDir(makeFile('a.png', 'image/png', 10), '/tmp')

    expect(workspace.uploadInit).toHaveBeenCalledTimes(1)
    expect(workspace.uploadComplete).toHaveBeenCalledWith('p1')
    expect(path).toBe('/tmp/a.png')
  })

  it('uploadFilesToPasteDir uploads through the remote api and returns child paths', async () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(FIXED_MS)
    const remote = makeRemoteApi({ pasteDir: '/child/tmp' })

    const paths = await uploadFilesToPasteDir(
      [makeFile('img.png', 'image/png', 10)],
      { api: remote, cacheKey: 'env-a' },
    )

    expect(remote.pasteDir).toHaveBeenCalledTimes(1)
    expect(remote.uploadInit).toHaveBeenCalledTimes(1)
    expect(remote.uploadInit.mock.calls[0][0].file_name).toBe('img_143025_123.png')
    expect(remote.uploadChunk).toHaveBeenCalledTimes(1)
    expect(remote.uploadComplete).toHaveBeenCalledWith('remote-1')
    // Nothing leaks to the parent workspace.
    expect(workspace.pasteDir).not.toHaveBeenCalled()
    expect(workspace.uploadInit).not.toHaveBeenCalled()
    expect(paths).toEqual(['/child/tmp/img_143025_123.png'])
    nowSpy.mockRestore()
  })
})

describe('getPasteDir per-target cache', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetPasteDirCache()
    workspace.pasteDir.mockResolvedValue({ path: '/tmp' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('resolves parent and remote paste dirs independently (once each, no mixing)', async () => {
    const remote = makeRemoteApi({ pasteDir: '/child/tmp' })

    expect(await getPasteDir()).toBe('/tmp')
    expect(await getPasteDir({ api: remote, cacheKey: 'env-a' })).toBe('/child/tmp')
    // Second lookups hit the per-target cache: no extra round-trips.
    expect(await getPasteDir()).toBe('/tmp')
    expect(await getPasteDir({ api: remote, cacheKey: 'env-a' })).toBe('/child/tmp')

    expect(workspace.pasteDir).toHaveBeenCalledTimes(1)
    expect(remote.pasteDir).toHaveBeenCalledTimes(1)
  })

  it('keeps different child envs in separate cache slots', async () => {
    const childA = makeRemoteApi({ pasteDir: '/a/tmp' })
    const childB = makeRemoteApi({ pasteDir: '/b/tmp' })

    expect(await getPasteDir({ api: childA, cacheKey: 'env-a' })).toBe('/a/tmp')
    expect(await getPasteDir({ api: childB, cacheKey: 'env-b' })).toBe('/b/tmp')
    expect(await getPasteDir({ api: childA, cacheKey: 'env-a' })).toBe('/a/tmp')
    expect(await getPasteDir({ api: childB, cacheKey: 'env-b' })).toBe('/b/tmp')

    expect(childA.pasteDir).toHaveBeenCalledTimes(1)
    expect(childB.pasteDir).toHaveBeenCalledTimes(1)
  })

  it('resetPasteDirCache clears every target slot', async () => {
    workspace.pasteDir
      .mockResolvedValueOnce({ path: '/tmp' })
      .mockResolvedValueOnce({ path: '/tmp-2' })
    const remote = makeRemoteApi({ pasteDir: '/child/tmp' })

    await getPasteDir()
    await getPasteDir({ api: remote, cacheKey: 'env-a' })
    resetPasteDirCache()

    // Both targets re-resolve after a reset (the remote mock still returns '/child/tmp').
    expect(await getPasteDir()).toBe('/tmp-2')
    expect(await getPasteDir({ api: remote, cacheKey: 'env-a' })).toBe('/child/tmp')
    expect(workspace.pasteDir).toHaveBeenCalledTimes(2)
    expect(remote.pasteDir).toHaveBeenCalledTimes(2)
  })
})
