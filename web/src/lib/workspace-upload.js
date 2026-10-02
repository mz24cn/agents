/**
 * Shared workspace upload helpers.
 *
 * Reuses the same chunked upload pipeline as the Workspace File Manager
 * panel (uploadInit -> uploadChunk -> uploadComplete) so clipboard-pasted
 * files from the ChatInput behave exactly like the file manager's
 * "paste upload" + "select file" operations.
 */

import { workspace as defaultWorkspace } from './api.js'

/** Normalize an upload target path: strip traversal and duplicate separators. */
function normalizeUploadPath(path) {
  return String(path || '')
    .replace(/\\/g, '/')
    .split('/')
    .filter((part) => part && part !== '.' && part !== '..')
    .join('/')
}

/**
 * True when an upload-complete error means the server no longer has one or
 * more chunks that the client believes it already uploaded.
 *
 * This happens when a flaky link (mobile networks retransmit chunk PUTs; a
 * remote child running an older backend may let a retransmitted PUT reset an
 * already-uploaded chunk's state) desynchronizes server and client. The
 * client still holds the file data, so it can re-upload the missing chunks
 * and retry instead of failing the task.
 */
export function isMissingChunksError(err) {
  if (!err || err.status !== 409) return false
  const code = err.code || err.message || ''
  return /some chunks are missing/i.test(String(code))
}

/**
 * parallel_ids the server reports as missing, or null when the backend is
 * too old to include the list (callers must then re-upload every chunk).
 */
export function missingChunkIds(err) {
  const ids = err?.data?.missing_chunks
  return Array.isArray(ids) ? ids : null
}

/**
 * True when a chunk-PUT failure is TRANSIENT (worth one retry).
 *
 * Over a tunnel the response can be lost even though the chunk write
 * succeeded on the child (e.g. the response sat behind a stalled
 * connection, or the link hiccuped) -- and a duplicate PUT is idempotent
 * server-side (an already-uploaded chunk returns 200 without touching its
 * state).  Deterministic 4xx verdicts are not transient: retrying a 400
 * (size mismatch) / 404 (unknown upload or chunk) / 409 (cancelled or
 * already completing) can only fail again.
 */
export function isTransientUploadError(err) {
  if (!err) return false
  if (err.name === 'AbortError') return false
  const status = Number(err.status || 0)
  if (status === 400 || status === 404 || status === 409) return false
  return true
}

/**
 * Await the settlement of a chunk-PUT handle.
 *
 * Both the parent api (`workspace.uploadChunk`) and the remote one
 * (`remoteWorkspace.uploadChunk`) return `{ promise, abort }`; a bare
 * promise (or any thenable) is accepted too, because awaiting it is exactly
 * as correct.  Anything that is NOT awaitable is a bug in the caller's
 * contract and must fail loudly: `await handle.promise` on a handle that
 * lost its `promise` key resolves on `undefined` immediately, silently
 * marking the chunk "completed" while its body is still in flight -- the
 * next complete then reports the chunk as missing on the server, which no
 * amount of re-uploading can fix.
 *
 * @param {{promise?: Promise<any>}|Promise<any>} request chunk-PUT handle
 * @returns {Promise<any>}
 */
export function chunkRequestPromise(request) {
  const p = (request && request.promise) || request
  if (!p || typeof p.then !== 'function') {
    throw new Error('uploadChunk did not return an awaitable handle (expected { promise, abort })')
  }
  return p
}

/**
 * True when a directory listing shows the uploaded file already landed
 * (exact name + size).  Used to recover from a lost complete response:
 * the child may have finished the merge (and popped its upload state)
 * while the response never reached the browser.
 */
export function fileLanded(items, fileName, fileSize) {
  if (!Array.isArray(items) || !fileName) return false
  return items.some(
    (item) => item && !item.is_dir
      && item.name === fileName
      && (fileSize == null || item.size === fileSize),
  )
}

/** Join a directory path and a filename, preserving the directory's separator style. */
export function joinPath(dir, name) {
  if (!dir) return name
  const sep = dir.includes('\\') ? '\\' : '/'
  if (dir.endsWith('/') || dir.endsWith('\\')) return dir + name
  return dir + sep + name
}

/**
 * Upload a single File into targetDirPath using the chunked workspace upload API.
 *
 * The API client is an explicit parameter so callers never have to guess where
 * the file should land: the parent `workspace` (default) writes to the parent
 * host, while `remoteWorkspace` writes straight to the bound child env.  The
 * two expose the same `uploadInit -> uploadChunk -> uploadComplete /
 * uploadCancel` contract, so the whole pipeline below is target-agnostic.
 *
 * @param {File} file              File to upload
 * @param {string} targetDirPath   Absolute directory path to upload into
 * @param {object} [options]
 * @param {Function} [options.onProgress]
 * @param {object} [options.api]   workspace API to drive the upload; defaults to
 *   the parent `workspace` (pass `remoteWorkspace` to target a child env)
 * @returns {Promise<string>} Absolute path of the uploaded file
 */
