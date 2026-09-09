/**
 * Markdown 数学公式提取 + KaTeX CDN 按需渲染（零打包成本）
 *
 * 设计对齐 mermaid.ink / pdfjs CDN 方案：
 * - extractMath(src)      : 在 marked 之前运行，提取数学片段（代码块 / 行内代码受保护），
 *                           替换为 <span class="math-pending" data-math="base64">源码</span> 占位符
 * - renderMathElements()  : DOM 就绪后从 jsDelivr 按需下载 katex.min.js + katex.min.css
 *                           （模块级缓存，会话内只下载一次），katex.render 渲染占位符；
 *                           下载失败或渲染失败时保留源码可见并加 ⚠（与 mermaid 行为一致）。
 *
 * 支持的定界符：
 *   $$...$$     块级（标准）
 *   \[...\]     块级
 *   [ ... ]     块级（LLM 输出习惯：独占一行，内容须含反斜杠命令；
 *               行尾 ] 后不能再跟 (，天然排除链接 [text](url)）
 *   \( ... \)   行内
 *   $...$       行内（保守启发式：内容须含反斜杠命令，避免 $100 金额误判）
 */

const KATEX_JS = 'https://cdn.jsdelivr.net/npm/katex@latest/dist/katex.min.js'
const KATEX_CSS = 'https://cdn.jsdelivr.net/npm/katex@latest/dist/katex.min.css'

let katexPromise = null
const renderCache = new Map() // (mode + 源码) -> 渲染后的 HTML

function b64encode(s) {
  return btoa(unescape(encodeURIComponent(s)))
}

function b64decode(s) {
  return decodeURIComponent(escape(atob(s)))
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function placeholder(src, display) {
  const cls = display ? 'math-pending math-display' : 'math-pending'
  return `<span class="${cls}" data-math="${b64encode(src)}">${escapeHtml(src)}</span>`
}

/**
 * 按行号切分 代码 / 非代码 片段（``` / ~~~ 围栏 + 4空格缩进代码块）。
 * 返回 {code, start, end} 行区间（含端点）——按区间替换可精确还原文本，
 * 不会丢失片段边界处的换行符。
 */
function splitCodeFences(lines) {
  const segs = []
  const isFence = (line) => line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/)
  const isIndented = (line) => /^ {4,}\S/.test(line) || /^\t/.test(line)
  let i = 0
  let plainStart = 0
  const flushPlain = (end) => {
    if (end >= plainStart) segs.push({ code: false, start: plainStart, end })
  }
  while (i < lines.length) {
    const fm = isFence(lines[i])
    if (fm) {
      // 围栏代码块（含未闭合：流式中）
      flushPlain(i - 1)
      const fenceChar = fm[1][0]
      const fenceLen = fm[1].length
      let j = i + 1
      while (j < lines.length) {
        const cj = isFence(lines[j])
        if (cj && cj[1][0] === fenceChar && cj[1].length >= fenceLen && cj[2].trim() === '') break
        j++
      }
      const end = Math.min(j, lines.length - 1)
      segs.push({ code: true, start: i, end })
      i = j < lines.length ? j + 1 : lines.length
      plainStart = i
      continue
    }
    if (isIndented(lines[i])) {
      // 缩进代码块：连续缩进行/空行，末尾空行归下一段
      flushPlain(i - 1)
      let j = i + 1
      while (j < lines.length && (isIndented(lines[j]) || lines[j].trim() === '')) j++
      let end = j - 1
      while (end > i && lines[end].trim() === '') end--
      segs.push({ code: true, start: i, end })
      i = end + 1
      plainStart = i
      continue
    }
    i++
  }
  flushPlain(lines.length - 1)
  return segs
}

