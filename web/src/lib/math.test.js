/**
 * Tests for math.js — extractMath 数学片段提取
 * 覆盖：5 种定界符、代码保护、金额/链接/括号误判防护、围栏边界回归
 */
import { describe, it, expect } from 'vitest'
import { extractMath } from './math.js'

describe('extractMath — 定界符识别', () => {
  it('LLM [ ... ] 块级（用户示例公式）→ display 占位符', () => {
    const userFormula = '[\nd(x,\\mu_k)=\n\\left(\\sum_{j=1}^{784}|x_j-\\mu_{k,j}|^3\\right)^{1/3}\n]'
    const r = extractMath('前文\n\n' + userFormula + '\n\n后文')
    expect(r).toContain('math-pending math-display')
    expect(r).toContain('d(x,\\mu_k)=')
    expect(r).not.toContain('\n[\nd(x')
  })

  it('$$...$$ 块级', () => {
    const r = extractMath('能量 $$E = mc^2$$ 结束')
    expect(r).toContain('math-display')
    expect(r).toMatch(/data-math=/)
    expect(r).not.toContain('$$')
  })

  it('\\[...\\] 块级', () => {
    const r = extractMath('公式 \\[ \\frac{a}{b} \\] 结束')
    expect(r).toContain('math-display')
    expect(r).not.toContain('\\[')
  })

  it('\\(...\\) 行内', () => {
    const r = extractMath('内联 \\(x^2 + y^2\\) 文本')
    expect(r).toContain('math-pending"')
    expect(r).not.toContain('\\(')
  })

  it('$...$ 行内（含反斜杠命令）', () => {
    const r = extractMath('系数 $\\alpha + \\beta$ 与 $x^2$')
    expect(r).toContain('data-math=')
    expect(r).not.toContain('$\\alpha')
  })

  it('块内嵌套 [ ]（矩阵）取最外层', () => {
    const r = extractMath('[\n\\begin{bmatrix} a & b \\\\\\end{bmatrix}\n]')
    expect((r.match(/data-math=/g) || []).length).toBe(1)
  })

  it('占位符 base64 可解码还原', () => {
    const r = extractMath('$$x^2$$')
    const m = r.match(/data-math="([^"]+)"/)
    expect(decodeURIComponent(escape(atob(m[1])))).toBe('x^2')
  })
})

describe('extractMath — 误判防护', () => {
  it('金额 $100/$200 原样保留', () => {
    expect(extractMath('价格 $100 和 $200 之间')).toContain('$100 和 $200')
  })

  it('$ 5（前导空格）原样保留', () => {
    expect(extractMath('写 $ 5 这种')).toContain('$ 5')
  })

  it('[text](url) 链接原样保留', () => {
    expect(extractMath('看 [文档](https://example.com) 这里')).toContain('[文档](https://example.com)')
  })

  it('无 \\ 命令的 [ ] 原样保留', () => {
    expect(extractMath('备注 [注意: 这不是公式] 结束')).toContain('[注意: 这不是公式]')
  })

  it('无定界符文本 fast path 原样', () => {
    expect(extractMath('普通文本，没有公式')).toBe('普通文本，没有公式')
  })
})

describe('extractMath — 代码保护', () => {
  it('围栏代码块内数学原样', () => {
    const r = extractMath('```js\nconst p = "$100 and \\sum $$x$$"\n```')
    expect(r).toContain('$$x$$')
    expect(r).toContain('\\sum')
    expect(r).not.toContain('math-pending')
  })

  it('行内代码内数学原样', () => {
    const r = extractMath('用 `$$x^2$$` 和 `\\[a\\]` 写公式')
    expect(r).toContain('`$$x^2$$`')
    expect(r).toContain('`\\[a\\]`')
  })

  it('4空格缩进代码块原样', () => {
    expect(extractMath('    indented code $\\alpha$ here')).toContain('$\\alpha$ here')
  })

  it('围栏紧贴非空行：输出与输入一致（回归：不吞边界换行）', () => {
    const tight = '文本行\n```js\nconst x = "$\\alpha$$"\n```\n后续行'
    expect(extractMath(tight)).toBe(tight)
  })

  it('完整混合文档：非数学行原样、边界完整', () => {
    const doc = [
      '## 标题',
      '',
      '1. 块级 ' + String.raw`$$E = mc^2$$` + ' 结束',
      '',
      '金额 $100 和 $200 不应渲染',
      '',
      '代码块保护:',
      '```js',
      'const p = "$100 and \\sum $$x$$"',
      '```',
      '',
      '行内代码 ' + '`' + String.raw`$$x^2$$` + '`' + ' 保持字面',
      '',
      '[文档](https://example.com)',
      '',
      '[注意: 这不是公式]',
    ].join('\n')
    const outLines = extractMath(doc).split('\n')
    const origLines = doc.split('\n')
    expect(outLines).toContain(origLines[4])   // 金额行
    expect(outLines).toContain(origLines[8])   // 代码行
    expect(outLines).toContain(origLines[13])  // 链接行
    const fenceIdx = outLines.indexOf('```js')
    expect(fenceIdx).toBeGreaterThan(0)
    expect(outLines[fenceIdx - 1]).toBe(origLines[6]) // 围栏前一行独立成行
    expect(outLines[fenceIdx + 2]).toBe('```')
    expect(outLines[fenceIdx + 3]).toBe('')
  })
})
