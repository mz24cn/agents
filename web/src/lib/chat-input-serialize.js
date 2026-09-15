/**
 * Serialization helpers for the ChatInput rich-text box (contenteditable).
 *
 * The editor is a plain `contenteditable` div. Its children are the browser's
 * own DOM: ordinary text nodes, file-ref chips, and — for line breaks made with
 * Shift+Enter — block elements (`<div>` / `<p>`).
 *
 * A single empty line is represented by Chromium as `<div><br></div>`. That
 * placeholder must serialize to exactly one `\n`: the `<br>` and the wrapping
 * `<div>` must not each contribute a newline, otherwise one Shift+Enter shows
 * up as two blank lines in the sent message.
 *
 * Kept as pure functions over the node shape (nodeType / tagName / childNodes /
 * dataset) — no reliance on the global `Node` — so they can be unit-tested
 * without a browser.
 */

// nodeType values (see DOM spec); avoids depending on the global `Node`.
const TEXT_NODE = 3
const ELEMENT_NODE = 1

/**
 * Serialize a single node to its plain-text (<file>…) representation.
 * @param {Node} node
 * @returns {string}
 */
export function serializeNode(node) {
  if (node.nodeType === TEXT_NODE) return node.nodeValue ?? ''
  if (node.nodeType !== ELEMENT_NODE) return ''

  const el = node
  if (el.dataset?.fileRef) {
    return `<file>${el.dataset.fileRef}</file>`
  }
  if (el.tagName === 'BR') return '\n'

  const isBlock = el.tagName === 'DIV' || el.tagName === 'P'
  // Chromium's placeholder for one empty line. Count it once.
  if (isBlock && el.childNodes.length === 1 &&
    el.firstChild.nodeType === ELEMENT_NODE && el.firstChild.tagName === 'BR') {
    return '\n'
  }

  let out = ''
  for (const child of el.childNodes) out += serializeNode(child)
  if (isBlock) out += '\n'
  return out
}

/**
 * Serialize the whole editor back to the plain text submitted to the backend.
 *
 * @param {HTMLElement|null} editorEl the contenteditable element
 * @param {string} [fallback] returned when the editor is not mounted
 * @returns {string}
 */
export function serializeEditor(editorEl, fallback = '') {
  if (!editorEl) return fallback

  let out = ''
  for (const child of editorEl.childNodes) {
    // When a <div>/<p> follows a text node (e.g. first line is bare text,
    // subsequent lines wrapped in <div> by the browser), insert a newline
    // before the <div> to prevent lines from merging.
    if (out && !out.endsWith('\n') &&
      child.nodeType === ELEMENT_NODE &&
      (child.tagName === 'DIV' || child.tagName === 'P')) {
      out += '\n'
    }
    out += serializeNode(child)
  }
  // Normalize line endings (CRLF / CR → LF), then strip trailing newline
  return out.replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n$/g, '')
}
