/**
 * 标签发起新会话 / 标签重命名的前端契约。
 *
 * 后端行为由 tests/test_label_*.py 覆盖；这里守住"前端确实把来源标签交给后端"
 * 这条链路——它以前只体现在 UI 上（草稿里显示标签名），刷新或重进就丢了：
 *   * api.js：renameLabel 打到 POST /v1/sessions/labels/rename，body {from,to}；
 *   * ChatPage：标签"发起新会话"记录来源，首轮系统消息带 label_origin，
 *     创建会话的那一次请求带 source_labels（后端据此写 index.json + tree.json）；
 *   * Sidebar：标签右键含"重命名标签"，并调用 sessions.renameLabel；
 *   * i18n：措辞是"发起新会话/Start new session"，不再是"生成新会话"；
 *     改名对话框的 key 两套语言齐全，提示说明"传播到全部会话 + 同名合并后
 *     下次需要时重算"，不承诺"立即生成"（实际是文档作废、下次读取才重算）。
 *
 * 用源码扫描而非挂载组件，与 api-wiring.test.js 同一套取舍：这条链路跨
 * ChatPage/Sidebar/i18n 三处，任何一处被顺手改回去（例如复用代理提示词时
 * 丢掉 marker），运行时只会静默丢失上下文。
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { sessions } from './api.js'

const SRC = join(__dirname, '..')
const read = (...parts) => readFileSync(join(SRC, ...parts), 'utf8')

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api.js sessions.renameLabel', () => {
  it('POST /v1/sessions/labels/rename with {from,to} and returns the result', async () => {
    const calls = []
    vi.stubGlobal('fetch', vi.fn(async (path, opts) => {
      calls.push({ path: String(path), opts })
      return {
        ok: true,
        status: 200,
        json: async () => ({ changed: true, from: '旧', to: '新', session_count: 2 }),
        headers: { get: () => '0' },
      }
    }))

    const res = await sessions.renameLabel('旧', '新')

    expect(calls).toHaveLength(1)
    expect(calls[0].path).toBe('/v1/sessions/labels/rename')
    expect(calls[0].opts.method).toBe('POST')
    expect(JSON.parse(calls[0].opts.body)).toEqual({ from: '旧', to: '新' })
    expect(res.session_count).toBe(2)
  })

  it('surfaces the backend error message (404 unknown label / 400 invalid name)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ error: 'label not found: 旧' }),
      headers: { get: () => '0' },
    })))

    await expect(sessions.renameLabel('旧', '新')).rejects.toThrow(/label not found/)
  })
})

describe('ChatPage：标签来源随创建会话的请求持久化', () => {
  const source = read('components', 'chat', 'ChatPage.svelte')

  it('消费 labelSessionRequest 时记录来源标签（systemPromptOrigin）', () => {
    expect(source).toMatch(/let systemPromptOrigin = \$state\(''\)/)
    expect(source).toMatch(/systemPromptOrigin = label/)
    // 草稿被普通新会话复用时必须清空，否则来源标签会串到无关会话上
    expect(source).toMatch(/systemPromptOrigin = ''/)
  })

  it('首轮系统消息与 source_labels 都走 label-session-request.js（标记不被漏）', () => {
    expect(source).toMatch(/import \{ buildFirstTurnSystemMessages, sourceLabelsForRequest \} from '\.\.\/\.\.\/lib\/label-session-request\.js'/)
    expect(source).toMatch(/return buildFirstTurnSystemMessages\(\{[\s\S]{0,160}origin: systemPromptOrigin,/)
    expect(source).toMatch(/sourceLabelsForRequest\(\{ sessionId, origin: systemPromptOrigin \}\)/)
    expect(source).toMatch(/reqBody\.source_labels = sourceLabels/)
  })
})

describe('Sidebar + i18n：标签右键三项与措辞', () => {
  const sidebar = read('components', 'Sidebar.svelte')
  const i18n = read('lib', 'i18n.svelte.js')

  it('标签菜单含"重命名标签"，并调用 sessions.renameLabel', () => {
    expect(sidebar).toMatch(/\{t\('labelSummarize'\)\}/)
    expect(sidebar).toMatch(/\{t\('labelNewSession'\)\}/)
    expect(sidebar).toMatch(/\{t\('labelRename'\)\}/)
    expect(sidebar).toMatch(/await sessions\.renameLabel\(from, to\)/)
    // 改名后旧标签节点必须从分类树上消失
    expect(sidebar).toMatch(/sessions\.renameLabel\(from, to\)[\s\S]{0,300}loadCategoryTree\(true\)/)
  })

  it('措辞改为"发起新会话/Start new session"（不是"生成新会话"）', () => {
    expect(i18n).toMatch(/labelNewSession: '发起新会话'/)
    expect(i18n).toMatch(/labelNewSession: 'Start new session'/)
    expect(i18n).not.toMatch(/labelNewSession: '生成新会话'/)
    expect(i18n).toMatch(/labelRename: '重命名标签'/)
    expect(i18n).toMatch(/labelRename: 'Rename label'/)
  })

  it('改名对话框文案：zh/en 两套齐全（缺 en 会静默回落成中文）', () => {
    for (const key of ['labelRename', 'labelRenameTitle', 'labelRenamePlaceholder',
                       'labelRenameHint', 'labelRenameDone', 'labelRenameFailed',
                       'labelRenameInvalid']) {
      const hits = i18n.match(new RegExp(`^\\s+${key}:`, 'gm')) || []
      expect(hits, `${key} 应在 zh 与 en 两处各出现一次`).toHaveLength(2)
    }
  })

  it('改名提示说明"传播到全部会话 + 同名合并后下次需要时重算"，不承诺立即生成', () => {
    // 后端 rename_label_doc：成员集合没变才改名搬迁；同名合并时两边文档都删除，
    // 下一次"发起新会话"读不到才自动重算 —— 文案不能写成"立即重新生成"。
    expect(i18n).toMatch(/labelRenameHint: '[^']*全部会话[^']*合并[^']*下次需要时[^']*'/)
    expect(i18n).toMatch(/labelRenameHint: '[^']*every session[^']*merge[^']*next time they[^']*'/)
    expect(i18n).not.toMatch(/labelRenameHint: '[^']*(立即生成|immediately)/)
  })
})
