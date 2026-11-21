// @vitest-environment jsdom
/**
 * Tests for mermaid.js — extractMermaidFences mermaid 围栏提取
 * 覆盖：闭合围栏提取、未闭合（流式中）不提取、其他语言代码块不受影响、占位符内容转义
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect } from 'vitest'
import { extractMermaidFences, normalizeMermaidSource, standaloneSvg } from './mermaid.js'

describe('normalizeMermaidSource — subgraph 标题特殊字符容错', () => {
  it('含未引号括号的 subgraph 标题 → 自动补引号（真实踩坑数据）', () => {
    const r = normalizeMermaidSource('subgraph G1 [图片转视频 (逐张)]')
    expect(r).toBe('subgraph G1 ["图片转视频 (逐张)"]')
  })

  it('纯中文无特殊字符的标题 → 保持不变', () => {
    const r = normalizeMermaidSource('subgraph C1 [缓存命中分支]')
    expect(r).toBe('subgraph C1 [缓存命中分支]')
  })

  it('已有引号的标题 → 保持不变', () => {
    const r = normalizeMermaidSource('subgraph G ["x (y)"]')
    expect(r).toBe('subgraph G ["x (y)"]')
  })

  it('含尖括号的标题 → 补引号', () => {
    const r = normalizeMermaidSource('subgraph S [a < b > c]')
    expect(r).toBe('subgraph S ["a < b > c"]')
  })

  it('无 subgraph 的图 → 原样返回', () => {
    const src = 'flowchart TD\n  A-->B'
    expect(normalizeMermaidSource(src)).toBe(src)
  })
})

describe('extractMermaidFences — 围栏识别', () => {
  it('```mermaid 闭合围栏 → 占位符', () => {
    const r = extractMermaidFences('前言\n\n```mermaid\nflowchart TD\n  A-->B\n```\n\n后记')
    expect(r).toContain('mermaid-pending')
    expect(r).toMatch(/data-mermaid="/)
    expect(r).toContain('flowchart TD')
    expect(r).not.toContain('```mermaid')
  })

  it('~~~mermaid 波浪围栏 → 占位符', () => {
    const r = extractMermaidFences('~~~mermaid\nsequenceDiagram\nA->>B\n~~~')
    expect(r).toContain('mermaid-pending')
  })

  it('未闭合围栏（流式中）→ 原样保留，交给 marked 按代码块展示', () => {
    const src = '```mermaid\nflowchart TD\n  A--'
    expect(extractMermaidFences(src)).toBe(src)
  })

  it('其他语言代码块不受影响', () => {
    const src = '```js\nconst x = 1\n```\n```python\nprint(1)\n```'
    expect(extractMermaidFences(src)).toBe(src)
  })

  it('含尖括号/中文的图内容 → 占位符内转义且 base64 可还原', () => {
    const body = 'flowchart TD\n  A[开始]-->|是|B["结束 <end>"]'
    const r = extractMermaidFences('```mermaid\n' + body + '\n```')
    expect(r).toContain('&lt;end&gt;')
    const m = r.match(/data-mermaid="([A-Za-z0-9+/=]+)"/)
    expect(m).not.toBeNull()
    expect(decodeURIComponent(escape(atob(m[1])))).toBe(body)
  })

  it('无 mermaid 字样的文本 → 原样返回', () => {
    const src = '普通文本\n```js\nalert(1)\n```'
    expect(extractMermaidFences(src)).toBe(src)
  })

  it('占位符内联文本不得含真实换行（防 marked 把它误判为缩进代码块）', () => {
    // body 含「空行 + 4 空格缩进行」——正是触发 marked 缩进代码块误判的特征
    const body = 'flowchart TD\n    A["x"] --> B{"y"}\n\n    MKDIR --> CACHE{"z"}\n    subgraph C1 [t]\n    end'
    const r = extractMermaidFences('前文\n\n```mermaid\n' + body + '\n```\n\n后记')
    const m = r.match(/<span class="mermaid-pending" data-mermaid="([A-Za-z0-9+/=]+)">(.*?)<\/span>/s)
    expect(m).not.toBeNull()
    // 内联文本不含真实换行（换行已被转成 <br/>）
    expect(m[2]).not.toMatch(/\n/)
    // 但仍可读：保留转义源码，失败兜底展示不丢
    expect(m[2]).toContain('flowchart TD')
    expect(m[2]).toContain('subgraph C1')
    expect(m[2]).toContain('<br/>')
  })

  it('经 marked 渲染后不得把 mermaid 源码拆出 <pre><code> 代码块（真实踩坑回归）', async () => {
    const { marked } = await import('marked')
    const body = 'flowchart TD\n    START([开始]) --> ARGS["解析命令行参数"]\n    ARGS --> MKDIR["创建目录"]\n\n    MKDIR --> CACHE{"--cache 含 r?"}\n    CACHE -->|"缓存命中"| LOADCACHE\n\n    subgraph C1 [缓存命中分支]\n        LOADCACHE["读取 .cache"] --> MIN3C{"素材 < 3 张?"}\n    end\n    MIN3C -->|"是"| EXIT2([任务中止])'
    const extracted = extractMermaidFences('前文\n\n```mermaid\n' + body + '\n```\n\n后记')
    const html = marked.parse(extracted, { gfm: true, breaks: true })
    // 之前 bug：html 里同时出现 <pre><code>MKDIR --> CACHE... 的伪代码块。现在必须为 0。
    expect(html).not.toMatch(/<pre[\s\S]*<code/)
    // 占位符仍在，等待 mermaid 渲染
    expect(html).toContain('mermaid-pending')
  })
})

describe('standaloneSvg — 导出用自包含 SVG（下载 SVG/PNG 前置）', () => {
  const makeSvg = () => {
    const NS = 'http://www.w3.org/2000/svg'
    const svg = document.createElementNS(NS, 'svg')
    svg.setAttribute('viewBox', '0 0 100 50')
    svg.setAttribute('width', '100%')
    const g = document.createElementNS(NS, 'g')
    g.setAttribute('id', 'diagram')
    const rect = document.createElementNS(NS, 'rect')
    rect.setAttribute('x', '0')
    rect.setAttribute('y', '0')
    svg.appendChild(g)
    g.appendChild(rect)
    return svg
  }

  it('补上 xmlns 与 xmlns:xlink（脱离 DOM 也能渲染）', () => {
    const out = standaloneSvg(makeSvg(), 200, 100)
    expect(out.getAttribute('xmlns')).toBe('http://www.w3.org/2000/svg')
    expect(out.getAttribute('xmlns:xlink')).toBe('http://www.w3.org/1999/xlink')
  })

  it('width/height 写成取整像素（不再是 100%），供 canvas 光栅化', () => {
    const out = standaloneSvg(makeSvg(), 2325.4, 16384.7)
    expect(out.getAttribute('width')).toBe('2325')
    expect(out.getAttribute('height')).toBe('16385')
    expect(out.getAttribute('width')).not.toBe('100%')
  })

  it('深克隆内容（含子节点）且不改动原节点', () => {
    const src = makeSvg()
    const out = standaloneSvg(src, 200, 100)
    // 克隆：不是同一引用，内容等价
    expect(out).not.toBe(src)
    expect(out.querySelector('g#diagram rect')).not.toBeNull()
    // 原节点 width 仍是 100%（未被污染）
    expect(src.getAttribute('width')).toBe('100%')
    expect(src.getAttribute('xmlns')).toBeNull()
  })
})

describe('mermaid 不得给整页加出滚动条 —— markdown.css 里的 pin 守卫', () => {
  // 真实踩坑：mermaid 的 insertTooltip() 把 <div class="mermaidTooltip"> append 到 <body>，
  // 并且永不删除。它拿到 position:absolute（mermaid 自己写在行内样式上）却没有 top/left，
  // 于是停在静止位置 —— 也就是 </body> 之前、整页底下几个像素。界面本来是 100vh 刚好不出
  // 滚动条，这 6px 让 document 多出滚动区：打开过含 mermaid 图的会话之后，窗口右侧多出一条
  // 上下滚动条，切会话、切 Setup 都不消失，只有刷新才带走（刷新把这条 div 一起带走）。
  // 防线只有 markdown.css 里那条 `body > .mermaidTooltip { position: fixed !important }`
  // —— fixed 元素不参与文档的滚动区域，而本页文档本身不滚动（滚动都在 .message-list 里），
  // mermaid 悬停时写的行内 top/left 在视口坐标下等价，提示框位置不受影响。
  // 删掉它就复发，所以在这里钉住。
  const css = readFileSync(join(__dirname, 'markdown.css'), 'utf8')

  it('body 直下的 .mermaidTooltip 被钉到视口（fixed，且带 !important 才盖得住行内样式）', () => {
    const rule = css.match(/body\s*>\s*\.mermaidTooltip\s*\{([^}]*)\}/)
    expect(rule, 'markdown.css 需要保留 body > .mermaidTooltip 规则').toBeTruthy()
    expect(rule[1]).toMatch(/position:\s*fixed\s*!important/)
  })
})
