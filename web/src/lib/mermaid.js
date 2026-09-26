/**
 * Markdown mermaid 图表提取 + mermaid UMD CDN 按需本地渲染
 *
 * 设计对齐 math.js 的 CDN 按需方案（此前用 mermaid.ink 远程渲染，
 * 浏览器 fetch 被 CORS 拦截——mermaid.ink 不返回 Access-Control-Allow-Origin，
 * 且依赖外部服务，故改为本地渲染，零打包成本）：
 * - extractMermaidFences(src)   : 在 marked 之前运行，保护 ```mermaid / ~~~mermaid
 *                                 围栏代码块 → <span class="mermaid-pending" data-mermaid="base64">源码</span>
 *                                 占位符（span 内联，与 KaTeX 一致：避免 div 在 <p> 内无效嵌套）
 * - renderMermaidElements(els)  : DOM 就绪后从 jsDelivr 按需下载 mermaid.min.js（UMD，
 *                                 模块级缓存，会话内只下载一次），mermaid.render 渲染占位符；
 *                                 下载失败或语法错误时保留源码可见并加 ⚠（.mermaid-error）。
 *
 * 仅处理已闭合的围栏（流式未闭合时交给 marked 按普通代码块展示，闭合后自然升级渲染）。
 */

const MERMAID_JS = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js'

let mermaidPromise = null
let renderId = 0
const renderCache = new Map() // 源码 -> SVG 字符串

function b64encode(s) {
  return btoa(unescape(encodeURIComponent(s)))
}

function b64decode(s) {
  return decodeURIComponent(escape(atob(s)))
}

/**
 * 容错预处理：subgraph 标题含未加引号的括号/尖括号等特殊字符时自动补引号。
 * LLM 高频踩坑——mermaid 把 ( ) 当作"体育场节点"形状界定符，
 * 如 `subgraph G1 [图片转视频 (逐张)]` 会 Parse error；
 * 补引号 `subgraph G1 ["图片转视频 (逐张)"]` 即可正常解析。已含引号的标题不动。
 */
