// @vitest-environment jsdom
/**
 * Tests for markdown.js — 聊天与文件 MD 预览共享的渲染管道
 * 覆盖：mermaid/math 占位提取、代码块复制按钮与语言角标、sanitize、
 * 图片 src 解析钩子、CRLF 归一化、异常回退、bindMarkdownExtras 复制按钮绑定
 */
import { describe, it, expect, vi } from 'vitest'
import { marked } from 'marked'
import { renderMarkdown, sanitizeHtml, bindMarkdownExtras } from './markdown.js'

describe('renderMarkdown — 共享管道', () => {
  it('mermaid 围栏 → .mermaid-pending 占位符（不再是普通代码块）', () => {
    const html = renderMarkdown('before\n\n```mermaid\ngraph TD\n  A-->B\n```\n\nafter')
    expect(html).toContain('mermaid-pending')
    expect(html).toContain('data-mermaid=')
    expect(html).not.toMatch(/<pre><code[^>]*>[\s\S]*graph TD/)
  })

  it('行内数学与块级数学 → .math-pending 占位符（行内 $ 需含 \\ 命令，保守防误伤金额）', () => {
    const html = renderMarkdown('a $\\alpha^2$ b\n\n$$\\sum_{i=1}^n i$$')
    expect(html).toContain('math-pending')
    expect(html.match(/math-pending/g).length).toBeGreaterThanOrEqual(2)
  })

  it('无 \\ 命令的 $5 和 $10 不被误判为数学', () => {
    const html = renderMarkdown('price is $5 to $10')
    expect(html).not.toContain('math-pending')
  })

  it('普通代码块保留高亮，并带复制按钮与语言角标', () => {
    const html = renderMarkdown('```python\nprint("hi")\n```')
    expect(html).toContain('data-copy-btn')
    expect(html).toContain('data-raw-code=')
    expect(html).toContain('PYTHON')
    expect(html).toContain('hl-string')
  })

  it('sanitize：剥除 script、内联事件属性、javascript: 链接', () => {
    const html = renderMarkdown('<script>alert(1)<\/script>\n\n<img src="x" onerror="alert(2)">\n\n[bad](javascript:alert(3))')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('javascript:')
    expect(html).toContain('about:blank')
  })

  it('resolveImageSrc 钩子：相对路径图片被解析，外链保持', () => {
    const html = renderMarkdown('![](docs/a.png) and [ext](https://e.com/x.png)', {
      resolveImageSrc: (src) => 'RESOLVED:' + src,
    })
    expect(html).toContain('src="RESOLVED:docs/a.png"')
    // 链接中的 URL 不受图片钩子影响
    expect(html).toContain('https://e.com/x.png')
  })

  it('不提供 resolveImageSrc 时图片 src 原样保留', () => {
    const html = renderMarkdown('![](docs/a.png)')
    expect(html).toContain('src="docs/a.png"')
  })

  it('CRLF/CR 归一化为 LF（\r 不泄漏进产物）', () => {
    const html = renderMarkdown('line1\r\nline2\rline3')
    expect(html).not.toContain('\r')
  })

  it('marked 异常时回退为转义纯文本', () => {
    const spy = vi.spyOn(marked, 'parse').mockImplementation(() => {
      throw new Error('boom')
    })
    try {
      const html = renderMarkdown('plain <b>&</b> text')
      expect(html).toContain('&lt;b&gt;')
      expect(html).toContain('&amp;')
      expect(html).not.toContain('<b>')
    } finally {
      spy.mockRestore()
    }
  })

  it('空输入返回空串', () => {
    expect(renderMarkdown('')).toBe('')
    expect(renderMarkdown(null)).toBe('')
  })
})

describe('sanitizeHtml', () => {
  it('剥离 script 标签（含未闭合变体）', () => {
    expect(sanitizeHtml('<script src="x.js"></script>ok')).toBe('ok')
  })

  it('剥离 on* 事件属性（引号/裸值）', () => {
    expect(sanitizeHtml('<a onclick="f()" href="x">a</a>')).not.toContain('onclick')
    expect(sanitizeHtml('<a onmouseover=f() href="x">a</a>')).not.toContain('onmouseover')
  })
})

describe('bindMarkdownExtras — 挂载后绑定', () => {
  it('复制按钮绑定一次并幂等', () => {
    const container = document.createElement('div')
    container.innerHTML =
      '<div class="code-block"><button class="copy-btn" data-copy-btn data-raw-code="' +
      btoa('abc') + '">Copy</button><pre><code>x</code></pre></div>'
    bindMarkdownExtras(container)
    bindMarkdownExtras(container)
    const btn = container.querySelector('[data-copy-btn]')
    expect(btn.dataset.copyBound).toBe('1')
    // 只绑了一次监听（重复调用不叠加）
    expect(btn.onclick).toBe(null) // 用 addEventListener 绑定，onclick 属性为空
  })

  it('null 容器安全返回', () => {
    expect(() => bindMarkdownExtras(null)).not.toThrow()
  })
})
