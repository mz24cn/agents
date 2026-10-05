/**
 * 图像预览双击缩放：放大到「短边贴合窗口」（横图按高贴、竖图按宽贴），
 * 长边一般会超出窗口，由容器的 overflow:auto 出滚动条。
 * 纯几何计算，便于单测与浏览器 e2e 共用同一份逻辑。
 */

/**
 * @param {HTMLElement} containerEl 图像容器（overflow:auto，含 padding）
 * @param {HTMLImageElement} imgEl 当前处于自适应（未放大）状态的图像
 * @returns {{ w: number, h: number } | null} 放大后的像素尺寸；无法计算时返回 null
 */
export function computeShortEdgeZoomSize(containerEl, imgEl) {
  const cs = getComputedStyle(containerEl)
  // 可用区域 = 容器内容盒（扣除 padding）
  const W = containerEl.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
  const H = containerEl.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)
  // 当前（自适应）渲染尺寸；用它算比例可兼容 SVG 无固有尺寸的情况
  const w0 = imgEl.clientWidth
  const h0 = imgEl.clientHeight
  if (!w0 || !h0 || !W || !H) return null
  // 短边贴合窗口 = 两个贴合比中较大的那个
  const scale = Math.max(W / w0, H / h0)
  return {
    w: Math.round(w0 * scale),
    h: Math.round(h0 * scale),
  }
}