export function normalizeMermaidSource(code) {
  if (!code) return code
  return code.replace(
    /^([ \t]*subgraph[ \t]+(?:\S+[ \t]+)?)\[([^\]]*)\](?=[ \t]*$)/gm,
    (m, pre, title) => {
      if (/["']/.test(title)) return m // 已有引号
      if (/[(<>/|;)]/.test(title)) return `${pre}["${title.replace(/"/g, '\\"')}"]`
      return m
    },
  )
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/**
 * 提取已闭合的 mermaid 围栏代码块为占位符（导出供测试）。
 * 未闭合围栏、其他语言的代码块原样保留。
 */
export function extractMermaidFences(src) {
  if (!src) return src
  if (!src.includes('mermaid')) return src
  return src.replace(
    /(^|\n) {0,3}(`{3,}|~{3,})[ \t]*mermaid[ \t]*\n([\s\S]*?)\n {0,3}\2[ \t]*(?=\n|$)/g,
    (_, lead, _fence, body) => {
      // 内联文本仅供失败兜底展示；真实源码存于 data-mermaid（base64，见 renderMermaidElements）。
      // 关键：必须把内联里的真实换行替换为 <br/>。若保留 \n，占位符会落入 <p>，
      // 而 mermaid 源码里「空行 + 4 空格缩进行」会被 marked 误判为 markdown 缩进代码块，
      // 额外生成一个 <pre><code>——导致「图已渲染成功，却又在图下方吐出一段转义源码」
      // （源码里的 -- > / <br/> 等被标记为 &amp;gt; / &lt;br/&gt;）。换行转 <br/> 后不再有
      // 真实 \n，marked 视其为纯内联内容，不再拆出代码块；失败兜底仍可读（<br/> 即换行）。
      const inline = escapeHtml(body).replace(/\r?\n/g, '<br/>')
      return `${lead}<span class="mermaid-pending" data-mermaid="${b64encode(body)}">${inline}</span>`
    },
  )
}

/** 按需加载 mermaid（UMD → window.mermaid），模块级缓存，失败可重试 */
function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = new Promise((resolve, reject) => {
      if (window.mermaid) {
        resolve(window.mermaid)
        return
      }
      const script = document.createElement('script')
      script.src = MERMAID_JS
      script.onload = () => (window.mermaid ? resolve(window.mermaid) : reject(new Error('mermaid global missing')))
      script.onerror = () => {
        script.remove()
        reject(new Error('Failed to load ' + MERMAID_JS))
      }
      document.head.appendChild(script)
    }).catch((err) => {
      mermaidPromise = null // 允许下次重试
      throw err
    })
  }
  return mermaidPromise
}

// ---------------------------------------------------------------------------
// 工具栏：放大 / 缩小 + 导出 SVG / PNG（全部浏览器原生 API，零依赖）
// ---------------------------------------------------------------------------

// 从 svg 的 viewBox 取自然尺寸（mermaid 的 svg 用 width="100%" + viewBox）
function viewBoxSize(svg) {
  const vb = svg.viewBox && svg.viewBox.baseVal
  if (vb && vb.width > 0 && vb.height > 0) return { w: vb.width, h: vb.height }
  const w = parseFloat(svg.getAttribute('width')) || 800
  const h = parseFloat(svg.getAttribute('height')) || 600
  return { w, h }
}

// 克隆一份带固定像素宽高 + xmlns 的独立 svg（脱离 DOM 也能自包含渲染）。
// 导出供测试：保证"下载 SVG/PNG"用的 SVG 一定自包含（含 xmlns、固定像素宽高、无外部资源）。
export function standaloneSvg(svg, w, h) {
  const c = svg.cloneNode(true)
  c.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  c.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink')
  c.setAttribute('width', String(Math.round(w)))
  c.setAttribute('height', String(Math.round(h)))
  return c
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

// 导出矢量 SVG（无损，可任意放大；尺寸取自然 1x）
function downloadSvg(svg, base) {
  const { w, h } = viewBoxSize(svg)
  const xml = new XMLSerializer().serializeToString(standaloneSvg(svg, w, h))
  const str = `<?xml version="1.0" encoding="UTF-8"?>\n` + xml
  saveBlob(new Blob([str], { type: 'image/svg+xml;charset=utf-8' }), `${base}.svg`)
}

// 导出 PNG（位图，方便直接发群/贴文档；放大到 2x 更清晰，且限制在 canvas 上限内）
function downloadPng(svg, base) {
  const MAX_DIM = 16384 // 多数浏览器 canvas 单边上限
  const { w, h } = viewBoxSize(svg)
  let scale = 2
  const m = Math.max(w, h)
  if (m * scale > MAX_DIM) scale = MAX_DIM / m // 超高/超宽图退到合适倍率
  if (scale < 1) scale = 1
  const W = Math.round(w * scale)
  const H = Math.round(h * scale)
  const xml = new XMLSerializer().serializeToString(standaloneSvg(svg, W, H))
  // 关键：必须用 data: URL 而非 blob: URL 喂给 <img> 再 drawImage。
  // blob: 的 SVG 被浏览器视为外部文档 → canvas 被"污染"(tainted) → toBlob 抛 SecurityError。
  // data: URL 内联、同源，不污染 canvas，toBlob 正常。
  const dataUrl = 'data:image/svg+xml;charset=utf-8;base64,' + b64encode(xml)
  const img = new Image()
  img.onload = () => {
    try {
      const canvas = document.createElement('canvas')
      canvas.width = W
      canvas.height = H
      const ctx = canvas.getContext('2d')
      ctx.fillStyle = '#ffffff'
      ctx.fillRect(0, 0, W, H)
      ctx.drawImage(img, 0, 0, W, H)
      canvas.toBlob((blob) => {
        if (blob) saveBlob(blob, `${base}.png`)
      }, 'image/png')
    } catch {
      /* 忽略：导出失败不阻断页面 */
    }
  }
  img.onerror = () => {}
  img.src = dataUrl
}

// 给渲染成功的占位符（span）加上工具栏。DOM 用 createElement 构建，避免转义/XSS 隐患。
function attachMermaidToolbar(span, opts) {
  const svg = span.querySelector('svg')
  if (!svg || span.querySelector(':scope > .mermaid-toolbar')) return
  const labels = (opts && opts.labels) || {}
  const bar = document.createElement('div')
  bar.className = 'mermaid-toolbar'
  span.insertBefore(bar, svg)

  const factor = { v: 1 }
  const applyZoom = () => {
    // 内联样式优先于 .mermaid-rendered svg{max-width:100%}，factor>1 时超出容器宽 → 横向滚动
    svg.style.width = `${factor.v * 100}%`
    svg.style.maxWidth = factor.v > 1 ? 'none' : '100%'
  }
  const btn = (label, title, handler) => {
    if (!label) return
    const b = document.createElement('button')
    b.type = 'button'
    b.className = 'mermaid-tbtn'
    b.textContent = label
    b.title = title || label
    b.addEventListener('click', (e) => {
      e.preventDefault()
      handler()
    })
    bar.appendChild(b)
  }
  btn(labels.zoomIn, labels.zoomIn, () => {
    factor.v = Math.min(4, factor.v * 1.25)
    applyZoom()
  })
  btn(labels.zoomOut, labels.zoomOut, () => {
    factor.v = Math.max(0.25, factor.v / 1.25)
    applyZoom()
  })
  btn(labels.downloadSvg, labels.downloadSvg, () => downloadSvg(svg, 'mermaid-diagram'))
  btn(labels.downloadPng, labels.downloadPng, () => downloadPng(svg, 'mermaid-diagram'))
}

/**
 * 渲染所有 mermaid-pending 占位符（幂等：已处理元素跳过）。
 * 渲染结果按源码缓存 —— 流式重渲染时 DOM 重建，同图可同步回填、零闪烁。
 * opts.labels 可选：{ zoomIn, zoomOut, downloadSvg, downloadPng } 工具栏文案（由 i18n 提供）。
 */
export async function renderMermaidElements(els, opts) {
  let mermaid
  try {
    mermaid = await loadMermaid()
  } catch {
    for (const el of els) {
      if (el.dataset.mermaidDone) continue
      el.dataset.mermaidDone = '1'
      el.classList.add('mermaid-error')
    }
    return
  }
  mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' })
  for (const el of els) {
    if (el.dataset.mermaidDone) continue
    el.dataset.mermaidDone = '1'
    let src
    try {
      src = b64decode(el.dataset.mermaid || '')
    } catch {
      el.classList.add('mermaid-error')
      continue
    }
    // 容错：subgraph 标题特殊字符自动补引号（见 normalizeMermaidSource）
    const norm = normalizeMermaidSource(src)
    let out = renderCache.get(norm)
    if (out === undefined) {
      const id = `mm-render-${renderId++}`
      let svgStr = ''
      try {
        const res = await mermaid.render(id, norm)
        // v10 返回 string；v11 返回 { svg, bindFunctions? } —— 两种都取 SVG 字符串
        if (typeof res === 'string') svgStr = res
        else if (res && typeof res.svg === 'string') svgStr = res.svg
        // bindFunctions 存在时调用以绑定交互（如 flowchart 点击）；调用后仍需移除临时节点
        if (res && typeof res.bindFunctions === 'function') res.bindFunctions()
      } catch {
        // 语法错误：清理 mermaid 生成的临时错误节点
        document.getElementById(id)?.remove()
        el.classList.add('mermaid-error') // 保留源码 + ⚠
        continue
      }
      // mermaid.render 会把 <div id=id> 临时插入 body，渲染后必须移除
      document.getElementById(id)?.remove()
      if (!svgStr || !svgStr.includes('<svg')) {
        el.classList.add('mermaid-error')
        continue
      }
      out = svgStr
      if (renderCache.size > 100) renderCache.clear()
      renderCache.set(src, out)
    }
    if (!el.isConnected) continue // 流式重渲染导致节点被替换：缓存已就位，新节点命中缓存
    el.innerHTML = out
    el.classList.add('mermaid-rendered')
    attachMermaidToolbar(el, opts) // 放大 / 导出工具栏（attach 内有 :scope 去重，幂等）
  }
}
