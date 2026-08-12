/**
 * Clipboard paste helpers for the ChatInput box.
 *
 * Pure functions so the paste handling logic can be unit-tested without a DOM.
 * Mirrors the exact logic used by ChatInput.svelte's handlePaste.
 */

/**
 * Extract File objects from a clipboard paste event's DataTransfer.
 *
 * Prefers DataTransferItem entries (kind === 'file'), which is how browsers
 * expose files dropped/copied onto the clipboard (images, PDFs, DOCX, ...).
 * Falls back to the DataTransfer.files collection.
 *
 * @param {DataTransfer|Object|null} clipboardData
 * @returns {File[]}
 */
export function extractPastedFiles(clipboardData) {
  if (!clipboardData) return []
  const items = Array.from(clipboardData.items || [])
  const fromItems = items
    .filter((it) => it && it.kind === 'file' && typeof it.getAsFile === 'function')
    .map((it) => it.getAsFile())
    .filter(Boolean)
  if (fromItems.length > 0) return fromItems
  return Array.from(clipboardData.files || [])
}

/**
 * Build a space-separated list of `<file>` tags from absolute paths.
 * These tags are parsed by the backend into file attachments, and rendered as
 * removable chips inside the ChatInput editor.
 *
 * @param {string[]} paths
 * @returns {string}
 */
export function buildFileRefs(paths) {
  return (paths || []).map((p) => `<file>${p}</file>`).join(' ')
}

/**
 * Orchestrate a paste event: if the clipboard carries files, upload them into
 * the paste directory and return their absolute paths; otherwise return null
 * (caller should fall back to plain-text insertion).
 *
 * @param {Object} clipboardData   Paste event's clipboardData
 * @param {{ uploadFiles: Function }} deps
 * @returns {Promise<string[]|null>}
 */
export async function handleClipboardPaste(clipboardData, { uploadFiles } = {}) {
  const files = extractPastedFiles(clipboardData)
  if (files.length === 0) return null
  return uploadFiles(files)
}
