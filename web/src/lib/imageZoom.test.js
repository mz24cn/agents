// @vitest-environment jsdom
/**
 * Tests for imageZoom.js — 图像预览双击缩放的几何计算
 * jsdom 无布局，mock 容器/图像尺寸与 getComputedStyle 的 padding
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { computeShortEdgeZoomSize } from './imageZoom.js'

function makePair({ cw, ch, padL, padR, padT, padB, w0, h0 }) {
  const container = document.createElement('div')
  Object.defineProperty(container, 'clientWidth', { value: cw })
  Object.defineProperty(container, 'clientHeight', { value: ch })
  const img = document.createElement('img')
  Object.defineProperty(img, 'clientWidth', { value: w0 })
  Object.defineProperty(img, 'clientHeight', { value: h0 })
  vi.stubGlobal('getComputedStyle', () => ({
    paddingLeft: `${padL}px`,
    paddingRight: `${padR}px`,
    paddingTop: `${padT}px`,
    paddingBottom: `${padB}px`,
  }))
  return { container, img }
}

afterEach(() => vi.unstubAllGlobals())

describe('computeShortEdgeZoomSize — 短边贴合窗口', () => {
  it('竖图（w<h）：放大后宽度 = 容器内容宽，高度成比例超出', () => {
    // 容器 800×600，padding 20 → 内容盒 760×560；自适应后竖图 28×560
    const { container, img } = makePair({ cw: 800, ch: 600, padL: 20, padR: 20, padT: 20, padB: 20, w0: 28, h0: 560 })
    const r = computeShortEdgeZoomSize(container, img)
    expect(r.w).toBe(760)
    expect(r.h).toBe(15200) // 560 × (760/28)
    expect(r.h).toBeGreaterThan(560) // 长边超出 → 需要滚动条
  })

  it('横图（w>h）：放大后高度 = 容器内容高，宽度成比例超出', () => {
    const { container, img } = makePair({ cw: 800, ch: 600, padL: 20, padR: 20, padT: 20, padB: 20, w0: 760, h0: 38 })
    const r = computeShortEdgeZoomSize(container, img)
    expect(r.h).toBe(560)
    expect(r.w).toBe(11200) // 760 × (560/38)
    expect(r.w).toBeGreaterThan(760)
  })

  it('小图：同样按短边贴合放大（scale > 1）', () => {
    const { container, img } = makePair({ cw: 800, ch: 600, padL: 20, padR: 20, padT: 20, padB: 20, w0: 100, h0: 50 })
    const r = computeShortEdgeZoomSize(container, img)
    // scale = max(760/100, 560/50) = 11.2 → 1120×560
    expect(r).toEqual({ w: 1120, h: 560 })
  })

  it('正方形图：两边等比贴满内容盒', () => {
    const { container, img } = makePair({ cw: 500, ch: 500, padL: 0, padR: 0, padT: 0, padB: 0, w0: 250, h0: 250 })
    const r = computeShortEdgeZoomSize(container, img)
    expect(r).toEqual({ w: 500, h: 500 })
  })

  it('退化尺寸（0/NaN）→ 返回 null，调用方保持原状', () => {
    const { container, img } = makePair({ cw: 800, ch: 600, padL: 20, padR: 20, padT: 20, padB: 20, w0: 0, h0: 560 })
    expect(computeShortEdgeZoomSize(container, img)).toBeNull()
  })

  it('padding 为 0 时内容盒 = client 尺寸', () => {
    const { container, img } = makePair({ cw: 400, ch: 300, padL: 0, padR: 0, padT: 0, padB: 0, w0: 400, h0: 100 })
    const r = computeShortEdgeZoomSize(container, img)
    expect(r).toEqual({ w: 1200, h: 300 })
  })
})
