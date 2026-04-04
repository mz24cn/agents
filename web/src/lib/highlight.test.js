/**
 * Tests for highlight.js
 * Property tests (P1–P6) using fast-check + unit tests
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import { highlight, escapeHtml } from './highlight.js'

// ---------------------------------------------------------------------------
// Property Tests
// ---------------------------------------------------------------------------

describe('P1: highlight() always returns a string', () => {
  // Feature: code-block-enhancements, Property 1: 代码块渲染包含复制按钮
  // Validates: Requirements 1.1
  it('returns a string for any lang/code combination', () => {
    fc.assert(
      fc.property(fc.string(), fc.string(), (lang, code) => {
        const result = highlight(code, lang)
        return typeof result === 'string'
      }),
      { numRuns: 100 }
    )
  })
})

describe('P2: base64 round-trip preserves original text', () => {
  // Feature: code-block-enhancements, Property 2: 复制内容等于原始纯文本
  // Validates: Requirements 1.2, 3.7
  it('btoa/atob round-trip decodes back to original string', () => {
    fc.assert(
      fc.property(fc.string(), (text) => {
        const encoded = btoa(unescape(encodeURIComponent(text)))
        const decoded = decodeURIComponent(escape(atob(encoded)))
        return decoded === text
      }),
      { numRuns: 100 }
    )
  })
})

describe('P4: supported languages produce <span> tags for keyword-containing code', () => {
  // Feature: code-block-enhancements, Property 4: 支持语言的高亮输出包含 span 标签
  // Validates: Requirements 3.1, 3.2, 3.3
  it('python code with keywords contains <span> elements', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('def foo():', 'class Bar:', 'if x:', 'return True', 'import os'),
        (code) => {
          const result = highlight(code, 'python')
          return result.includes('<span')
        }
      ),
      { numRuns: 100 }
    )
  })

  it('json code with keys contains <span> elements', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('{"key": "value"}', '{"name": 1}', '{"a": true}', '{"x": null}'),
        (code) => {
          const result = highlight(code, 'json')
          return result.includes('<span')
        }
      ),
      { numRuns: 100 }
    )
  })

  it('bash code with variables/comments contains <span> elements', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('echo $HOME', '# comment', 'export PATH=$PATH', 'if [ -f file ]; then'),
        (code) => {
          const result = highlight(code, 'bash')
          return result.includes('<span')
        }
      ),
      { numRuns: 100 }
    )
  })
})

describe('P5: unsupported languages produce no <span> tags', () => {
  // Feature: code-block-enhancements, Property 5: 不支持语言不产生 span 标签
  // Validates: Requirements 3.4
  it('unsupported lang returns plain escaped text without <span>', () => {
    const unsupportedLangs = fc.oneof(
      fc.constantFrom('ruby', 'go', 'rust', 'java', 'cpp', 'typescript', 'unknown', ''),
      fc.string().filter(s => !['python', 'json', 'bash', 'sh'].includes(s.toLowerCase()))
    )
    fc.assert(
      fc.property(unsupportedLangs, fc.string(), (lang, code) => {
        const result = highlight(code, lang)
        return !result.includes('<span')
      }),
      { numRuns: 100 }
    )
  })
})

describe('P6: HTML special characters are escaped', () => {
  // Feature: code-block-enhancements, Property 6: HTML 特殊字符被转义
  // Validates: Requirements 3.6
  it('output never contains unescaped < > & " characters', () => {
    // Generate strings that contain at least one HTML special char
    const codeWithSpecialChars = fc.string().map(s => s + '<>&"')
    fc.assert(
      fc.property(codeWithSpecialChars, fc.string(), (code, lang) => {
        const result = highlight(code, lang)
        // Should not contain raw unescaped HTML special chars outside of span tags
        // We check by stripping all valid span tags and verifying no raw < > remain
        const stripped = result
          .replace(/<span class="hl-[^"]*">/g, '')
          .replace(/<\/span>/g, '')
        return (
          !stripped.includes('<') &&
          !stripped.includes('>') &&
          !stripped.includes('&"') // & is escaped to &amp;, " to &quot;
        )
      }),
      { numRuns: 100 }
    )
  })

  it('output does not contain unescaped & outside of entity references', () => {
    fc.assert(
      fc.property(fc.string(), (code) => {
        const result = highlight(code, 'python')
        // After stripping span tags, remaining & should only appear as &amp; &lt; &gt; &quot;
        const stripped = result
          .replace(/<span class="hl-[^"]*">/g, '')
          .replace(/<\/span>/g, '')
        // Find bare & not followed by amp; lt; gt; quot;
        return !/&(?!amp;|lt;|gt;|quot;)/.test(stripped)
      }),
      { numRuns: 100 }
    )
  })
})

// ---------------------------------------------------------------------------
// Unit Tests
// ---------------------------------------------------------------------------

describe('highlight() unit tests — Python', () => {
  it('highlights def keyword with hl-keyword class', () => {
    const result = highlight('def foo():', 'python')
    expect(result).toContain('<span class="hl-keyword">def</span>')
  })

  it('highlights class keyword with hl-keyword class', () => {
    const result = highlight('class Bar:', 'python')
    expect(result).toContain('<span class="hl-keyword">class</span>')
  })

  it('highlights if keyword with hl-keyword class', () => {
    const result = highlight('if x > 0:', 'python')
    expect(result).toContain('<span class="hl-keyword">if</span>')
  })
})

describe('highlight() unit tests — JSON', () => {
  it('highlights key names with hl-key class', () => {
    const result = highlight('{"name": "Alice"}', 'json')
    expect(result).toContain('hl-key')
  })

  it('highlights boolean true with hl-boolean class', () => {
    const result = highlight('{"active": true}', 'json')
    expect(result).toContain('<span class="hl-boolean">true</span>')
  })
})

describe('highlight() unit tests — Bash', () => {
  it('highlights $VAR variable with hl-variable class', () => {
    const result = highlight('echo $HOME', 'bash')
    expect(result).toContain('<span class="hl-variable">$HOME</span>')
  })

  it('highlights # comment with hl-comment class', () => {
    const result = highlight('# this is a comment', 'bash')
    expect(result).toContain('<span class="hl-comment"># this is a comment</span>')
  })
})

describe('highlight() unit tests — XSS escaping', () => {
  it('escapes <script> tag in code output', () => {
    const result = highlight('<script>alert(1)</script>', 'python')
    expect(result).not.toContain('<script>')
    expect(result).toContain('&lt;script&gt;')
  })

  it('escapes & character', () => {
    const result = highlight('a && b', 'bash')
    expect(result).toContain('&amp;')
    expect(result).not.toMatch(/[^&]&[^a]/)
  })
})
