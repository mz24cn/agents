/**
 * 剪贴板工具函数
 * 提供可靠的复制文本到剪贴板功能，兼容非HTTPS环境
 */

/**
 * 复制文本到剪贴板
 * 优先使用 navigator.clipboard API，如果不支持则使用 fallback 方法
 * @param {string} text - 要复制的文本
 * @returns {Promise<boolean>} - 复制是否成功
 */
export async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch (err) {
      // 失败时尝试 fallback
      return fallbackCopy(text)
    }
  } else {
    return fallbackCopy(text)
  }
}

/**
 * Fallback 复制方法（兼容非HTTPS环境）
 * @param {string} text - 要复制的文本
 * @returns {boolean} - 复制是否成功
 */
function fallbackCopy(text) {
  try {
    const el = document.createElement('textarea')
    el.value = text
    el.style.cssText = 'position:fixed;top:-9999px;left:-9999px'
    document.body.appendChild(el)
    el.select()
    const success = document.execCommand('copy')
    document.body.removeChild(el)
    return success
  } catch (err) {
    console.error('Fallback copy failed:', err)
    return false
  }
}