export async function uploadFileToDir(file, targetDirPath, { onProgress, api = defaultWorkspace } = {}) {
  const init = await api.uploadInit({
    workspace_id: 'default',
    file_name: file.name,
    file_size: file.size,
    target_dir_path: targetDirPath,
    target_path: normalizeUploadPath(file.name),
  })
  const { upload_id, chunks = [] } = init
  // The backend's upload/init response chunks only carry {parallel_id, offset,
  // size} — the file manager enriches them with the total file size before
  // sending X-File-Size, so we must do the same here (otherwise the header
  // becomes "undefined" and the backend rejects it).
  const sizedChunks = chunks.map((chunk) => ({ ...chunk, file_size: file.size }))
  try {
    for (const chunk of sizedChunks) {
      const body = file.slice(chunk.offset, chunk.offset + chunk.size)
      const request = api.uploadChunk(upload_id, chunk, body, (uploaded) => {
        onProgress?.({ name: file.name, uploaded, size: chunk.size })
      })
      await chunkRequestPromise(request)
    }
    try {
      await api.uploadComplete(upload_id)
    } catch (err) {
      if (!isMissingChunksError(err)) throw err
      // The file data is still local: re-upload exactly the chunks the
      // server reports as missing (all of them when the backend does not
      // report which) and retry complete once.
      const ids = missingChunkIds(err)
      const retry = ids
        ? sizedChunks.filter((chunk) => ids.includes(chunk.parallel_id))
        : sizedChunks
      for (const chunk of retry) {
        const body = file.slice(chunk.offset, chunk.offset + chunk.size)
        await chunkRequestPromise(api.uploadChunk(upload_id, chunk, body))
      }
      await api.uploadComplete(upload_id)
    }
    return joinPath(targetDirPath, file.name)
  } catch (err) {
    try { await api.uploadCancel(upload_id) } catch { /* best-effort cleanup */ }
    throw err
  }
}

// Pasted files land in a per-target temp dir: the parent host and every child
// env have their own `/tmp` (or OS temp dir).  A single module-level value would
// leak one env's paste dir into another, so switching env/session would upload
// into the wrong host and leave the inserted `<file>` refs dangling.  Key the
// cache by the resolved target: an explicit `cacheKey` from the caller (a child
// env id) when given, else the API object identity (which separates the parent
// from the remote facade).
const pasteDirCache = new Map()

function pasteDirCacheKey(api, cacheKey) {
  if (cacheKey !== undefined && cacheKey !== null && cacheKey !== '') {
    return `target:${cacheKey}`
  }
  return api
}

/**
 * Resolve (and cache per target) the clipboard paste directory from the backend.
 *
 * @param {object} [options]
 * @param {object} [options.api]      workspace API to query (defaults to parent)
 * @param {string} [options.cacheKey] stable id of the target (e.g. a child env
 *   id) so different children never share a cached paste dir
 * @returns {Promise<string>} Absolute paste directory path
 */
export async function getPasteDir({ api = defaultWorkspace, cacheKey } = {}) {
  const key = pasteDirCacheKey(api, cacheKey)
  const cached = pasteDirCache.get(key)
  if (cached) return cached
  const data = await api.pasteDir()
  const path = data.path
  pasteDirCache.set(key, path)
  return path
}

/** Reset the cached paste directory for every target (mainly for tests). */
export function resetPasteDirCache() {
  pasteDirCache.clear()
}

/**
 * Format a timestamp as HHMMSS_ms (e.g. 143025_123 = 14:30:25.123).
 * The 时分秒 part keeps pasted-file names human-readable; the milliseconds
 * keep them unique even for several pastes within the same second.
 */
export function pasteTimestamp(date = new Date()) {
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  const ss = String(date.getSeconds()).padStart(2, '0')
  const ms = String(date.getMilliseconds()).padStart(3, '0')
  return `${hh}${mm}${ss}_${ms}`
}

/** Last stamp used; strictly incremented so names can never repeat. */
let lastPasteStampMs = 0

/** Reset the monotonic stamp counter (mainly for tests). */
export function resetPasteStamp() {
  lastPasteStampMs = 0
}

/**
 * Give each pasted file a unique, timestamped name:
 * `image.png` -> `image_143025_123.png`.
 *
 * Uniqueness comes from a module-level monotonic counter, so no directory
 * listing round-trip is needed: pasting the same-named screenshots slowly,
 * rapidly or even concurrently can never collide with or overwrite an earlier
 * paste (the /tmp-style paste dir is shared, volatile and often huge, so
 * listing-based dedup is unreliable there).
 */
export function stampPastedFileNames(files, now = Date.now) {
  return (files || []).map((file) => {
    lastPasteStampMs = Math.max(now(), lastPasteStampMs + 1)
    const dot = file.name.lastIndexOf('.')
    const base = dot > 0 ? file.name.slice(0, dot) : file.name
    const ext = dot > 0 ? file.name.slice(dot) : ''
    const name = `${base}_${pasteTimestamp(new Date(lastPasteStampMs))}${ext}`
    return new File([file], name, { type: file.type })
  })
}

/**
 * Upload clipboard-pasted files into the paste directory (resolved from the
 * target backend: `/tmp` on Linux, OS temp dir on Windows).
 *
 * @param {File[]} files
 * @param {object} [options]
 * @param {Function} [options.onProgress]
 * @param {object} [options.api]      workspace API to drive both the paste-dir
 *   lookup and the chunked upload; defaults to the parent `workspace`
 * @param {string} [options.cacheKey] stable id of the target (e.g. a child env
 *   id) used to keep per-target paste dirs in separate cache slots
 * @returns {Promise<string[]>} Absolute paths of the uploaded files
 */
export async function uploadFilesToPasteDir(files, { onProgress, api = defaultWorkspace, cacheKey } = {}) {
  const pasteDir = await getPasteDir({ api, cacheKey })
  // Timestamped names are self-unique, so each pasted file keeps its own name
  // and can never silently overwrite a previous paste of the same-named file.
  const named = stampPastedFileNames(files)
  const paths = []
  for (const file of named) {
    paths.push(await uploadFileToDir(file, pasteDir, { onProgress, api }))
  }
  return paths
}
