import { describe, it, expect } from 'vitest'

/**
 * 路径计算的纯逻辑测试
 * 纯函数已移入 WorkspaceFileManager.svelte 的 relativeWorkspacePath
 * 此处独立验证相同逻辑
 */

function normalizePathForCompare(path) {
  return String(path || '').replace(/\\/g, '/').replace(/\/$/, '')
}

function relativePathWithTraversal(targetPath, basePath) {
  const normalizedTarget = normalizePathForCompare(targetPath)
  const normalizedBase = normalizePathForCompare(basePath)

  if (normalizedTarget === normalizedBase) return '.'
  if (normalizedTarget.startsWith(normalizedBase + '/')) {
    return normalizedTarget.slice(normalizedBase.length + 1)
  }

  const targetParts = normalizedTarget.split('/').filter(Boolean)
  const baseParts = normalizedBase.split('/').filter(Boolean)

  let commonLength = 0
  while (
    commonLength < targetParts.length &&
    commonLength < baseParts.length &&
    targetParts[commonLength] === baseParts[commonLength]
  ) {
    commonLength++
  }

  const upCount = baseParts.length - commonLength
  const upPath = '../'.repeat(upCount)
  const remainingPath = targetParts.slice(commonLength).join('/')

  return upCount > 0 ? (upPath + remainingPath).replace(/\/$/, '') || '..' : remainingPath || '.'
}

describe('relativeWorkspacePath', () => {
  // Basic relative path within workspace
  it('returns relative path for file inside workspace', () => {
    expect(relativePathWithTraversal('/home/user/workspace/src/main.js', '/home/user/workspace'))
      .toBe('src/main.js')
  })

  // Same path returns '.'
  it('returns "." when target equals base', () => {
    expect(relativePathWithTraversal('/home/user/workspace', '/home/user/workspace'))
      .toBe('.')
  })

  // Nested deeply
  it('returns nested relative path', () => {
    expect(relativePathWithTraversal('/home/user/workspace/a/b/c/file.txt', '/home/user/workspace'))
      .toBe('a/b/c/file.txt')
  })

  // File outside workspace uses ../
  it('returns ../ path for file outside workspace', () => {
    expect(relativePathWithTraversal('/home/user/other/file.txt', '/home/user/workspace'))
      .toBe('../other/file.txt')
  })

  // Completely different paths
  it('returns full relative path for distant paths', () => {
    expect(relativePathWithTraversal('/var/data/file.txt', '/home/user/workspace'))
      .toBe('../../../var/data/file.txt')
  })

  // Windows-style paths
  it('handles Windows-style backslash paths', () => {
    expect(relativePathWithTraversal('C:\\Users\\user\\workspace\\src\\main.js', 'C:\\Users\\user\\workspace'))
      .toBe('src/main.js')
  })

  // Windows outside workspace
  it('handles Windows paths outside workspace (different drives)', () => {
    expect(relativePathWithTraversal('D:\\other\\file.txt', 'C:\\Users\\user\\workspace'))
      .toBe('../../../../D:/other/file.txt')
  })

  // Edge: empty basePath (not valid in real usage, but shouldn't crash)
  it('returns normalized target if basePath is empty', () => {
    expect(relativePathWithTraversal('/some/path', '')).toBe('some/path')
  })

  // Edge: empty targetPath
  it('returns .. if targetPath is empty', () => {
    expect(relativePathWithTraversal('', '/workspace')).toBe('..')
  })

  // Path with trailing slashes
  it('normalizes trailing slashes', () => {
    expect(relativePathWithTraversal('/home/user/workspace/file.txt', '/home/user/workspace/'))
      .toBe('file.txt')
  })

  // Sibling directory
  it('handles sibling directory with ../', () => {
    expect(relativePathWithTraversal('/home/user/workspace-sibling/file.txt', '/home/user/workspace'))
      .toBe('../workspace-sibling/file.txt')
  })
})
