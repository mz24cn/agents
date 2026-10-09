/**
 * Workspace file references inside user messages — one parser for both spellings.
 *
 * The editor and the wire format use tags:
 *
 *     <file>path</file>
 *
 * The backend expands every tag once, before the message is persisted, so
 * conversation.json stores the *expanded* form (runtime/workspace_manager.py
 * `expand_workspace_file_refs_in_message`):
 *
 *     [Image file attached: <path>]                       image  -> path on msg.images
 *     [Text file attached: <path>]\n```\n<content>\n```   text   -> content inlined
 *     [file attached: <path>]                             other  -> path only
 *
 * Text content blocks are **prepended** at the head of the message (the user's
 * own text stays at the end, which reads better for the model), so a reference
 * appears twice: once with the content, once where the user typed it.  When the
 * model has no "vlm" label, Runtime._apply_vlm_image_fallback prepends the same
 * shape for images, holding a transcription instead of file content:
 *
 *     [Image file attached: <labels>]\n```\n<transcription>\n```
 *
 * Everything that re-displays or re-edits a persisted user message therefore
 * has to fold that form back:
 *
 *  - MessageBubble: render chips instead of raw placeholder text, with the
 *    inlined content collapsed (parseFileRefParts)
 *  - ChatInput / revoke: placeholders back into <file> tags (restoreFileRefTags)
 *  - ImageViewer: msg.images entries are workspace paths, not base64
 *    (resolveImageSource)
 *
 * Kept as pure functions over strings so they can be unit-tested without a
 * browser.
 */

// Group 1: <file> path.  Group 2: optional Image/Text marker.  Group 3: placeholder path.
const REF_RE_SOURCE = '<file>\\s*([^<]+?)\\s*</file>|\\[(?:(Image|Text) )?file attached: ([^\\]]*)\\]'

// A fenced block glued to a placeholder holds that reference's inlined payload
// (text file content, or an image transcription).
const FENCE_RE_SOURCE = '[ \\t]*\\n```[^\\n]*\\n([\\s\\S]*?)\\n```[ \\t]*(?:\\n+|$)'

// Same block, anchored at the start of the message: the backend prepends them.
const LEADING_ATTACHMENT_RE_SOURCE =
  '^\\[(?:(Image|Text) )?file attached: ([^\\]]*)\\]' + FENCE_RE_SOURCE

/** Fresh /g regexes: a shared instance would carry lastIndex between calls. */
function refRe() {
  return new RegExp(REF_RE_SOURCE, 'g')
}

function fenceRe() {
  return new RegExp('^' + FENCE_RE_SOURCE)
}

function leadingAttachmentRe() {
  return new RegExp(LEADING_ATTACHMENT_RE_SOURCE)
}

function kindOf(marker) {
  if (marker === 'Image') return 'image'
  if (marker === 'Text') return 'text'
  return 'other'
}

/** True when the text carries a file reference in either spelling. */
export function hasFileRefs(text) {
  return refRe().test(String(text ?? ''))
}

/**
 * Split the prepended attachment blocks off a persisted message.
 *
 * Each block is `[Kind file attached: ref]` followed by a fenced code block.
 * They are consumed from the head only, which is exactly where the backend puts
 * them; the inline placeholder that marks the user's own position stays in
 * `rest`.
 *
 * @param {string} text
 * @returns {{ attachments: Array<{kind: string, ref: string, content: string}>, rest: string }}
 */
export function splitLeadingFileAttachments(text) {
  let rest = String(text ?? '')
  const attachments = []
  for (;;) {
    const match = leadingAttachmentRe().exec(rest)
    if (!match) break
    attachments.push({
      kind: kindOf(match[1]),
      ref: (match[2] ?? '').trim(),
      content: match[3],
      matched: false,
    })
    rest = rest.slice(match[0].length)
  }
  return { attachments, rest }
}

/**
 * Turn message content into renderable parts.
 *
 * Parts are `{ type: 'text', value }` or
 * `{ type: 'file', ref, kind, content }`, where `kind` is `tag` (a live
 * `<file>` tag), `image`, `text` or `other`, and `content` is the inlined
 * payload when one was found (text file content / image transcription).
 *
 * A prepended attachment block is merged into the inline chip for the same
 * reference, so a file shows up once, where the user put it.  Attachments with
 * no inline counterpart (e.g. the `(inline image)` label of a base64 upload)
 * keep their position at the head.
 *
 * @param {string} text
 * @returns {Array<object>}
 */
