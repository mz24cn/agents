/**
 * Shared workspace upload helpers.
 *
 * Reuses the same chunked upload pipeline as the Workspace File Manager
 * panel (uploadInit -> uploadChunk -> uploadComplete) so clipboard-pasted
 * files from the ChatInput behave exactly like the file manager's
 * "paste upload" + "select file" operations.
 */

import { workspace } from './api.js'

/** Normalize an upload target path: strip traversal and duplicate separators. */
function normalizeUploadPath(path) {
  return String(path || '')
    .replace(/\\/g, '/')
    .split('/')
    .filter((part) => part && part !== '.' && part !== '..')
    .join('/')
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
 * @param {File} file              File to upload
 * @param {string} targetDirPath   Absolute directory path to upload into
 * @param {{onProgress?: Function}} [options]
 * @returns {Promise<string>} Absolute path of the uploaded file
 */
export async function uploadFileToDir(file, targetDirPath, { onProgress } = {}) {
  const init = await workspace.uploadInit({
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
      const request = workspace.uploadChunk(upload_id, chunk, body, (uploaded) => {
        onProgress?.({ name: file.name, uploaded, size: chunk.size })
      })
      await request.promise
    }
    await workspace.uploadComplete(upload_id)
    return joinPath(targetDirPath, file.name)
  } catch (err) {
    try { await workspace.uploadCancel(upload_id) } catch { /* best-effort cleanup */ }
    throw err
  }
}

let cachedPasteDir = null

/** Resolve (and cache) the clipboard paste directory from the backend. */
export async function getPasteDir() {
  if (cachedPasteDir) return cachedPasteDir
  const data = await workspace.pasteDir()
  cachedPasteDir = data.path
  return cachedPasteDir
}

/** Reset the cached paste directory (mainly for tests). */
export function resetPasteDirCache() {
  cachedPasteDir = null
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
 * backend: `/tmp` on Linux, OS temp dir on Windows).
 *
 * @param {File[]} files
 * @param {{onProgress?: Function}} [options]
 * @returns {Promise<string[]>} Absolute paths of the uploaded files
 */
export async function uploadFilesToPasteDir(files, { onProgress } = {}) {
  const pasteDir = await getPasteDir()
  // Timestamped names are self-unique, so each pasted file keeps its own name
  // and can never silently overwrite a previous paste of the same-named file.
  const named = stampPastedFileNames(files)
  const paths = []
  for (const file of named) {
    paths.push(await uploadFileToDir(file, pasteDir, { onProgress }))
  }
  return paths
}
