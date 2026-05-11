const ROOT_SELECTOR = 'pre, code'
const PREVIEW_SELECTOR = '.hl-json-string-preview'
const MAX_PREVIEW_CHARS = 240
const MIN_PREVIEW_CHARS = 24
const RESERVED_PX = 32

function getTextWidth(text, font) {
  const canvas = getTextWidth.canvas || (getTextWidth.canvas = document.createElement('canvas'))
  const ctx = canvas.getContext('2d')
  ctx.font = font
  return ctx.measureText(text).width
}

function findCodeRoot(el) {
  return el.closest(ROOT_SELECTOR) || el.parentElement
}

function getAvailableWidth(preview, root) {
  const rootRect = root.getBoundingClientRect()
  const previewRect = preview.getBoundingClientRect()
  const usedBeforePreview = Math.max(0, previewRect.left - rootRect.left)
  return Math.max(0, root.clientWidth - usedBeforePreview - RESERVED_PX)
}

function fitPreview(preview) {
  const raw = preview.dataset.rawJsonPreview
  if (!raw) return

  const root = findCodeRoot(preview)
  if (!root) return

  const style = getComputedStyle(preview)
  const font = style.font || `${style.fontSize} ${style.fontFamily}`
  const available = getAvailableWidth(preview, root)
  if (available <= 0) return

  let low = MIN_PREVIEW_CHARS
  let high = Math.min(MAX_PREVIEW_CHARS, raw.length)
  let best = Math.min(low, high)

  while (low <= high) {
    const mid = Math.floor((low + high) / 2)
    const candidate = raw.slice(0, mid) + '…"'
    if (getTextWidth(candidate, font) <= available) {
      best = mid
      low = mid + 1
    } else {
      high = mid - 1
    }
  }

  preview.textContent = raw.slice(0, best) + '…"'
}

function fitAll(root = document) {
  root.querySelectorAll(PREVIEW_SELECTOR).forEach(fitPreview)
}

export function installJsonPreviewAutoFit() {
  if (typeof window === 'undefined' || typeof document === 'undefined') return
  if (window.__jsonPreviewAutoFitInstalled) return
  window.__jsonPreviewAutoFitInstalled = true

  let scheduled = false
  const schedule = (root = document) => {
    if (scheduled) return
    scheduled = true
    requestAnimationFrame(() => {
      scheduled = false
      fitAll(root)
    })
  }

  schedule()

  const mutationObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue
        if (node.matches?.(PREVIEW_SELECTOR) || node.querySelector?.(PREVIEW_SELECTOR)) {
          schedule()
          return
        }
      }
    }
  })
  mutationObserver.observe(document.body, { childList: true, subtree: true })

  if (typeof ResizeObserver !== 'undefined') {
    const resizeObserver = new ResizeObserver(() => schedule())
    resizeObserver.observe(document.body)
  } else {
    window.addEventListener('resize', () => schedule())
  }
}