/** 在"行内代码已掩码"的文本上按优先级提取数学片段 */
function extractFromMasked(text) {
  // 1) $$...$$ 块级
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, inner) => placeholder(inner.trim(), true))
  // 2) \[...\] 块级
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_, inner) => placeholder(inner.trim(), true))
  // 3) [ ... ] 独占一行块级（LLM 习惯）
  text = text.replace(/(^|\n)[ \t]*(\[[\s\S]+?\])[ \t]*(?=\n|$)/g, (m, lead, bracket) => {
    const inner = bracket.slice(1, -1)
    if (!/\\/.test(inner)) return m // 无 LaTeX 命令 → 普通括号文本
    return lead + placeholder(inner.trim(), true)
  })
  // 4) \(...\) 行内
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_, inner) => placeholder(inner.trim(), false))
  // 5) $...$ 行内（保守：须含反斜杠命令，且不能以空格/数字开头 → 排除金额）
  text = text.replace(/\$([^\n$]+?)\$/g, (m, inner) => {
    if (!/\\/.test(inner)) return m
    if (/^\s/.test(inner) || /\s$/.test(inner) || /^\d/.test(inner)) return m
    return placeholder(inner.trim(), false)
  })
  return text
}

/** 提取数学片段为占位符（导出供测试） */
export function extractMath(src) {
  if (!src) return src
  if (!src.includes('\\') && !src.includes('$') && !src.includes('[')) return src
  const lines = src.split('\n')
  const outLines = []
  for (const seg of splitCodeFences(lines)) {
    const slice = lines.slice(seg.start, seg.end + 1).join('\n')
    if (seg.code) {
      outLines.push(...slice.split('\n'))
      continue
    }
    // 先掩码行内代码（`...` / ``...``），防止解释 markdown 语法的消息被误提取
    const codes = []
    const masked = slice.replace(/(`{1,2})(?!`)([^\n]*?)\1(?!`)/g, (m) => {
      codes.push(m)
      return `\u0000C${codes.length - 1}\u0000`
    })
    let text = extractFromMasked(masked)
    text = text.replace(/\u0000C(\d+)\u0000/g, (_, i) => codes[+i])
    outLines.push(...text.split('\n'))
  }
  return outLines.join('\n')
}

function ensureKatexCss() {
  if (document.querySelector('link[data-katex-css]')) return
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = KATEX_CSS
  link.dataset.katexCss = '1'
  document.head.appendChild(link)
}

/** 按需加载 KaTeX（UMD → window.katex），模块级缓存，失败可重试 */
function loadKatex() {
  if (!katexPromise) {
    ensureKatexCss()
    katexPromise = new Promise((resolve, reject) => {
      if (window.katex) {
        resolve(window.katex)
        return
      }
      const script = document.createElement('script')
      script.src = KATEX_JS
      script.onload = () => (window.katex ? resolve(window.katex) : reject(new Error('katex global missing')))
      script.onerror = () => {
        script.remove()
        reject(new Error('Failed to load ' + KATEX_JS))
      }
      document.head.appendChild(script)
    }).catch((err) => {
      katexPromise = null // 允许下次重试
      throw err
    })
  }
  return katexPromise
}

/**
 * 渲染所有 math-pending 占位符（幂等：已处理元素跳过）。
 * 渲染结果按 (mode+源码) 缓存 —— 流式重渲染时 DOM 重建，同公式可同步回填、零闪烁。
 */
export async function renderMathElements(els) {
  let katex
  try {
    katex = await loadKatex()
  } catch {
    for (const el of els) {
      if (el.dataset.mathDone) continue
      el.dataset.mathDone = '1'
      el.classList.add('math-error')
    }
    return
  }
  for (const el of els) {
    if (el.dataset.mathDone) continue
    el.dataset.mathDone = '1'
    let src
    try {
      src = b64decode(el.dataset.math || '')
    } catch {
      el.classList.add('math-error')
      continue
    }
    const display = el.classList.contains('math-display')
    const key = (display ? 'D' : 'I') + '\u0000' + src
    let out = renderCache.get(key)
    if (out === undefined) {
      const tmp = document.createElement('span')
      try {
        katex.render(src, tmp, { displayMode: display, throwOnError: false, strict: 'ignore' })
      } catch {
        el.classList.add('math-error')
        continue
      }
      if (tmp.querySelector('.katex-error')) {
        el.classList.add('math-error') // 语法错误 → 保留源码 + ⚠
        continue
      }
      out = tmp.innerHTML
      if (renderCache.size > 500) renderCache.clear()
      renderCache.set(key, out)
    }
    if (!el.isConnected) continue // 流式重渲染导致节点被替换：缓存已就位，新节点命中缓存
    el.innerHTML = out
    el.classList.add('math-rendered')
  }
}