export function parseFileRefParts(text) {
  const { attachments, rest } = splitLeadingFileAttachments(text)
  const byRef = new Map()
  for (const attachment of attachments) {
    if (attachment.ref && !byRef.has(attachment.ref)) byRef.set(attachment.ref, attachment)
  }

  const parts = []
  const re = refRe()
  let index = 0
  let match
  while ((match = re.exec(rest)) !== null) {
    if (match.index > index) parts.push({ type: 'text', value: rest.slice(index, match.index) })
    const isTag = match[1] !== undefined
    const ref = (isTag ? match[1] : match[3] ?? '').trim()
    const part = {
      type: 'file',
      ref,
      kind: isTag ? 'tag' : kindOf(match[2]),
      content: null,
    }
    // An inline placeholder can carry its own fenced block (the backend only
    // does this for image/text references, never for bare path references).
    if (part.kind === 'image' || part.kind === 'text') {
      const fence = fenceRe().exec(rest.slice(re.lastIndex))
      if (fence) {
        part.content = fence[1]
        re.lastIndex += fence[0].length
      }
    }
    const attached = byRef.get(ref)
    if (attached) {
      if (part.content === null) part.content = attached.content
      attached.matched = true
    }
    parts.push(part)
    index = re.lastIndex
  }
  if (index < rest.length) parts.push({ type: 'text', value: rest.slice(index) })

  for (const attachment of attachments) {
    if (!attachment.matched) {
      parts.unshift({
        type: 'file',
        ref: attachment.ref,
        kind: attachment.kind,
        content: attachment.content,
      })
    }
  }
  return parts
}

/**
 * Fold a persisted message back into the editor form.
 *
 * Placeholders become `<file>ref</file>` and the inlined payload blocks are
 * dropped: they are regenerated by the backend when the message is sent again,
 * and keeping them would paste a whole file into the input box.  References the
 * backend never paired with a path (an inline base64 upload is labelled
 * `(inline image)`) keep their placeholder text — there is nothing to reference.
 *
 * @param {string} text
 * @returns {string}
 */
export function restoreFileRefTags(text) {
  const { rest } = splitLeadingFileAttachments(text)
  const re = refRe()
  let out = ''
  let index = 0
  let match
  while ((match = re.exec(rest)) !== null) {
    out += rest.slice(index, match.index)
    const isTag = match[1] !== undefined
    const ref = (isTag ? match[1] : match[3] ?? '').trim()
    const kind = isTag ? 'tag' : kindOf(match[2])
    if (kind === 'image' || kind === 'text') {
      const fence = fenceRe().exec(rest.slice(re.lastIndex))
      if (fence) re.lastIndex += fence[0].length
    }
    out += ref ? `<file>${ref}</file>` : match[0]
    index = re.lastIndex
  }
  out += rest.slice(index)
  return out.replace(/^\n+/, '').replace(/\s+$/, '')
}

const IMAGE_MIME_BY_EXT = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.tiff': 'image/tiff',
  '.tif': 'image/tiff',
  '.avif': 'image/avif',
  '.heic': 'image/heic',
}

/** Best-effort MIME type for an image path/name; falls back to image/png. */
export function imageMimeFor(path) {
  const name = String(path ?? '').replace(/\\/g, '/')
  const dot = name.lastIndexOf('.')
  if (dot < 0) return 'image/png'
  return IMAGE_MIME_BY_EXT[name.slice(dot).toLowerCase()] || 'image/png'
}

/**
 * A bare base64 payload (no path separator, base64 alphabet, long enough to be
 * an image).  Mirrors the backend's `is_likely_base64` heuristic in
 * runtime/common.py so both sides agree on what `msg.images` entries mean.
 */
export function isRawBase64(value) {
  const text = String(value ?? '')
  if (text.includes('/') || text.includes('\\')) return false
  const compact = text.replace(/\s+/g, '')
  if (compact.length <= 128) return false
  return /^[A-Za-z0-9+/]+={0,2}$/.test(compact)
}

/**
 * Resolve one `msg.images` entry to something an `<img src>` can load.
 *
 * The field is polymorphic by design: the deprecated file-upload path stored
 * raw base64, a URL stays a URL, and workspace `<file>` references are stored as
 * **paths** on purpose (conversation.json must not carry image bytes).  Paths —
 * local or child-side for remote sessions — are served by
 * `GET /v1/workspace/content`; `restrict=0` because pasted uploads live in the
 * paste directory (e.g. /tmp), outside the workspace.
 *
 * @param {string} value raw entry from msg.images
 * @param {{ content: (path: string, restrict?: boolean) => string }} api
 *        workspace api of the environment that ran the message
 * @returns {string|null} src URL, or null when there is nothing to show
 */
export function resolveImageSource(value, api) {
  const text = String(value ?? '').trim()
  if (!text) return null
  if (text.startsWith('data:')) return text
  if (/^https?:\/\//i.test(text)) return text
  if (text.startsWith('/v1/')) return text
  if (isRawBase64(text)) return `data:${imageMimeFor('')};base64,${text}`
  if (!api) return null
  return api.content(text, false)
}
