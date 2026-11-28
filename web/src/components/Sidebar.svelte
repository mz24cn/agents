<script>
  import { onMount, onDestroy, tick } from 'svelte'
  import { router, navigate } from '../lib/router.svelte.js'
  import { t } from '../lib/i18n.svelte.js'
  import { sessions, subscribeSessionEvents } from '../lib/api.js'
  import { sessionRestore, sessionDownload, newSessionCreated, sessionDeleted, sessionForked, currentSession, newSessionRequest, terminalOpen, openSessionLogDir, messageScrollRequest, awaitingUserAnswer, labelSessionRequest } from '../lib/session-state.svelte.js'
  import { sidebarWidth, setSidebarWidth, toggleSidebarCollapsed, collapseSidebar, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from '../lib/sidebar-width.svelte.js'
  import SessionCategoryNode from './SessionCategoryNode.svelte'
  import { locateTerminalEnvNode } from '$lib/terminalChain.js'
  import { parseLabels } from '$lib/labels.js'
  import { SESSION_FLAGS, sessionFlagLabel, sameFlag } from '$lib/session-flags.js'
  import { collectLabelCandidates, appendLabelToList } from '$lib/session-label-candidates.js'
  import {
    CLIENT_TOOL_STATUS,
    applyLabelRename,
    factsFromRow,
    mergeFacts,
    projectRow,
  } from '$lib/session-facts.js'

  const SESSION_PAGE_SIZE = 100

  // 列表接口返回的原始行：只承载"哪些会话属于这个分类的这一页"。标题 / 旗标 /
  // 标签 / 推理状态不在这里，它们在 sessionFacts 里按 session_id 共享一份（见下）。
  let sessionRows = $state([])
  let sessionError = $state('')
  let sessionLoading = $state(false)
  let sessionLoadingMore = $state(false)
  let sessionPage = $state(1)
  let sessionHasMore = $state(true)
  let restoreError = $state('')
  let lastClickTime = $state(0)
  let searchOpen = $state(false)
  let searchText = $state('')
  let activeSearchQuery = $state('')
  let searchInput = $state(null)
  // 视图模式：'recent'（整体会话列表，无分类框）| 'category'（主题分类树）|
  // 'workspace'（工作区目录）| 'history'（月份分组）| 'terminal'（终端列表）。
  // 五个森林根不再作为树节点相互挤占：折叠成标题行的一组模式图标，标题行
  // 之下只渲染当前模式根的子级。directoryOpen = 非 recent（树框可见）。
  let viewMode = $state('recent')
  let directoryOpen = $derived(viewMode !== 'recent')
  // 每个分类模式各自记住选中的末级分类，模式间切换不丢选择与滚动位置。
  let modeSelections = new Map()
  let categoryTree = $state([])
  let categoryTreeLoading = $state(false)
  let categoryTreeError = $state('')
  let categoryExpandedPaths = $state(new Set())
  let selectedCategory = $state('')
  let categoryTreePromise = null
  let categoryTreeBuilding = $state(false)
  let categoryTreePollTimer = null
  let categoryTreePolling = false
  const CATEGORY_TREE_POLL_MS = 10000
  let sessionListElement = $state(null)
  let categoryTreeElement = $state(null)
  let recentSessionView = null
  let categorySessionViews = new Map()
  let directoryTreeScrollTop = 0
  let sessionLoadToken = 0

  // 菜单位置可见性保护：菜单与屏幕边缘保留的安全间距
  const MENU_EDGE_MARGIN = 8
  const SUBMENU_PREFERRED_MAX = 620
  const SUBMENU_MIN_HEIGHT = 160

  // 弹出菜单状态
  let menuOpenId = $state(null)   // 当前展开菜单的 session id
  let menuAnchor = $state({ x: 0, y: 0 })  // 触发点（··· 按钮右下角 / 触摸点）
  let menuPos = $state({ x: 0, y: 0 })  // fixed 定位坐标（已经过 clampMenuIntoView 夹取）
  let menuElement = $state(null)      // 弹出菜单 DOM，用于测量自身尺寸
  let menuSize = $state({ w: 0, h: 0 })  // 弹出菜单实测尺寸（子面板锚点用）
  let menuConstrained = $state(false) // 屏幕太矮、菜单整体放不下时启用（压缩行高 + 内部滚动）
  // ··· 子面板的锚点（相对视口）、展开方向与可用高度：下方空间不够时向上展开
  let submenuPlacement = $state({ left: 0, top: 0, bottom: 0, up: false, maxHeight: SUBMENU_PREFERRED_MAX })
  let userMessageMenuOpen = $state(false)
  let userMessageMenuLoading = $state(false)
  let userMessageMenuError = $state('')
  let menuUserMessages = $state([])
  let menuSessionData = $state(null)
  let executionAnalysisOpen = $state(false)
  let executionAnalysisLoading = $state(false)
  let executionAnalysisError = $state('')
  let executionAnalysisData = $state(null)
  let sessionDirectoryMenuOpen = $state(false)
  let sessionDirectoryMenuLoading = $state(false)
  let sessionDirectoryMenuError = $state('')
  let menuSessionDirectories = $state([])
  // 会话"打开终端"子面板：按会话执行环境定位终端根下的环境节点，
  // 面板内默认展开全部多级嵌套终端（terminal_layer），双击打开语义不变。
  let sessionTerminalMenuOpen = $state(false)
  let sessionTerminalLoading = $state(false)
  let sessionTerminalError = $state('')
  let sessionTerminalTarget = $state(null)   // {node, path}；null=环境无节点
  let sessionTerminalCollapsed = $state(new Set())  // 默认全展开，点击可临时收起

  // 「设置标题」「设置标签」两项末尾的 ··· 面板：主区域照常打开对话框，··· 悬停
  // 打开右侧子面板。旗标面板只做旗标（点选即时保存），标签面板只做候选追加。
  let sessionFlagMenuOpen = $state(false)
  let sessionFlagMenuBusy = $state(false)
  let sessionFlagMenuError = $state('')
  let sessionLabelsMenuOpen = $state(false)
  let sessionLabelsMenuBusy = $state(false)
  let sessionLabelsMenuError = $state('')

  // 标题对话框状态（会话「…」菜单里的「设置标题」）：只有标题输入框，旗标不在这里改。
  let titleDialogOpen = $state(false)
  let titleDialogSid = $state(null)
  let titleDialogValue = $state('')
  let titleDialogLoading = $state(false)
  let titleDialogError = $state('')
  let titleDialogInput = $state(null)

  // 标签对话框状态（「设置标签」）：只有输入框 + 保存/取消；追加走 ··· 面板，删除
  // 只能在输入框里改（半角/全角逗号、空白都是分隔符，与模型/工具标签输入同一语义）。
  let labelsDialogOpen = $state(false)
  let labelsDialogSid = $state(null)
  let labelsDialogValue = $state('')
  let labelsDialogLoading = $state(false)
  let labelsDialogError = $state('')
  let labelsDialogInput = $state(null)

  // ··· 标签面板的候选：森林里的全部标签节点（去重 + 稳定排序），缺数据时打开面板
  // 按需 loadCategoryTree()；点选即追加的当前状态见 menuSessionLabelSet()。
  const labelCandidates = $derived(collectLabelCandidates(categoryTree))

  // hover 弹出菜单状态：鼠标悬停到 ... 按钮即弹出，悬停在菜单上保持显示
  let hoverBtnId = $state(null)   // 当前鼠标悬停的 ... 按钮 session id
  let hoverMenuId = $state(null)  // 当前鼠标悬停的弹出菜单 session id
  let closeTimer = null           // 延迟关闭定时器（跨越按钮与菜单之间的缝隙）

  // 拖动状态
  let isDragging = $state(false)
  let dragStartX = 0        // mousedown 时的 clientX
  let dragStartWidth = 0    // mousedown 时的侧边栏宽度

  // --- 会话权威事实（Session Facts / Session Status Stream）---------------------
  // 同一个 session 会同时挂在「主题分类 / 工作区目录 / 标签 / 历史月份」等多个列表下：
  // 每个列表各自分页、各自刷新，而 SSE 的状态事件与 init 快照根本不区分分类。标题、
  // 旗标、标签、推理状态一旦只写回"当前这一行的副本"，同一会话在不同分类里的副本就会
  // 各自漂移 —— 在主题下改完名切到标签页仍是旧名；某个分类里晚到的列表响应会把另一个
  // 分类刚收到的 streaming / client_tool_calls 盖回上一轮的 done_success_unread。
  // 服务端的权威同样只有一份（index.json 的元数据 + 内存里的 _session_statuses），所以
  // 前端也只维护一份按 session_id 索引的事实，列表行只是它在"某分类某页"下的投影：
  // 切分类、翻页、SSE、刷新快照读写的是同一份。字段级胜出规则（新值赢旧值、用户改名
  // 不被自动标题降级、快照不许倒退实时状态）在 $lib/session-facts.js 的 mergeFacts 里，
  // 纯函数、可脱离组件单测。
  // 共享事实字典：session_id -> 权威事实。用普通对象而不是 Map —— Svelte 的 $state
  // 对「对象属性写入 / 删除」的追踪是可靠的，而 Map.set() 不会让只读 .get() 的派生值
  // 重算（svelte 5.55 实测）：事实改了、行却不改，正是「切分类不一致」的来源之一。
  let sessionFacts = $state({})
  // 「某个 key 首次写入」这种新增属性同样可能不在派生依赖里，投影显式读这个版本号：
  // 每次写事实都 bump，保证「事实变了，行一定重算」。
  let factsRev = $state(0)
  // 渲染入口：所有读取都走这份投影，切换分类 / 翻页 / 搜索时同一会话自动带上新事实。
  let sessionList = $derived(projectSessionRows())

  function factKey(id) { return String(id ?? '') }

  function projectSessionRows() {
    void factsRev   // 依赖事实版本号，见上
    return sessionRows.map(row => projectRow(row, sessionFacts[factKey(row.session_id)] ?? null))
  }
  // 本次页面会话内「衍生会话 -> 原会话 ID」的记录，仅用于在条目前显示来源标记。
  // 普通 Map 即可：写入时机总在 sessionRows 重新赋值之前，渲染时读取即可见。
  const forkParents = new Map()

  /**
   * 写入一个会话的权威事实。``at`` 是这条事实的"可采信时刻"：SSE 用收到时刻、用户
   * 写入用服务端确认时刻、列表 / 快照响应用**请求发起时刻**（响应不可能比它自己的
   * 请求更早，晚到的旧响应因此天然输给请求期间到达的事件）。
   */
  function patchFacts(sid, patch, opts = {}) {
    if (!sid || !patch) return
    // 第三个参数可以简写成来源字符串（'user' | 'sse' | 'snapshot' | 'local'），
    // 也可以传 { at, kind }：列表/快照必须显式带"请求发起时刻"。
    const options = typeof opts === 'string' ? { kind: opts } : (opts || {})
    const at = Number(options.at) || Date.now()
    const next = mergeFacts(
      sessionFacts[factKey(sid)],
      { ...patch, updatedAt: at, statusAt: options.statusAt || at },
      { session_id: sid, at, kind: options.kind || 'user' },
    )
    if (next) {
      sessionFacts[factKey(sid)] = next
      factsRev += 1
    }
  }

  /** 列表 / 快照响应整批种进事实表：整页共用"请求发起时刻"。 */
  function seedFacts(rows, requestedAt) {
    for (const row of rows || []) {
      const patch = factsFromRow(row)
      if (!patch) continue
      patchFacts(row.session_id, patch, { at: requestedAt, kind: 'snapshot' })
    }
  }

  /**
   * 会话被确认不存在（删除成功 / 下载拿到 404）：从当前页与所有分类缓存里一并移除，
   * 并清掉共享事实。只删当前分类那一行会留下"历史里没了、工作区里还在"的不一致。
   */
  function forgetSession(sessionId) {
    if (!sessionId) return
    sessionRows = sessionRows.filter(row => row.session_id !== sessionId)
    delete sessionFacts[factKey(sessionId)]
    factsRev += 1
    for (const [key, view] of Array.from(categorySessionViews.entries())) {
      if (!Array.isArray(view?.rows)) continue
      if (!view.rows.some(row => row?.session_id === sessionId)) continue
      categorySessionViews.set(key, {
        ...view,
        rows: view.rows.filter(row => row.session_id !== sessionId),
      })
    }
    if (Array.isArray(recentSessionView?.rows)
        && recentSessionView.rows.some(row => row?.session_id === sessionId)) {
      recentSessionView = {
        ...recentSessionView,
        rows: recentSessionView.rows.filter(row => row.session_id !== sessionId),
      }
    }
  }

  /**
   * 标签改名后同步事实层：同一会话在任何分类下都立即显示新标签；目标标签已存在时
   * 按后端的同名合并语义去重（顺序保持不变，列表不跳）。原始行与分类缓存里可能还
   * 残留旧标签（facts 尚未建立时），一并改写。
   */
  function renameLabelInFacts(from, to) {
    if (!from || !to || from === to) return
    let touched = false
    for (const sid of Object.keys(sessionFacts)) {
      const facts = sessionFacts[sid]
      if (!Array.isArray(facts?.labels) || !facts.labels.includes(from)) continue
      sessionFacts[sid] = {
        ...facts,
        labels: applyLabelRename(facts.labels, from, to),
        updatedAt: Date.now(),
      }
      touched = true
    }
    if (touched) factsRev += 1
    const fix = rows => (rows || []).map(row => (
      Array.isArray(row?.labels) && row.labels.includes(from)
        ? { ...row, labels: applyLabelRename(row.labels, from, to) }
        : row
    ))
    sessionRows = fix(sessionRows)
    for (const [key, view] of Array.from(categorySessionViews.entries())) {
      if (Array.isArray(view?.rows)) {
        categorySessionViews.set(key, { ...view, rows: fix(view.rows) })
      }
    }
    if (Array.isArray(recentSessionView?.rows)) {
      recentSessionView = { ...recentSessionView, rows: fix(recentSessionView.rows) }
    }
  }

  /** 状态事件（SSE 实时事件与 init 快照）→ 权威事实。 */
  function applySessionStatus(sid, status) {
    if (!sid || typeof status !== 'string' || !status) return
    patchFacts(sid, { status }, { kind: 'sse' })
    // 推理中的新会话：SSE 会早于 newSessionCreated 到达，先补一行占位，标题由
    // title_update 或随后的列表刷新补全，否则用户看不到"正在推理"的会话。
    if (status === 'streaming' && !(directoryOpen && selectedCategory)
        && !sessionRows.some(row => row.session_id === sid)) {
      sessionRows = [{ session_id: sid, title: sid }, ...sessionRows]
    }
  }

  // 经 SSE 收到的权威标题（title_update / 终态 done_* 事件 / init 快照）。
  // 事实按 session_id 共享，所以一个标题同时生效于所有分类下的条目；用户手填过的
  // 标题不会被这里回填的自动标题降级（胜出规则见 session-facts.js 的 mergeFacts）。
  function applySseTitle(sid, title, titleGiven) {
    if (!sid || !title) return
    patchFacts(sid, { title, titleGiven }, { kind: 'sse' })
  }

  let _unsubscribeSessionEvents = null

  onMount(() => {
    loadSessions()
    _unsubscribeSessionEvents = subscribeSessionEvents(
      (data) => {
        if (data.event === 'init') {
          // init 快照：一次种进服务端当前的全部状态与权威标题。重连后错过的
          // 事件（包括别的标签页触发的推理）都由这里补齐，且不区分分类。
          const sids = data.sessions || {}
          const titles = data.titles || {}
          for (const [sid, status] of Object.entries(sids)) {
            applySessionStatus(sid, status)
            // init 快照附带权威标题：重连后补回那些错过了终态事件的
            // 会话的侧边栏标题。
            const info = titles[sid]
            if (info && info.title) applySseTitle(sid, info.title, info.title_given)
          }
        } else if (data.event === 'message') {
          applySessionStatus(data.session_id, data.status)
          // 终态事件携带后端权威标题（AI 生成或原始标题）：推理结束后
          // 恢复侧边栏条目的标题，避免临时用户消息标题残留到刷新页面。
          // client_tool_calls 也带标题（后端同样从 index.json 取），等待中的
          // 蓝色条目同样要在每个分类显示同一个标题。
          if (data.title && typeof data.status === 'string'
              && (data.status.startsWith('done_') || data.status === CLIENT_TOOL_STATUS)) {
            applySseTitle(data.session_id, data.title, data.title_given)
          }
        } else if (data.event === 'title_update') {
          applySseTitle(data.session_id, data.title, data.title_given)
        }
      },
      (_err) => {
        // SSE error — silently ignore; no automatic reconnect per spec
      },
    )
  })

  onDestroy(() => {
    stopCategoryTreePolling()
    if (labelStatusTimer) {
      clearTimeout(labelStatusTimer)
      labelStatusTimer = null
    }
    if (_unsubscribeSessionEvents) {
      _unsubscribeSessionEvents()
      _unsubscribeSessionEvents = null
    }
  })

  /**
   * Derive a CSS class string for a session row based on its status.
   * Combines status with active state.
   */
  function getSessionStatusClass(entry) {
    const status = entry._status || ''
    if (status === 'streaming') return 'status-streaming'
    if (status === 'paused') return 'status-paused'
    // 等待客户端工具回传：蓝色，既不是"推理中"的橙色也不是"已成功结束"的绿色。
    if (status === CLIENT_TOOL_STATUS) return 'status-client-tool'
    if (status === 'done_success_unread') return 'status-done-success-unread'
    if (status === 'done_error_unread') return 'status-done-error-unread'
    return ''
  }

  function mergeSessionLists(existing, incoming) {
    const seen = new Set(existing.map(s => s.session_id))
    const appended = incoming.filter(s => {
      if (!s?.session_id || seen.has(s.session_id)) return false
      seen.add(s.session_id)
      return true
    })
    return [...existing, ...appended]
  }

  async function loadSessions(append = false) {
    if (append && (!sessionHasMore || sessionLoading || sessionLoadingMore)) return

    const requestToken = ++sessionLoadToken
    // 请求发起时刻：一份响应不可能比它自己的请求更早，所以这个时刻就是这份快照
    // 的"可采信上界"。事实按这个时刻写入，晚到的旧响应自然输给请求期间到达的
    // SSE 事件（标题与状态都是同一套规则，见 session-facts.js 的 mergeFacts）。
    const requestedAt = Date.now()
    const requestedCategory = directoryOpen ? selectedCategory : ''
    const requestedSearch = activeSearchQuery
    const nextPage = append ? sessionPage + 1 : 1
    if (append) {
      sessionLoadingMore = true
    } else {
      sessionLoading = true
      sessionPage = 1
      sessionHasMore = true
      sessionRows = []
    }
    sessionError = ''
    try {
      const data = requestedSearch
        ? await sessions.search(requestedSearch, nextPage, SESSION_PAGE_SIZE)
        : await sessions.list(nextPage, SESSION_PAGE_SIZE, requestedCategory)
      if (requestToken !== sessionLoadToken) return
      // 列表响应带的是持久化元数据 + 取数那一刻的状态覆盖（_status）。先把整页
      // 种进按 session_id 共享的权威事实，再只更新"本分类这一页"的原始行：事实层
      // 已经记住的更新值（请求期间到达的 streaming / client_tool_calls / 用户改名）
      // 不会被这份快照抹掉，反过来快照也不会保留已经作废的旧标题。
      const incoming = (data.sessions ?? []).map(entry => ({ ...entry }))
      seedFacts(incoming, requestedAt)
      if (append) {
        sessionRows = mergeSessionLists(sessionRows, incoming)
      } else {
        // A just-created inference is announced as streaming before its initial
        // user message has necessarily reached conversation.json/session index.
        // Keep those SSE-only rows until a later list response can supply the
        // persisted title and metadata.
        const incomingIds = new Set(incoming.map(entry => entry.session_id))
        const liveOnly = requestedCategory ? [] : sessionRows.filter(entry =>
          !incomingIds.has(entry.session_id)
          && (entry._status === 'streaming'
              || sessionFacts[factKey(entry.session_id)]?.status === 'streaming')
        )
        sessionRows = [...liveOnly, ...incoming]
      }
      sessionPage = data.page ?? nextPage
      sessionHasMore = Boolean(data.has_more)
      if (append) sessionLoadingMore = false
      else sessionLoading = false
      await tick()
      if (directoryOpen && requestedCategory === selectedCategory) {
        categorySessionViews.set(requestedCategory, currentSessionView())
      } else if (!directoryOpen && !requestedCategory && !requestedSearch) {
        recentSessionView = currentSessionView()
      }
    } catch (err) {
      if (requestToken === sessionLoadToken) {
        sessionError = err.message || t('fetchSessionsFailed')
      }
    } finally {
      if (requestToken === sessionLoadToken) {
        if (append) {
          sessionLoadingMore = false
        } else {
          sessionLoading = false
        }
      }
    }
  }

  function handleSessionScroll(e) {
    const { scrollTop, scrollHeight, clientHeight } = e.target
    if (directoryOpen && selectedCategory) {
      const view = categorySessionViews.get(selectedCategory)
      if (view) view.scrollTop = scrollTop
    } else if (!directoryOpen && !activeSearchQuery && recentSessionView) {
      recentSessionView.scrollTop = scrollTop
    }
    if (scrollHeight - scrollTop - clientHeight < 50) {
      loadSessions(true)
    }
  }

  function currentSessionView() {
    return {
      // 缓存"这个分类看哪几行、什么顺序、翻到第几页"——不缓存标题/旗标/标签/状态：
      // 那些是按 session_id 共享的权威事实，切回本分类时重新投影，别处改名/进入
      // 推理后这里自然一致（旧实现把整行含标题一起缓存，切回来就是陈旧副本）。
      rows: sessionRows.map(entry => ({ ...entry })),
      sessionError,
      sessionPage,
      sessionHasMore,
      loaded: !sessionLoading && !sessionLoadingMore,
      scrollTop: sessionListElement?.scrollTop || 0,
    }
  }

  function saveCurrentSessionView() {
    if (sessionLoading || sessionLoadingMore) return
    if (directoryOpen && selectedCategory) {
      categorySessionViews.set(selectedCategory, currentSessionView())
    } else if (!directoryOpen && !activeSearchQuery) {
      recentSessionView = currentSessionView()
    }
  }

  async function restoreSessionView(view) {
    if (!view?.loaded) return false
    sessionLoadToken++
    sessionRows = (view.rows ?? []).map(entry => ({ ...entry }))
    sessionError = view.sessionError || ''
    sessionPage = view.sessionPage || 1
    sessionHasMore = Boolean(view.sessionHasMore)
    sessionLoading = false
    sessionLoadingMore = false
    await tick()
    if (sessionListElement) sessionListElement.scrollTop = view.scrollTop || 0
    return true
  }

  async function restoreDirectoryScroll() {
    await tick()
    if (categoryTreeElement) categoryTreeElement.scrollTop = directoryTreeScrollTop
  }

  function scrollItemWithinList(listElement, itemElement) {
    if (!listElement || !itemElement) return
    const listRect = listElement.getBoundingClientRect()
    const itemRect = itemElement.getBoundingClientRect()
    let nextScrollTop = listElement.scrollTop
    if (itemRect.top < listRect.top) {
      nextScrollTop += itemRect.top - listRect.top
    } else if (itemRect.bottom > listRect.bottom) {
      nextScrollTop += itemRect.bottom - listRect.bottom
    }
    if (nextScrollTop !== listElement.scrollTop) {
      listElement.scrollTo({ top: nextScrollTop, behavior: 'smooth' })
    }
  }

  async function scrollCategoryIntoView(path) {
    if (!path) return
    await tick()
    const row = [...(categoryTreeElement?.querySelectorAll('[data-category-path]') || [])]
      .find(element => element.dataset.categoryPath === path)
    scrollItemWithinList(categoryTreeElement, row)
  }

  async function scrollSessionIntoView(sessionId) {
    if (!sessionId) return
    await tick()
    const row = [...(sessionListElement?.querySelectorAll('[data-session-id]') || [])]
      .find(element => element.dataset.sessionId === sessionId)
    scrollItemWithinList(sessionListElement, row)
  }

  async function loadCategoryTree(force = false) {
    if (!force && categoryTree.length > 0 && !categoryTreeBuilding) return categoryTree
    if (categoryTreePromise) return categoryTreePromise
    categoryTreeLoading = true
    categoryTreeError = ''
    categoryTreePromise = sessions.tree()
      .then(data => {
        categoryTree = data.tree || []
        if (data.building) {
          // 后端正在后台生成分类树：显示提示并按固定间隔重试，直到 building 消失
          categoryTreeBuilding = true
          startCategoryTreePolling()
        } else {
          categoryTreeBuilding = false
          stopCategoryTreePolling()
        }
        // "所属目录"菜单打开期间树可能已变化（新会话挂载、后台构建完成等），同步刷新
        if (sessionDirectoryMenuOpen && menuOpenId) {
          menuSessionDirectories = findSessionDirectories(menuOpenId)
        }
        return categoryTree
      })
      .catch(err => {
        categoryTreeError = err.message || t('sessionDirectoryLoadFailed')
        return []
      })
      .finally(() => {
        categoryTreeLoading = false
        categoryTreePromise = null
      })
    return categoryTreePromise
  }

  function startCategoryTreePolling() {
    stopCategoryTreePolling()
    categoryTreePollTimer = setInterval(async () => {
      if (categoryTreePolling) return
      categoryTreePolling = true
      try {
        await loadCategoryTree(true)
      } finally {
        categoryTreePolling = false
      }
    }, CATEGORY_TREE_POLL_MS)
  }

  function stopCategoryTreePolling() {
    if (categoryTreePollTimer) {
      clearInterval(categoryTreePollTimer)
      categoryTreePollTimer = null
    }
  }

  function expandCategoryAncestors(path) {
    const parts = String(path || '').split('/').filter(Boolean)
    const next = new Set(categoryExpandedPaths)
    for (let index = 1; index < parts.length; index++) {
      next.add(parts.slice(0, index).join('/'))
    }
    categoryExpandedPaths = next
  }

  // 森林根按类型定位（"最近会话"根已在 v7 移除；旧树里即使残留也不作为模式）。
  function findRootByKind(tree, kind) {
    for (const node of tree || []) {
      if (!node || typeof node !== 'object') continue
      const type = node.type || 'category'
      if (type === kind) return node
    }
    return null
  }

  // 分类路径首段是根 id；据此反查该路径属于哪个模式。
  function modeOfCategoryPath(path) {
    const rootId = String(path || '').split('/')[0]
    const root = (categoryTree || []).find(n => n && String(n.id) === rootId)
    if (!root) return ''
    const type = root.type || 'category'
    return type === 'recent' ? 'recent' : type
  }

  let activeRoot = $derived(
    viewMode === 'recent' ? null : findRootByKind(categoryTree, viewMode))

  // 根节点本身不再渲染：把当前模式根的子级提为顶层，查询路径仍带根 id 前缀
  // （与后端按 id 逐段解析一致）。单环境"本地终端"根没有环境子节点时，根
  // 自身就是可双击的终端目标，作为唯一条目渲染。
  let treeTopNodes = $derived.by(() => {
    const root = activeRoot
    if (!root) return []
    const kids = (root.children || []).filter(child => child && typeof child === 'object')
    if (viewMode === 'terminal') {
      const envKids = kids.filter(child => (child.type || 'category') === 'terminal')
      if (envKids.length) {
        return envKids.map(child => ({ ...child, category: `${root.id}/${child.id}` }))
      }
      // 只有本地环境时，根本身就是"本地"环境节点，必须始终渲染这一行：它是
      // 右键"新建子终端"的锚点（终端层直接挂在它下面）。若把它提成子级，用户
      // 连第一个嵌套终端都无处创建，第二个也无从追加。
      return [{ ...root, category: String(root.id) }]
    }
    return kids.map(child => ({ ...child, category: `${root.id}/${child.id}` }))
  })

  function clearSessionListState() {
    sessionLoadToken++
    sessionRows = []
    sessionError = ''
    sessionPage = 1
    sessionHasMore = false
    sessionLoading = false
    sessionLoadingMore = false
  }

  // 切换视图模式：记住旧模式选中的分类，恢复新模式自己的选择；recent 模式
  // 只有整体列表，terminal 模式只有终端列表，两者都不带会话分类列表。
  async function showModeState(mode) {
    if (viewMode === mode) return
    saveCurrentSessionView()
    if (selectedCategory) modeSelections.set(modeOfCategoryPath(selectedCategory), selectedCategory)
    viewMode = mode
    searchOpen = false
    activeSearchQuery = ''
    await loadCategoryTree()
    selectedCategory = modeSelections.get(mode) || ''
    if (mode !== 'terminal' && selectedCategory) {
      expandCategoryAncestors(selectedCategory)
      const restored = await restoreSessionView(categorySessionViews.get(selectedCategory))
      if (!restored) await loadSessions()
    } else {
      clearSessionListState()
    }
    await restoreDirectoryScroll()
  }

  function toggleCategoryPath(path) {
    const next = new Set(categoryExpandedPaths)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    categoryExpandedPaths = next
  }

  async function selectCategory(path) {
    if (selectedCategory === path && sessionList.length > 0) return
    saveCurrentSessionView()
    selectedCategory = path
    modeSelections.set(viewMode, path)
    activeSearchQuery = ''
    const restored = await restoreSessionView(categorySessionViews.get(path))
    if (!restored) await loadSessions()
  }

  async function showRecentSessions() {
    if (viewMode === 'recent' && !activeSearchQuery) return
    saveCurrentSessionView()
    if (categoryTreeElement) directoryTreeScrollTop = categoryTreeElement.scrollTop
    if (selectedCategory) modeSelections.set(modeOfCategoryPath(selectedCategory), selectedCategory)
    viewMode = 'recent'
    searchOpen = false
    activeSearchQuery = ''
    const restored = await restoreSessionView(recentSessionView)
    if (!restored) await loadSessions()
  }

  function handleNewSession(e) {
    e.preventDefault()
    e.stopPropagation()
    closeMenu()
    newSessionRequest.token += 1
    navigate('#/chat')
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
  }

  function toggleSearch(e) {
    e.preventDefault()
    e.stopPropagation()
    if (selectedCategory) modeSelections.set(modeOfCategoryPath(selectedCategory), selectedCategory)
    viewMode = 'recent'
    selectedCategory = ''
    searchOpen = !searchOpen
    if (searchOpen) {
      // 等待 DOM 更新（{#if} 渲染 input）后再聚焦
      tick().then(() => {
        if (searchInput) searchInput.focus()
      })
    } else {
      // 收起搜索框 = 取消搜索，恢复默认列表
      activeSearchQuery = ''
      loadSessions()
    }
  }

  async function handleSearchKeydown(e) {
    if (e.key !== 'Enter') return
    e.preventDefault()
    activeSearchQuery = searchText.trim()
    await loadSessions()
  }

  async function handleSessionClick(sessionId) {
    const now = Date.now()
    if (now - lastClickTime < 300) {
      return
    }
    lastClickTime = now
    restoreError = ''
    await loadSessionIntoChat(sessionId, { collapseMobile: true })
  }

  /**
   * 下载并选中一个会话：走的是会话列表点击的同一条路径
   * （sessions.get + sessionRestore.pending），所以 ChatPage 的会话恢复
   * 逻辑只有一份。衍生新会话后要复用它，因此从 handleSessionClick 里拆出来。
   */
  async function loadSessionIntoChat(sessionId, { collapseMobile = true } = {}) {
    // 移动端：立即收起侧边栏，不能等网络请求返回后再收起，
    // 否则 fixed 遮罩持续挡住对话内容，用户体验为"点两下"
    if (collapseMobile && window.innerWidth < 1024) {
      collapseSidebar()
    }
    // 先进入 ChatPage，再开始下载。这样从 Setup 等页面切换会话时，
    // 500ms 后出现的下载进度条也有可见的挂载区域。
    navigate('#/chat')
    const downloadToken = sessionDownload.token + 1
    sessionDownload.token = downloadToken
    sessionDownload.loading = true
    sessionDownload.visible = false
    sessionDownload.received = 0
    sessionDownload.total = 0
    const progressDelay = setTimeout(() => {
      if (sessionDownload.loading && sessionDownload.token === downloadToken) {
        sessionDownload.visible = true
      }
    }, 500)
    try {
      const data = await sessions.get(sessionId, ({ received, total }) => {
        if (sessionDownload.token !== downloadToken) return
        sessionDownload.received = received
        sessionDownload.total = total
      })
      // A newer click owns the UI now; do not let this response replace it.
      if (sessionDownload.token !== downloadToken) return
      // 直接展开所有字段，保持与后端数据一致
      const msgs = (data.messages ?? []).map(m => ({ ...m }))
      const meta = data.meta ?? null
      sessionRestore.pending = { sessionId, messages: msgs, meta }
    } catch (err) {
      if (sessionDownload.token !== downloadToken) return
      restoreError = err.message || t('restoreSessionFailed')
      // 只有后端明确回答"这个会话不存在"（404）时才把条目从列表里同步移除。
      // 其它失败一律不是"会话没了"：5xx / 501 / 代理抖动 / keep-alive 串流错位
      // 都只是这一次传输坏了，会话在磁盘上好好的。曾经一次 501 让侧边栏条目
      // 当场消失、刷新又回来 —— 前端不能凭一次失败的传输状态删除 UI 状态。
      if (err.status === 404) {
        forgetSession(sessionId)
      }
    } finally {
      clearTimeout(progressDelay)
      if (sessionDownload.token === downloadToken) {
        sessionDownload.loading = false
        sessionDownload.visible = false
        sessionDownload.received = 0
        sessionDownload.total = 0
      }
    }
  }

  function openMenuAt(e, sid) {
    e.stopPropagation()
    const btn = e.currentTarget
    const rect = btn.getBoundingClientRect()
    // 菜单出现在按钮右下角，用 fixed 定位浮于最顶层；
    // menuPos 随后由 clampMenuIntoView() 按菜单实测尺寸夹回视口内，
    // 贴近屏幕底部 / 右边缘的条目也不会被截断（见 clampMenuIntoView）。
    menuAnchor = { x: rect.right + 4, y: rect.top }
    menuPos = { ...menuAnchor }
    resetMenuPlacement()
    void clampMenuIntoView()
    if (menuOpenId !== sid) {
      userMessageMenuOpen = false
      userMessageMenuLoading = false
      userMessageMenuError = ''
      menuUserMessages = []
      menuSessionData = null
      executionAnalysisOpen = false
      executionAnalysisLoading = false
      executionAnalysisError = ''
      executionAnalysisData = null
      sessionDirectoryMenuOpen = false
      sessionDirectoryMenuLoading = false
      sessionDirectoryMenuError = ''
      menuSessionDirectories = []
      sessionFlagMenuOpen = false
      sessionFlagMenuBusy = false
      sessionFlagMenuError = ''
      sessionLabelsMenuOpen = false
      sessionLabelsMenuBusy = false
      sessionLabelsMenuError = ''
    }
    menuOpenId = sid
  }

  function openMenu(e, sid) {
    e.stopPropagation()
    openMenuAt(e, sid)
  }

  function closeMenu() {
    menuOpenId = null
    closeTerminalMenu()
    userMessageMenuOpen = false
    userMessageMenuLoading = false
    userMessageMenuError = ''
    menuUserMessages = []
    menuSessionData = null
    executionAnalysisOpen = false
    executionAnalysisLoading = false
    executionAnalysisError = ''
    executionAnalysisData = null
    sessionDirectoryMenuOpen = false
    sessionDirectoryMenuLoading = false
    sessionDirectoryMenuError = ''
    menuSessionDirectories = []
    sessionTerminalMenuOpen = false
    sessionTerminalLoading = false
    sessionTerminalError = ''
    sessionTerminalTarget = null
    sessionTerminalCollapsed = new Set()
    sessionFlagMenuOpen = false
    sessionFlagMenuBusy = false
    sessionFlagMenuError = ''
    sessionLabelsMenuOpen = false
    sessionLabelsMenuBusy = false
    sessionLabelsMenuError = ''
    hoverBtnId = null
    hoverMenuId = null
    if (closeTimer) {
      clearTimeout(closeTimer)
      closeTimer = null
    }
  }

  // --- hover 弹出/关闭逻辑 ---
  // 鼠标进入 ... 按钮：立即弹出菜单
  function handleMenuBtnEnter(e, sid) {
    hoverBtnId = sid
    cancelClose()
    openMenuAt(e, sid)
  }

  // 鼠标离开 ... 按钮：延迟关闭（给鼠标移入弹出菜单留出时间）
  function handleMenuBtnLeave() {
    hoverBtnId = null
    scheduleClose()
  }

  // 鼠标进入弹出菜单：保持稳定显示，取消关闭
  function handleMenuEnter() {
    if (menuOpenId !== null) hoverMenuId = menuOpenId
    cancelClose()
  }

  // 鼠标离开弹出菜单：延迟关闭
  function handleMenuLeave() {
    hoverMenuId = null
    scheduleClose()
  }

  // 仅当鼠标既不在 ... 按钮上也不在弹出菜单上时才真正关闭
  function scheduleClose() {
    if (closeTimer) clearTimeout(closeTimer)
    closeTimer = setTimeout(() => {
      closeTimer = null
      if (hoverBtnId === null && hoverMenuId === null) {
        menuOpenId = null
      }
    }, 150)
  }

  function cancelClose() {
    if (closeTimer) {
      clearTimeout(closeTimer)
      closeTimer = null
    }
  }

  // --- 菜单位置可见性保护 ---
  // 侧边栏靠近屏幕底部的会话条目（最近会话列表、或目录树末级分类下的列表都一样），
  // 菜单按触发点向下展开时底部条目会跑到视口外看不见。这里按菜单实测尺寸把 fixed
  // 坐标夹回视口内：放不下就整体上移；极端矮屏下再压缩行高并允许菜单内部滚动。
  async function clampMenuIntoView() {
    await tick()
    const el = menuElement
    if (!el || menuOpenId === null || typeof window === 'undefined') return

    const vpW = window.innerWidth
    const vpH = window.innerHeight
    let rect = el.getBoundingClientRect()

    // 整个菜单都比可视区高：切到压缩模式（更小的行高 + 内部滚动），再量一次
    const needCompact = rect.height > vpH - MENU_EDGE_MARGIN * 2
    if (needCompact !== menuConstrained) {
      menuConstrained = needCompact
      await tick()
      if (menuElement !== el || menuOpenId === null) return
      rect = el.getBoundingClientRect()
    }

    // 记下尺寸，供 ··· 子面板计算锚点用
    if (Math.abs(rect.width - menuSize.w) > 0.5 || Math.abs(rect.height - menuSize.h) > 0.5) {
      menuSize = { w: rect.width, h: rect.height }
    }

    let x = menuAnchor.x
    let y = menuAnchor.y
    // 右缘：放不下就左移（保持贴着触发点，除非会超出屏幕）
    if (x + rect.width + MENU_EDGE_MARGIN > vpW) x = vpW - rect.width - MENU_EDGE_MARGIN
    if (x < MENU_EDGE_MARGIN) x = MENU_EDGE_MARGIN
    // 底缘：这是「底部条目看不见」的主因——把菜单整体上移，保证底部可见
    if (y + rect.height + MENU_EDGE_MARGIN > vpH) y = vpH - rect.height - MENU_EDGE_MARGIN
    if (y < MENU_EDGE_MARGIN) y = MENU_EDGE_MARGIN

    // 只在变化明显时写入，避免与渲染来回触发造成抖动
    if (Math.abs(x - menuPos.x) > 0.5 || Math.abs(y - menuPos.y) > 0.5) {
      menuPos = { x, y }
    }
  }

  function resetMenuPlacement() {
    menuConstrained = false
    menuSize = { w: 0, h: 0 }
    submenuPlacement = { left: 0, top: 0, bottom: 0, up: false, maxHeight: SUBMENU_PREFERRED_MAX }
  }

  // ··· 子面板（用户消息 / 会话目录 / 执行分析）用 fixed 定位挂在菜单右侧：默认与菜单
  // 顶部对齐向下展开；下方空间不足时改为与菜单底部对齐向上展开，并把 max-height 压到
  // 剩余可用空间，保证面板底部不会跑到屏幕外。用 fixed 而不是 absolute，是为了在菜单
  // 进入内部滚动（压缩模式）时不被裁剪。
  function applySubmenuPlacement() {
    if (menuOpenId === null || typeof window === 'undefined' || menuSize.w === 0) return
    const vpH = window.innerHeight
    const menuTop = menuPos.y
    const menuBottom = menuPos.y + menuSize.h
    const below = vpH - menuTop - MENU_EDGE_MARGIN
    const above = menuBottom - MENU_EDGE_MARGIN
    const preferred = Math.min(SUBMENU_PREFERRED_MAX, vpH * 0.7)
    const up = below < SUBMENU_MIN_HEIGHT && above > below
    const placement = {
      left: menuPos.x + menuSize.w + 6,
      top: menuTop,
      bottom: vpH - menuBottom,
      up,
      maxHeight: Math.max(120, Math.min(preferred, up ? above : below)),
    }
    const prev = submenuPlacement
    if (
      Math.abs(placement.left - prev.left) > 0.5 ||
      Math.abs(placement.top - prev.top) > 0.5 ||
      Math.abs(placement.bottom - prev.bottom) > 0.5 ||
      placement.up !== prev.up ||
      Math.abs(placement.maxHeight - prev.maxHeight) > 0.5
    ) {
      submenuPlacement = placement
    }
  }

  function submenuStyle(width) {
    const { left, top, bottom, up, maxHeight } = submenuPlacement
    const vertical = up ? `bottom: ${bottom}px;` : `top: ${top}px;`
    return `left: ${left}px; width: ${width}px; ${vertical} max-height: ${maxHeight}px;`
  }

  function handleViewportResize() {
    if (menuOpenId === null) return
    applySubmenuPlacement()
    void clampMenuIntoView()
  }

  // 菜单内容变化（子面板展开、异步数据返回后条目变多）都会改变整体高度，
  // 需要重新夹取坐标。menuPos / menuConstrained 只被写出、不被读取，不会形成循环。
  $effect(() => {
    if (menuOpenId === null) return
    void menuAnchor.x
    void menuAnchor.y
    void userMessageMenuOpen
    void userMessageMenuLoading
    void userMessageMenuError
    void menuUserMessages.length
    void sessionDirectoryMenuOpen
    void sessionDirectoryMenuLoading
    void sessionDirectoryMenuError
    void menuSessionDirectories.length
    void sessionTerminalMenuOpen
    void sessionTerminalLoading
    void sessionTerminalError
    void sessionTerminalTarget
    void executionAnalysisOpen
    void executionAnalysisLoading
    void executionAnalysisError
    void executionAnalysisData
    void sessionFlagMenuOpen
    void sessionFlagMenuError
    void sessionLabelsMenuOpen
    void sessionLabelsMenuError
    void labelCandidates.length
    void clampMenuIntoView()
  })

  // 菜单位置变化（夹取 / 压缩）后，已展开子面板的可用空间要跟着重算
  $effect(() => {
    if (menuOpenId === null) return
    void menuPos.x
    void menuPos.y
    void menuConstrained
    void userMessageMenuOpen
    void sessionDirectoryMenuOpen
    void executionAnalysisOpen
    void sessionTerminalMenuOpen
    void sessionFlagMenuOpen
    void sessionLabelsMenuOpen
    applySubmenuPlacement()
  })

  function submenuPanelWidth(preferred = 600) {
    if (typeof window === 'undefined') return preferred
    const chatAreaWidth = Math.max(320, window.innerWidth - sidebarWidth.current)
    const availableWidth = Math.max(260, window.innerWidth - menuPos.x - 210)
    return Math.min(preferred, chatAreaWidth * 0.85, availableWidth)
  }

  function userMessageMenuWidth() {
    return submenuPanelWidth(600)
  }

  function formatDuration(ms) {
    const value = Number(ms) || 0
    if (value < 1000) return `${Math.round(value)} ms`
    if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`
    if (value < 3600000) {
      const minutes = Math.floor(value / 60000)
      const seconds = ((value % 60000) / 1000).toFixed(1)
      return `${minutes}m ${seconds}s`
    }
    const hours = Math.floor(value / 3600000)
    const minutes = Math.floor((value % 3600000) / 60000)
    const seconds = Math.floor((value % 60000) / 1000)
    return `${hours}h ${minutes}m ${seconds}s`
  }

  function analysisName(item, kind) {
    if (kind === 'agent') return item.agent_id || 'null'
    if (kind === 'model') return item.model_label ? `${item.model_id || 'null'} (${item.model_label})` : (item.model_id || 'null')
    return item.tool_name ? `${item.tool_name}${item.tool_id && item.tool_id !== item.tool_name ? ` (${item.tool_id})` : ''}` : (item.tool_id || 'null')
  }

  async function openExecutionAnalysis(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (executionAnalysisOpen) return
    applySubmenuPlacement()
    userMessageMenuOpen = false
    sessionDirectoryMenuOpen = false
    sessionTerminalMenuOpen = false
    sessionFlagMenuOpen = false
    sessionLabelsMenuOpen = false
    executionAnalysisOpen = true
    executionAnalysisLoading = true
    executionAnalysisError = ''
    executionAnalysisData = null
    const requestedSessionId = sessionId
    try {
      const data = await sessions.executionAnalysis(sessionId)
      if (menuOpenId !== requestedSessionId || !executionAnalysisOpen) return
      executionAnalysisData = data
    } catch (err) {
      if (menuOpenId === requestedSessionId) {
        executionAnalysisError = err.message || t('executionAnalysisFailed')
      }
    } finally {
      if (menuOpenId === requestedSessionId) executionAnalysisLoading = false
    }
  }

  function userMessageText(content) {
    if (typeof content === 'string') return content.replace(/\s+/g, ' ').trim()
    if (Array.isArray(content)) {
      return content.map(part => typeof part === 'string' ? part : (part?.text || '')).join(' ').replace(/\s+/g, ' ').trim()
    }
    return content == null ? '' : String(content).replace(/\s+/g, ' ').trim()
  }

  function findSessionDirectories(sessionId) {
    const matches = []
    function visit(nodes, pathIds = [], pathNames = []) {
      for (const node of nodes || []) {
        if (!node || typeof node !== 'object') continue
        const ids = [...pathIds, String(node.id)]
        const names = [...pathNames, node.name || String(node.id)]
        const childNodes = (node.children || []).filter(child => child && typeof child === 'object')
        if (childNodes.length === 0 && (node.children || []).includes(sessionId)) {
          matches.push({ path: ids.join('/'), names, label: names.join(' → ') })
        }
        visit(childNodes, ids, names)
      }
    }
    visit(categoryTree)
    return matches.sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: 'base' }))
  }

  async function openSessionDirectoryMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (sessionDirectoryMenuOpen) return
    applySubmenuPlacement()
    userMessageMenuOpen = false
    executionAnalysisOpen = false
    sessionTerminalMenuOpen = false
    sessionFlagMenuOpen = false
    sessionLabelsMenuOpen = false
    sessionDirectoryMenuOpen = true
    sessionDirectoryMenuLoading = true
    sessionDirectoryMenuError = ''
    menuSessionDirectories = []
    const requestedSessionId = sessionId
    await loadCategoryTree()
    if (menuOpenId !== requestedSessionId || !sessionDirectoryMenuOpen) return
    if (categoryTreeError) {
      sessionDirectoryMenuError = categoryTreeError
    } else {
      menuSessionDirectories = findSessionDirectories(sessionId)
      // 查不到且不在后台构建中：本地缓存树可能滞后于磁盘 tree.json，
      // 强制拉取一次（结果由 loadCategoryTree 回调自动同步到菜单）
      if (menuSessionDirectories.length === 0 && !categoryTreeBuilding) {
        await loadCategoryTree(true)
      }
    }
    sessionDirectoryMenuLoading = false
  }

  async function handleSessionDirectoryClick(e, directory) {
    e.stopPropagation()
    const targetPath = directory.path
    const targetSessionId = menuOpenId
    closeMenu()
    saveCurrentSessionView()
    if (categoryTreeElement) directoryTreeScrollTop = categoryTreeElement.scrollTop
    // 目标路径属于哪个森林根，就切到对应模式（recent 根已取消；旧树残留时
    // 退回主题模式）。
    const kind = modeOfCategoryPath(targetPath)
    viewMode = kind === 'recent' || kind === '' ? 'category' : kind
    modeSelections.set(viewMode, targetPath)
    searchOpen = false
    activeSearchQuery = ''
    await loadCategoryTree()
    expandCategoryAncestors(targetPath)
    selectedCategory = targetPath
    const restored = await restoreSessionView(categorySessionViews.get(targetPath))
    if (!restored) await loadSessions()
    await restoreDirectoryScroll()
    await scrollCategoryIntoView(targetPath)
    await scrollSessionIntoView(targetSessionId)
  }

  async function openUserMessageMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (userMessageMenuOpen) return
    applySubmenuPlacement()

    sessionDirectoryMenuOpen = false
    executionAnalysisOpen = false
    sessionTerminalMenuOpen = false
    sessionFlagMenuOpen = false
    sessionLabelsMenuOpen = false
    userMessageMenuOpen = true
    userMessageMenuLoading = true
    userMessageMenuError = ''
    menuUserMessages = []
    menuSessionData = null
    const requestedSessionId = sessionId
    try {
      const data = await sessions.get(sessionId)
      if (menuOpenId !== requestedSessionId || !userMessageMenuOpen) return
      const messages = (data.messages ?? []).map(m => ({ ...m }))
      menuSessionData = { messages, meta: data.meta ?? null }
      menuUserMessages = messages
        .map((message, index) => ({ index, text: userMessageText(message.content) }))
        .filter(item => messages[item.index]?.role === 'user')
    } catch (err) {
      if (menuOpenId === requestedSessionId) {
        userMessageMenuError = err.message || t('restoreSessionFailed')
      }
    } finally {
      if (menuOpenId === requestedSessionId) userMessageMenuLoading = false
    }
  }

  function handleUserMessageClick(e, sessionId, messageIndex) {
    e.stopPropagation()
    const data = menuSessionData
    if (!data) return

    if (currentSession.sessionId !== sessionId) {
      sessionRestore.pending = { sessionId, messages: data.messages, meta: data.meta }
    }
    messageScrollRequest.sessionId = sessionId
    messageScrollRequest.messageIndex = messageIndex
    messageScrollRequest.token++
    closeMenu()
    navigate('#/chat')
    if (window.innerWidth < 1024) collapseSidebar()
  }

  function handleOpenTerminal(e, sessionId) {
    e.stopPropagation()
    closeMenu()
    terminalOpen.sessionId = sessionId
    terminalOpen.token++
  }

  // 按会话执行环境（meta.remote_env，缺省本地）在终端根下定位环境节点；
  // 单环境森林只有根"本地终端"（无 env 子节点），此时根本身即本地目标。
  function locateSessionTerminalNode(envId) {
    return locateTerminalEnvNode(categoryTree, envId)
  }

  // 面板默认展开全部多级嵌套终端；单击可临时收起（不触碰主目录树的展开态）。
  const sessionTerminalExpandedPaths = $derived.by(() => {
    const target = sessionTerminalTarget
    const all = new Set()
    if (target) {
      const walk = (n, p) => {
        const kids = (n.children || []).filter(c => c && typeof c === 'object')
        if (kids.length) {
          all.add(p)
          for (const c of kids) walk(c, `${p}/${c.id}`)
        }
      }
      walk(target.node, target.path)
    }
    const collapsed = sessionTerminalCollapsed
    return new Set([...all].filter(p => !collapsed.has(p)))
  })

  function handleSessionTerminalToggle(path) {
    const next = new Set(sessionTerminalCollapsed)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    sessionTerminalCollapsed = next
  }

  async function openSessionTerminalMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (sessionTerminalMenuOpen) return
    applySubmenuPlacement()
    userMessageMenuOpen = false
    sessionDirectoryMenuOpen = false
    executionAnalysisOpen = false
    sessionFlagMenuOpen = false
    sessionLabelsMenuOpen = false
    sessionTerminalMenuOpen = true
    sessionTerminalLoading = true
    sessionTerminalError = ''
    sessionTerminalTarget = null
    sessionTerminalCollapsed = new Set()
    const requestedSessionId = sessionId
    try {
      await loadCategoryTree()
      const data = await sessions.get(sessionId)
      if (menuOpenId !== requestedSessionId || !sessionTerminalMenuOpen) return
      const envId = typeof data?.meta?.remote_env === 'string' ? data.meta.remote_env : ''
      sessionTerminalTarget = locateSessionTerminalNode(envId)
    } catch (err) {
      if (menuOpenId === requestedSessionId) sessionTerminalError = err.message || String(err)
    } finally {
      if (menuOpenId === requestedSessionId) sessionTerminalLoading = false
    }
  }

  // 面板内双击：保持原"打开该会话终端"语义，并携带环境 + 嵌套命令链，
  // ChatPage 对已有会话按 terminals-mux（env_id + session）路由并注入命令。
  function handleOpenSessionTerminalNode(node, path) {
    const sid = menuOpenId
    const target = collectTerminalTarget(path)
    const envId = target ? target.envId : ((node && node.env_id) || '')
    const commands = target ? target.commands : []
    closeMenu()
    if (!sid) return
    terminalOpen.sessionId = sid
    terminalOpen.remoteEnvId = envId
    terminalOpen.commands = commands
    terminalOpen.token++
  }

  // 双击目录树中的终端节点（"本地"或某个远程环境）：打开对应执行环境的终端。
  // 终端面板随会话显示，因此关联到全局新建会话 id 'new'（与聊天 wire 协议
  // reqBody.session_id='new' 及 ChatPage 的 'new' 终端映射一致）。节点携带的
  // env_id（remote_envs.json 登记的地址；本地节点为 ''）随 terminalOpen 传给
  // ChatPage，由其绑定草稿会话的执行环境；mux 按 env_id + session 路由通道。
  // 当前不在草稿会话（sessionId 非 null）时先请求新建；打开信号延迟一拍，
  // 确保 ChatPage 先完成切换、openTerminal 时目标会话已就绪并自动显示终端窗口。
  function handleOpenTerminalFromTree(node, path) {
    const target = collectTerminalTarget(path)
    const envId = target ? target.envId : ((node && node.env_id) || '')
    const commands = target ? target.commands : []
    if (currentSession.sessionId != null) newSessionRequest.token += 1
    setTimeout(() => {
      terminalOpen.sessionId = 'new'
      terminalOpen.remoteEnvId = envId
      terminalOpen.commands = commands
      terminalOpen.token++
    }, 0)
    navigate('#/chat')
    if (window.innerWidth < 1024) collapseSidebar()
  }

  // 沿 id 路径（如 '3/2/9'）在"终端"根内定位节点，返回 {envId, commands}：
  // commands 是从环境节点到该终端层沿途各层记录的进入指令，Terminal 连接后
  // 按序注入（terminalChain.js），逐层到达目标环境。
  function collectTerminalTarget(path) {
    const segments = String(path || '').split('/').map(Number)
    const root = categoryTree.find(n => n && n.type === 'terminal')
    if (!root || !segments.length) return null
    let current = root
    const chain = []
    for (const id of segments.slice(1)) {
      current = (current.children || []).find(
        (c) => c && typeof c === 'object' && c.id === id)
      if (!current) return null
      chain.push(current)
    }
    const layerCommands = (nodes) => nodes
      .filter((n) => n.type === 'terminal_layer' && n.command)
      .map((n) => n.command)
    if (!chain.length) {
      // 双击终端根本身：单环境森林的"本地终端"根即可连接的环境节点。
      return { envId: root.env_id || '', commands: [] }
    }
    if (chain[0].type === 'terminal') {
      return { envId: chain[0].env_id || '', commands: layerCommands(chain.slice(1)) }
    }
    // 终端层直接挂在终端根下（单环境形态）：根自身就是执行环境，
    // 整条 chain 都是要注入的进入指令。
    return { envId: root.env_id || '', commands: layerCommands(chain) }
  }

  // 终端环境节点/终端层节点右键：新建子终端（在环境或该层下再挂一层）、
  // 删除（仅终端层；环境节点来自注册配置，注销环境即从树上消失）。
  let terminalMenu = $state(null)
  let terminalLayerDialog = $state(null)

  function handleTerminalNodeContextmenu(node, path, event) {
    if (node?.type === 'label') { openLabelNodeMenu(node, event); return }
    const target = collectTerminalTarget(path)
    if (!target) return
    terminalMenu = {
      path,
      envId: target.envId,
      uid: node.type === 'terminal_layer' ? String(node.uid || '') : '',
      name: node.name || '',
      command: node.command || '',
      x: event.clientX,
      y: event.clientY,
    }
  }

  // 标签末级节点右键：生成概要和记忆 / 生成新会话。聚合文档存
  // DATA_DIR/labels/<label>.json（runtime/label_store.py），这里只触发与消费。
  let labelMenu = $state(null)      // { label, x, y }
  let labelAction = $state(null)    // 进行态 { label, kind: 'summarize'|'new' }
  let labelStatus = $state(null)    // 短暂结果提示 { text, error }
  let labelStatusTimer = null

  function openLabelNodeMenu(node, event) {
    labelMenu = { label: String(node.name || ''), x: event.clientX, y: event.clientY }
  }

  function closeLabelMenu() {
    if (labelMenu) labelMenu = null
  }

  function setLabelStatus(text, error = false) {
    labelStatus = { text, error }
    if (labelStatusTimer) clearTimeout(labelStatusTimer)
    labelStatusTimer = setTimeout(() => { labelStatus = null }, 5000)
  }

  // 生成概要和记忆：合并该标签全部会话的概要/记忆 + 众数配置统计。
  async function runLabelSummarize() {
    const m = labelMenu
    closeLabelMenu()
    if (!m || labelAction) return
    labelAction = { label: m.label, kind: 'summarize' }
    try {
      await sessions.summarizeLabel(m.label)
      setLabelStatus(t('labelSummarizeDone', { label: m.label }))
    } catch (err) {
      setLabelStatus(err?.message || t('labelSummarizeFailed'), true)
    } finally {
      labelAction = null
    }
  }

  // 生成新会话：取（或先生成）聚合文档，交给 ChatPage 消费——
  // 概要/记忆写入系统提示词，配置（环境/工作区/模型/工具/代理）套用到草稿。
  async function runLabelNewSession() {
    const m = labelMenu
    closeLabelMenu()
    if (!m || labelAction) return
    const label = m.label
    try {
      labelAction = { label, kind: 'new' }
      let doc = null
      try {
        doc = (await sessions.labelDoc(label))?.doc || null
      } catch (err) {
        doc = null
      }
      if (!doc || !(doc.summary || doc.memory)) {
        labelAction = { label, kind: 'summarize' }
        doc = (await sessions.summarizeLabel(label))?.doc || null
      }
      if (!doc) {
        setLabelStatus(t('labelSummarizeFailed'), true)
        return
      }
      labelSessionRequest.label = label
      labelSessionRequest.summary = doc.summary || ''
      labelSessionRequest.memory = doc.memory || ''
      labelSessionRequest.config = doc.config || null
      labelSessionRequest.token += 1
      navigate('#/chat')
      if (window.innerWidth < 1024) collapseSidebar()
    } catch (err) {
      setLabelStatus(err?.message || t('labelSummarizeFailed'), true)
    } finally {
      labelAction = null
    }
  }

  // 重命名标签：改名传播到携带该标签的全部会话（后端同步 index.json/tree.json
  // 与标签聚合文档）。前端只校验"单个标签名"这一条（与后端 clean_label 同规则），
  // 其余规则留在后端，避免两侧漂移。
  const LABEL_SEPARATOR_RE = /[,，;；\s]/
  let labelRenameDialog = $state(null)   // { from, to, error, busy }

  function openLabelRenameDialog() {
    const m = labelMenu
    closeLabelMenu()
    if (!m) return
    labelRenameDialog = { from: m.label, to: m.label, error: '', busy: false }
  }

  function closeLabelRenameDialog() {
    if (labelRenameDialog) labelRenameDialog = null
  }

  async function submitLabelRename() {
    const d = labelRenameDialog
    if (!d || d.busy) return
    const from = String(d.from || '').trim()
    const to = String(d.to || '').trim()
    if (!to || LABEL_SEPARATOR_RE.test(to)) {
      d.error = t('labelRenameInvalid')
      return
    }
    if (to === from) {
      // 同名 = 空操作，不打扰后端
      closeLabelRenameDialog()
      return
    }
    d.busy = true
    try {
      const res = await sessions.renameLabel(from, to)
      closeLabelRenameDialog()
      // 事实层里的标签立即改写：同一会话在任何分类下都同时显示新标签，不等刷新。
      renameLabelInFacts(from, to)
      setLabelStatus(t('labelRenameDone', { from, to, count: res?.session_count ?? 0 }))
      // 标签根与其下会话都变了：强制重建分类树，旧标签节点随之消失
      void loadCategoryTree(true)
    } catch (err) {
      d.error = err?.message || t('labelRenameFailed')
      d.busy = false
    }
  }

  function closeTerminalMenu() {
    if (terminalMenu) terminalMenu = null
  }

  function openTerminalLayerDialog() {
    const m = terminalMenu
    closeTerminalMenu()
    if (!m) return
    terminalLayerDialog = {
      mode: 'create',
      envId: m.envId,
      parentUid: m.uid,
      parentName: m.name,
      uid: '',
      command: '',
      name: '',
      error: '',
      busy: false,
    }
  }

  // 编辑已有终端层：预填当前进入指令与名称，uid 定位节点。
  function openTerminalLayerEditDialog() {
    const m = terminalMenu
    closeTerminalMenu()
    if (!m || !m.uid) return
    terminalLayerDialog = {
      mode: 'edit',
      envId: m.envId,
      parentUid: '',
      parentName: '',
      uid: m.uid,
      command: m.command,
      name: m.name,
      error: '',
      busy: false,
    }
  }

  function closeTerminalLayerDialog() {
    if (terminalLayerDialog && !terminalLayerDialog.busy) terminalLayerDialog = null
  }

  async function submitTerminalLayer() {
    const d = terminalLayerDialog
    if (!d || d.busy) return
    if (!d.command.trim()) { d.error = t('terminalLayerCommandRequired'); return }
    d.busy = true
    try {
      const payload = d.mode === 'edit'
        ? { env_id: d.envId, uid: d.uid, command: d.command.trim(), name: d.name.trim() }
        : { env_id: d.envId, command: d.command.trim(), name: d.name.trim(), parent_uid: d.parentUid }
      // env_id 原样带上（本地环境就是空串，不能省略：单环境形态下终端根自己
      // 就是本地环境节点）。走 api.js 而不是裸 fetch：口令保护的部署里 401 会
      // 先弹登录再重试，报错也取服务端的 message，而不是只剩 "HTTP 401"。
      if (d.mode === 'edit') await sessions.updateTerminalLayer(payload)
      else await sessions.addTerminalLayer(payload)
      terminalLayerDialog = null
      await loadCategoryTree(true)
    } catch (err) {
      d.error = err.message || String(err)
      d.busy = false
    }
  }

  async function deleteTerminalLayerFromMenu() {
    const m = terminalMenu
    closeTerminalMenu()
    if (!m || !m.uid) return
    if (!confirm(t('terminalLayerDeleteConfirm', { name: m.name }))) return
    try {
      await sessions.removeTerminalLayer({ env_id: m.envId, uid: m.uid })
      await loadCategoryTree(true)
    } catch (err) {
      // 另一端已经把它删掉了（404）：目的已达成，只需刷新树
      if (err?.status === 404) {
        await loadCategoryTree(true)
        return
      }
      categoryTreeError = err.message || String(err)
    }
  }

  // 打开会话日志目录：取本地（父端）log-dir 端点（conversation.json 所在目录），
  // 通知 ChatPage 显示文件管理器面板并导航。会话目录始终在本地——即使会话绑定
  // 远程环境（file journal 在子端）也打开本地会话目录。
  async function handleOpenSessionLogDir(e, sessionId) {
    e.stopPropagation()
    closeMenu()
    try {
      const data = await sessions.logDir(sessionId)
      if (!data?.path) throw new Error(t('openSessionLogDirFailed') || 'Failed to resolve log directory')
      openSessionLogDir.path = data.path
      // 子端 file journal 软链接（父端探测结果）：无 journal / 本地会话时为 null
      openSessionLogDir.remoteJournal = data.remote_journal || null
      openSessionLogDir.token++
    } catch (err) {
      restoreError = err.message || t('openSessionLogDirFailed')
    }
  }

  // 移动端长按手势：长按会话条目 500ms 后弹出操作菜单
  let longPressTimer = $state(null)

  function handleRowTouchStart(e, sid) {
    // 记录触摸位置，后续移动超过 10px 则取消长按（区分滚动）
    const touch = e.touches[0]
    longPressTimer = setTimeout(() => {
      longPressTimer = null
      // 在触摸位置弹出菜单
      menuAnchor = { x: touch.clientX, y: touch.clientY }
      menuPos = { ...menuAnchor }
      resetMenuPlacement()
      menuOpenId = sid
      void clampMenuIntoView()
    }, 500)
  }

  function handleRowTouchMove(e) {
    if (longPressTimer) {
      clearTimeout(longPressTimer)
      longPressTimer = null
    }
  }

  function handleRowTouchEnd(_e, _sid) {
    if (longPressTimer) {
      clearTimeout(longPressTimer)
      longPressTimer = null
    }
  }

  // --- 「设置标题」/「设置标签」两项末尾 ··· 的右侧子面板 ---------------------------
  // 复用「用户消息 / 所属目录 / 终端」那套 ··· 模式：入口是菜单项右侧的 ···，面板
  // fixed 挂在菜单右边，视口边缘的定位与向上翻转仍由 applySubmenuPlacement +
  // submenuStyle 计算（面板是 .session-dropdown 的 DOM 子节点，鼠标从 ··· 移到面板
  // 不会触发菜单的 mouseleave，所以不会中途关掉；键盘 Enter/Space 走 onclick 同样能开）。
  function openSessionFlagMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (!sessionId || menuOpenId !== sessionId || sessionFlagMenuOpen) return
    applySubmenuPlacement()
    userMessageMenuOpen = false
    sessionDirectoryMenuOpen = false
    sessionTerminalMenuOpen = false
    executionAnalysisOpen = false
    sessionLabelsMenuOpen = false
    sessionLabelsMenuError = ''
    sessionFlagMenuError = ''
    sessionFlagMenuOpen = true
  }

  async function openSessionLabelsMenu(e, sessionId) {
    e.stopPropagation()
    cancelClose()
    if (!sessionId || menuOpenId !== sessionId || sessionLabelsMenuOpen) return
    applySubmenuPlacement()
    userMessageMenuOpen = false
    sessionDirectoryMenuOpen = false
    sessionTerminalMenuOpen = false
    executionAnalysisOpen = false
    sessionFlagMenuOpen = false
    sessionFlagMenuError = ''
    sessionLabelsMenuError = ''
    sessionLabelsMenuOpen = true
    // 候选来自森林里的全部标签节点。用户可能从没打开过「所属目录」面板，那时 tree
    // 还没拉过 —— 这里按需补一次（非 force：有缓存就直接复用，不重复请求）。
    await loadCategoryTree()
    if (menuOpenId !== sessionId || !sessionLabelsMenuOpen) return
    if (categoryTreeError && categoryTree.length === 0) {
      // 候选一条都取不到时才插横幅说明原因（默默显示空面板会让用户以为「没有标签」）；
      // 树里已有候选时，不必为了一次失败的刷新在面板顶部挂红字。
      sessionLabelsMenuError = `${t('labelCandidatesLoadFailed')}: ${categoryTreeError}`
    }
  }

  // ··· 面板里要标的当前状态一律从共享事实投影出来的 sessionList 读：所以在别的分类
  // 改过、或 SSE 推过之后，面板里的选中态也跟着变。
  function menuSessionRow() {
    if (menuOpenId === null) return null
    return sessionList.find(s => s.session_id === menuOpenId) || null
  }
  function menuSessionFlag() {
    return sessionFlagLabel(menuSessionRow()?.flag)
  }
  function menuSessionLabelSet() {
    const labels = menuSessionRow()?.labels
    return new Set((Array.isArray(labels) ? labels : []).map(x => String(x).trim()))
  }

  // 点旗标 = 立刻落盘：先请求、成功之后才写共享事实（失败不乐观显示），写完 factsRev
  // 递增 → 所有分类下的同一条目和面板里的选中态一起更新。再点当前旗标不写盘，
  // 清除旗标只能选「无旗标」。
  async function handleSessionFlagPick(e, flag) {
    e.stopPropagation()
    const sid = menuOpenId
    if (!sid || sessionFlagMenuBusy) return
    if (sameFlag(menuSessionFlag(), flag)) return
    sessionFlagMenuBusy = true
    sessionFlagMenuError = ''
    try {
      const result = await sessions.setFlag(sid, flag)
      patchFacts(sid, { flag: typeof result?.flag === 'string' ? result.flag : flag }, 'user')
    } catch (err) {
      if (menuOpenId === sid) sessionFlagMenuError = err.message || t('sessionFlagMenuFailed')
    } finally {
      if (menuOpenId === sid) sessionFlagMenuBusy = false
    }
  }

  // 点候选 = 在当前会话的标签后面追加一个并落盘。setLabels 是「整组替换」语义，所以
  // 基准值必须取共享事实里的最新数组：连续点选不会把刚加上的那些丢掉（每次都是
  // 最新数组 + 新标签）。已持有的标签点击原样返回 → 既不重复也不删除，也不发请求；
  // 删除标签只能在「设置标签」对话框里改输入框再保存。
  async function handleSessionLabelPick(e, label) {
    e.stopPropagation()
    const sid = menuOpenId
    if (!sid || sessionLabelsMenuBusy) return
    const entry = sessionList.find(s => s.session_id === sid)
    const current = Array.isArray(entry?.labels) ? entry.labels : []
    const next = appendLabelToList(current, label)
    if (next.length === current.length) return
    sessionLabelsMenuBusy = true
    sessionLabelsMenuError = ''
    try {
      const result = await sessions.setLabels(sid, next)
      const saved = Array.isArray(result?.labels) ? result.labels : next
      patchFacts(sid, { labels: saved }, 'user')
      // 刷新森林（新标签立刻成为候选）+ 标签模式下的会话列表
      await refreshTreeAndLabelView()
    } catch (err) {
      if (menuOpenId === sid) sessionLabelsMenuError = err.message || t('setLabelsFailed')
    } finally {
      if (menuOpenId === sid) sessionLabelsMenuBusy = false
    }
  }

  // 「设置标题」：只改标题（留空确定 = 模型生成；非空确定 = 人工设定）。
  // 旗标不在这里改，它走上面「设置标题」项末尾 ··· 的旗标面板 + /flag。
  function handleOpenTitleDialog(e, sid) {
    e.stopPropagation()
    closeMenu()
    if (titleDialogOpen && titleDialogSid === sid) return
    const entry = sessionList.find(s => s.session_id === sid)
    const current = entry?.title
    titleDialogSid = sid
    titleDialogValue = current && current !== sid ? current : ''
    titleDialogError = ''
    titleDialogLoading = false
    titleDialogOpen = true
    tick().then(() => {
      if (titleDialogInput) {
        titleDialogInput.focus()
        titleDialogInput.select()
      }
    })
  }

  function closeTitleDialog() {
    if (titleDialogLoading) return
    titleDialogOpen = false
    titleDialogSid = null
    titleDialogError = ''
  }

  async function confirmTitleDialog() {
    if (titleDialogLoading) return
    const sid = titleDialogSid
    if (!sid) return
    const title = titleDialogValue.trim()
    titleDialogLoading = true
    titleDialogError = ''
    try {
      // labels 传 null：请求体不带 labels 键；旗标一概不碰（连响应回传的 flag 也不写
      // 回事实）—— 旗标只有 ··· 面板与 SSE 两个写入方，免得旧响应把新旗标退回旧值。
      const result = await sessions.generateTitle(sid, title, null)
      if (result && result.status === 'success') {
        const titleGiven = typeof result.title_given === 'boolean'
          ? result.title_given
          : Boolean(title)
        patchFacts(sid, {
          title: result.title,
          titleGiven,
          ...(Array.isArray(result.labels) ? { labels: result.labels } : {}),
        }, 'user')
        // 标签根挂载已在后端同步；刷新森林，并让标签模式下的会话列表即时对齐
        await refreshTreeAndLabelView()
      }
      titleDialogOpen = false
      titleDialogSid = null
    } catch (err) {
      titleDialogError = err.message || t('generateTitleFailed')
    } finally {
      titleDialogLoading = false
    }
  }

  function handleTitleDialogKeydown(e) {
    if (e.key === 'Enter') {
      e.preventDefault()
      confirmTitleDialog()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      closeTitleDialog()
    }
  }

  // 「设置标签」对话框：只有输入框 + 保存/取消（候选列表已挪到 ··· 面板，点选即追加）。
  // 保存 = 整组替换（走 /labels 专用接口，后端只写 labels 并同步树里所有分类副本），
  // 标题与旗标一概不动；要删标签就在输入框里删掉再保存。
  function handleOpenLabelsDialog(e, sid) {
    e.stopPropagation()
    closeMenu()
    if (labelsDialogOpen && labelsDialogSid === sid) return
    const entry = sessionList.find(s => s.session_id === sid)
    labelsDialogSid = sid
    labelsDialogValue = (entry?.labels || []).join(', ')
    labelsDialogError = ''
    labelsDialogLoading = false
    labelsDialogOpen = true
    tick().then(() => {
      if (labelsDialogInput) {
        labelsDialogInput.focus()
        labelsDialogInput.select()
      }
    })
  }

  function closeLabelsDialog() {
    if (labelsDialogLoading) return
    labelsDialogOpen = false
    labelsDialogSid = null
    labelsDialogValue = ''
    labelsDialogError = ''
  }

  function handleLabelsDialogKeydown(e) {
    if (e.key === 'Enter') {
      e.preventDefault()
      confirmLabelsDialog()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      closeLabelsDialog()
    }
  }

  async function confirmLabelsDialog() {
    if (labelsDialogLoading) return
    const sid = labelsDialogSid
    if (!sid) return
    // parseLabels：与模型/工具/AI代理标签输入完全相同的分隔与清洗语义
    const labels = parseLabels(labelsDialogValue)
    labelsDialogLoading = true
    labelsDialogError = ''
    try {
      const result = await sessions.setLabels(sid, labels)
      const nextLabels = Array.isArray(result?.labels) ? result.labels : labels
      // 只更新 labels：标题与旗标保持原样（后端这条路径也不会改它们）。走共享事实，
      // 于是标签模式里刚挂载/摘掉的根、以及其它分类下的同一条目都同步。
      patchFacts(sid, { labels: nextLabels }, 'user')
      await refreshTreeAndLabelView()
      labelsDialogOpen = false
      labelsDialogSid = null
      labelsDialogValue = ''
    } catch (err) {
      labelsDialogError = err.message || t('setLabelsFailed')
    } finally {
      labelsDialogLoading = false
    }
  }

  // 标题 / 标签写回后刷新森林，并让标签模式下的会话列表即时对齐
  async function refreshTreeAndLabelView() {
    await loadCategoryTree(true)
    if (viewMode === 'labels' && selectedCategory) {
      const labelsRootNow = findRootByKind(categoryTree, 'labels')
      const stillMounted = (labelsRootNow?.children || [])
        .some(c => c && String(c.id) === String(selectedCategory).split('/')[1])
      if (stillMounted) await loadSessions()
      else { selectedCategory = ''; clearSessionListState() }
    }
  }

  async function handleRegenerateSummary(e, sid) {
    e.stopPropagation()
    closeMenu()
    try {
      const result = await sessions.regenerateSummary(sid)
      if (result.status !== 'success') {
        restoreError = t('regenerateSummaryFailed')
      }
    } catch (err) {
      restoreError = err.message || t('regenerateSummaryFailed')
    }
  }

 async function handleDeleteSession(e, sid) {
    e.stopPropagation()
    closeMenu()
   // 优先展示会话标题，标题缺失或与 id 相同时回退为只显示 id
    const entry = sessionList.find(s => s.session_id === sid)
    const title = entry?.title
    const hasTitle = !!(title && title !== sid)
    if (!confirm(t(hasTitle ? 'confirmDeleteSession' : 'confirmDeleteSessionById', { title, id: sid }))) return
    try {
      await sessions.delete(sid)
      forgetSession(sid)
      // 通知 ChatPage 同步清空右侧面板
      if (currentSession.sessionId === sid) {
        sessionDeleted.sessionId = sid
      }
    } catch (err) {
      restoreError = err.message || t('deleteSessionFailed')
    }
  }

  // 拖动处理：记录起始点，用增量计算，避免按钮偏移导致宽度跳变
  function handleDragStart(e) {
    // 只响应鼠标左键
    if (e.type === 'mousedown' && e.button !== 0) return
    e.preventDefault()
    isDragging = false  // 先不标记，等真正移动再标记
    dragStartX = e.type.startsWith('touch') ? e.touches[0].clientX : e.clientX
    dragStartWidth = sidebarWidth.current
    document.addEventListener('mousemove', handleDragMove)
    document.addEventListener('mouseup', handleDragEnd)
    document.addEventListener('touchmove', handleDragMove, { passive: false })
    document.addEventListener('touchend', handleDragEnd)
  }

  function handleDragMove(e) {
    e.preventDefault()
    const clientX = e.type.startsWith('touch') ? e.touches[0].clientX : e.clientX
    const delta = clientX - dragStartX
    // 超过 3px 才认为是拖动，避免误触
    if (!isDragging && Math.abs(delta) < 3) return
    isDragging = true
    const newWidth = Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, dragStartWidth + delta))
    setSidebarWidth(newWidth)
  }

  function handleDragEnd() {
    document.removeEventListener('mousemove', handleDragMove)
    document.removeEventListener('mouseup', handleDragEnd)
    document.removeEventListener('touchmove', handleDragMove)
    document.removeEventListener('touchend', handleDragEnd)
    // 延迟重置 isDragging，让 click 事件能检测到
    setTimeout(() => { isDragging = false }, 0)
  }

  function handleToggleClick(e) {
    // 如果刚刚发生了拖动，不触发收缩切换
    if (isDragging) return
    toggleSidebarCollapsed()
  }

  function handleSetupClick(e) {
    // 不只依赖 <a href> 的默认 hash 跳转：
    // 1. 移动端侧边栏是 fixed 浮层，跳转后如果不收起，页面已切换但仍被侧边栏挡住，容易被感知为“点击无反应”；
    // 2. 当前已在 Setup 页时再次点击不会触发 hashchange，主动通知 SetupPage 回到默认入口。
    e.preventDefault()
    closeMenu()
    if (window.innerWidth < 1024) {
      collapseSidebar()
    }
    navigate('#/setup?tab=models')
    window.dispatchEvent(new CustomEvent('setup:reset'))
  }


  /**
   * 将 session_id（YYMMDD_HHmmss）解析为 "MM/DD HH:mm:ss" 格式的时间字符串。
   * 解析失败时返回空字符串。
   */
  function sessionIdToTime(sessionId) {
    const m = /^(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(sessionId)
    if (!m) return ''
    const [, , MM, DD, HH, mm, ss] = m
    return `${MM}/${DD} ${HH}:${mm}:${ss}`
  }

  /**
   * 计算会话条目的显示标题和 tooltip。
   * - 长度 < 10：display 追加时间字符串
   * - 长度 > 30：display 截断至 30 字符
   * - tooltip 始终为完整标题 + 时间（供 CSS ellipsis 和手动截断两种情况使用）
   * 返回 { display, tooltip }
   */
  function getSessionDisplay(entry) {
    const raw = entry.title || entry.session_id
    const time = sessionIdToTime(entry.session_id)
    // tooltip 始终携带完整内容，浏览器在文字被 CSS 截断时也会显示
    const tooltip = time ? `${raw}\n${time}` : raw
    let display = raw

    if (raw.length < 10) {
      display = time ? `${raw}  ${time}` : raw
    } else if (raw.length > 30) {
      display = raw.slice(0, 30) + '…'
    }
    return { display, tooltip }
  }

  // 监听新会话创建，动态添加到列表
  $effect(() => {
    const sid = newSessionCreated.sessionId
    if (sid) {
      const firstMsg = newSessionCreated.firstUserMessage
      const title = newSessionCreated.title
          || (firstMsg && firstMsg.trim() ? firstMsg.trim() : sid)
      const exists = sessionList.some(s => s.session_id === sid)
      if (!exists) {
        // 动态添加新会话条目到列表顶部。搜索过滤状态下仅当标题/首条消息命中时显示，
        // 避免破坏当前过滤结果；清空搜索后会正常显示所有会话。
        const searchable = `${title} ${firstMsg || ''} ${sid}`.toLowerCase()
        if (!(directoryOpen && selectedCategory) && (!activeSearchQuery || searchable.includes(activeSearchQuery.toLowerCase()))) {
          sessionRows = [{ session_id: sid, title }, ...sessionRows]
        }
      }
      // 标题走共享事实（条目已由 SSE streaming 事件插过占位行时也要补上真标题）。
      // 这是 ChatPage 的本地乐观值（首条用户消息），不是服务端事实：kind='local'
      // 保证它不会冒充"用户手填标题"去挡住后续服务端自动标题。
      patchFacts(sid, { title }, 'local')
      // 重置状态，避免重复处理
      newSessionCreated.sessionId = null
      newSessionCreated.firstUserMessage = null
      newSessionCreated.title = null
    }
  })

  // 衍生新会话：ChatPage 请求成功后在这里插入列表并选中。
  // 选中走的是 handleSessionClick 同一条加载路径，因此 ChatPage 的会话恢复
  // 逻辑只有一份；此后 ChatPage 的当前会话 ID 变成新会话，用户输入发往新 ID。
  $effect(() => {
    const sid = sessionForked.sessionId
    if (!sid) return
    const parentSid = sessionForked.parentSessionId
    const title = sessionForked.title || sid
    // 记下「衍生自哪条会话」，条目前显示一个来源标记（本次页面会话内有效）。
    if (parentSid) forkParents.set(sid, parentSid)
    if (!sessionList.some(s => s.session_id === sid)) {
      // 与新建会话同样的插入规则：只在「目录视图未启用」且（无搜索词或标题命中）
      // 时插入，避免破坏当前过滤结果。
      const searchable = `${title} ${sid}`.toLowerCase()
      if (!(directoryOpen && selectedCategory) && (!activeSearchQuery || searchable.includes(activeSearchQuery.toLowerCase()))) {
        sessionRows = [{ session_id: sid, title }, ...sessionRows]
      }
    }
    // 消费掉信号：失败也消费，避免反复重试同一个 fork 结果。
    // 衍生会话的标题由服务端写进 index.json（title_given=true），按权威事实记录。
    patchFacts(sid, { title, titleGiven: true }, 'user')
    sessionForked.sessionId = null
    sessionForked.parentSessionId = null
    sessionForked.title = null

    // 会话目录视图下新会话未必在当前过滤结果里，刷新列表兜底。
    if (directoryOpen && selectedCategory) {
      void loadCategoryTree(true)
    }
    // 选中新会话。加载失败（后端已建好目录但下载失败）时刷新列表，
    // 用户仍能从侧边栏点进去，不会丢会话。
    loadSessionIntoChat(sid).catch(() => {
      void loadSessions()
    })
  })
</script>

<!-- 点击空白处关闭菜单 -->
<svelte:window onclick={() => { closeMenu(); closeLabelMenu() }} onresize={handleViewportResize} />

<!-- 终端节点右键菜单：新建子终端 / 删除（级联）。fixed 渲染在 sidebar 外避免裁剪。 -->
{#if terminalMenu}
  <div
    class="terminal-node-menu"
    role="menu"
    style="left:{terminalMenu.x}px; top:{terminalMenu.y}px;"
    onclick={(e) => e.stopPropagation()}
  >
    <button class="terminal-node-menu-item" role="menuitem" onclick={openTerminalLayerDialog}>
      <span class="menu-emoji">🖥️</span>{t('terminalLayerNew')}
    </button>
    {#if terminalMenu.uid}
      <button class="terminal-node-menu-item" role="menuitem" onclick={openTerminalLayerEditDialog}>
        <span class="menu-emoji">✏️</span>{t('terminalLayerEdit')}
      </button>
      <button class="terminal-node-menu-item danger" role="menuitem" onclick={deleteTerminalLayerFromMenu}>
        <span class="menu-emoji">🗑️</span>{t('terminalLayerDelete')}
      </button>
    {/if}
  </div>
{/if}

<!-- 标签节点右键菜单：生成概要和记忆 / 生成新会话 -->
{#if labelMenu}
  <div
    class="terminal-node-menu"
    role="menu"
    style="left:{labelMenu.x}px; top:{labelMenu.y}px;"
    onclick={(e) => e.stopPropagation()}
  >
    <button class="terminal-node-menu-item" role="menuitem" onclick={runLabelSummarize}>
      <span class="menu-emoji">🧩</span>{t('labelSummarize')}
    </button>
    <button class="terminal-node-menu-item" role="menuitem" onclick={runLabelNewSession}>
      <span class="menu-emoji">✨</span>{t('labelNewSession')}
    </button>
    <!-- 菜单项就叫「重命名」（用户要求），对话框里才说明是「重命名标签」 -->
    <button class="terminal-node-menu-item" role="menuitem" onclick={openLabelRenameDialog}>
      <span class="menu-emoji">✏️</span>{t('rename')}
    </button>
  </div>
{/if}

<!-- 标签重命名对话框：改名传播到携带该标签的全部会话；聚合文档只在成员集合没变时
     搬迁，并入已有标签时旧标签与目标标签两边一起作废（下次发起会话自动重算） -->
{#if labelRenameDialog}
  <div class="terminal-layer-backdrop" onclick={closeLabelRenameDialog}>
    <div class="terminal-layer-dialog" role="dialog" aria-modal="true" onclick={(e) => e.stopPropagation()}>
      <div class="terminal-layer-title">{t('labelRenameTitle', { label: labelRenameDialog.from })}</div>
      <label class="terminal-layer-field">
        <span>{t('labelRename')}</span>
        <input
          type="text"
          bind:value={labelRenameDialog.to}
          placeholder={t('labelRenamePlaceholder')}
          disabled={labelRenameDialog.busy}
          onkeydown={(e) => { if (e.key === 'Enter') submitLabelRename(); if (e.key === 'Escape') closeLabelRenameDialog() }}
        />
      </label>
      <div class="terminal-layer-hint">{t('labelRenameHint')}</div>
      {#if labelRenameDialog.error}
        <div class="terminal-layer-error">{labelRenameDialog.error}</div>
      {/if}
      <div class="terminal-layer-actions">
        <button class="terminal-layer-btn" onclick={closeLabelRenameDialog} disabled={labelRenameDialog.busy}>{t('cancel')}</button>
        <button class="terminal-layer-btn primary" onclick={submitLabelRename} disabled={labelRenameDialog.busy}>{t('confirm')}</button>
      </div>
    </div>
  </div>
{/if}

<!-- 新建子终端对话框：记录进入指令（ssh/docker exec/wsl/psql…），名称可留空自动派生 -->
{#if terminalLayerDialog}
  <div class="terminal-layer-backdrop" onclick={closeTerminalLayerDialog}>
    <div class="terminal-layer-dialog" role="dialog" aria-modal="true" onclick={(e) => e.stopPropagation()}>
      <div class="terminal-layer-title">
        {terminalLayerDialog.mode === 'edit'
          ? `${t('terminalLayerEdit')} — ${terminalLayerDialog.name || terminalLayerDialog.command}`
          : `${t('terminalLayerNew')}${terminalLayerDialog.parentName ? ` — ${terminalLayerDialog.parentName}` : ''}`}
      </div>
      <label class="terminal-layer-field">
        <span>{t('terminalLayerCommandLabel')}</span>
        <input
          type="text"
          bind:value={terminalLayerDialog.command}
          placeholder={t('terminalLayerCommandPlaceholder')}
          disabled={terminalLayerDialog.busy}
          onkeydown={(e) => { if (e.key === 'Enter') submitTerminalLayer(); if (e.key === 'Escape') closeTerminalLayerDialog() }}
        />
      </label>
      <label class="terminal-layer-field">
        <span>{t('terminalLayerNameLabel')}</span>
        <input
          type="text"
          bind:value={terminalLayerDialog.name}
          placeholder={t('terminalLayerNamePlaceholder')}
          disabled={terminalLayerDialog.busy}
          onkeydown={(e) => { if (e.key === 'Enter') submitTerminalLayer(); if (e.key === 'Escape') closeTerminalLayerDialog() }}
        />
      </label>
      {#if terminalLayerDialog.error}
        <div class="terminal-layer-error">{terminalLayerDialog.error}</div>
      {/if}
      <div class="terminal-layer-actions">
        <button class="terminal-layer-btn" onclick={closeTerminalLayerDialog} disabled={terminalLayerDialog.busy}>{t('cancel')}</button>
        <button class="terminal-layer-btn primary" onclick={submitTerminalLayer} disabled={terminalLayerDialog.busy}>{t('confirm')}</button>
      </div>
    </div>
  </div>
{/if}

<!-- 浮层菜单：fixed 定位，渲染在 sidebar 之外确保不被裁剪 -->
{#if menuOpenId !== null}
  <div
    class="session-dropdown"
    class:constrained={menuConstrained}
    role="menu"
    bind:this={menuElement}
    style="left:{menuPos.x}px; top:{menuPos.y}px;"
    onclick={(e) => e.stopPropagation()}
    onmouseenter={handleMenuEnter}
    onmouseleave={handleMenuLeave}
  >
    <div class="session-dropdown-submenu-row" class:active={userMessageMenuOpen}>
      <div class="session-dropdown-item session-dropdown-submenu-label">
       <span class="menu-emoji">💬</span>
        <span>{t('userMessages')}</span>
      </div>
      <button
        class="submenu-more-btn"
        aria-label={t('userMessages')}
        aria-haspopup="menu"
        aria-expanded={userMessageMenuOpen}
        onclick={(e) => openUserMessageMenu(e, menuOpenId)}
        onmouseenter={(e) => openUserMessageMenu(e, menuOpenId)}
      >···</button>
    </div>
    {#if userMessageMenuOpen}
      <div
        class="user-message-submenu"
        role="menu"
        style={submenuStyle(userMessageMenuWidth())}
      >
        {#if userMessageMenuLoading}
          <div class="user-message-submenu-status">{t('loading')}</div>
        {:else if userMessageMenuError}
          <div class="user-message-submenu-status error">{userMessageMenuError}</div>
        {:else if menuUserMessages.length === 0}
          <div class="user-message-submenu-status">{t('noUserMessages')}</div>
        {:else}
          <div class="user-message-list">
            {#each menuUserMessages as item (item.index)}
              <button
                class="user-message-item"
                role="menuitem"
                title={item.text || t('emptyUserMessage')}
                onclick={(e) => handleUserMessageClick(e, menuOpenId, item.index)}
              >{item.text || t('emptyUserMessage')}</button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
    <div class="session-dropdown-submenu-row" class:active={sessionDirectoryMenuOpen}>
        <div class="session-dropdown-item session-dropdown-submenu-label">
          <span class="menu-emoji">🌲</span>
          <span>{t('sessionDirectories')}</span>
        </div>
        <button
          class="submenu-more-btn"
          aria-label={t('sessionDirectories')}
          aria-haspopup="menu"
          aria-expanded={sessionDirectoryMenuOpen}
          onclick={(e) => openSessionDirectoryMenu(e, menuOpenId)}
          onmouseenter={(e) => openSessionDirectoryMenu(e, menuOpenId)}
        >···</button>
      </div>
      {#if sessionDirectoryMenuOpen}
        <div
          class="session-directory-submenu"
          role="menu"
          style={submenuStyle(submenuPanelWidth(520))}
        >
          {#if sessionDirectoryMenuLoading}
            <div class="user-message-submenu-status">{t('loading')}</div>
          {:else if sessionDirectoryMenuError}
            <div class="user-message-submenu-status error">{sessionDirectoryMenuError}</div>
          {:else if menuSessionDirectories.length === 0}
            <div class="user-message-submenu-status">{t('noSessionDirectories')}</div>
          {:else}
            <div class="user-message-list">
              {#each menuSessionDirectories as directory (directory.path)}
                <button
                  class="user-message-item session-directory-item"
                  role="menuitem"
                  title={directory.label}
                  onclick={(e) => handleSessionDirectoryClick(e, directory)}
                >
                  {#each directory.names as name, index}
                    {#if index > 0}<span class="directory-path-arrow">→</span>{/if}
                    <span>{name}</span>
                  {/each}
                </button>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
    <!-- 「设置标题」「设置标签」各是一个 submenu-row：主区域（左边整块）点击仍打开
         原来的对话框，末尾 ··· 才展开右侧子面板 —— 交互/样式沿用「用户消息」「所属
         目录」那套：面板 fixed 挂在菜单右侧、视口边缘翻转复用 applySubmenuPlacement。
         两个 handler 各自 stopPropagation，主区域与 ··· 互不串事件。 -->
    <div class="session-dropdown-submenu-row" class:active={sessionFlagMenuOpen}>
      <button
        class="session-dropdown-item session-dropdown-action-label"
        role="menuitem"
        onclick={(e) => handleOpenTitleDialog(e, menuOpenId)}
      >
        <span class="menu-emoji">🖋️</span>
        {t('generateTitle')}
      </button>
      <button
        class="submenu-more-btn"
        aria-label={t('sessionFlagMoreAria')}
        aria-haspopup="menu"
        aria-expanded={sessionFlagMenuOpen}
        onclick={(e) => openSessionFlagMenu(e, menuOpenId)}
        onmouseenter={(e) => openSessionFlagMenu(e, menuOpenId)}
      >···</button>
    </div>
    {#if sessionFlagMenuOpen}
      <div
        class="user-message-submenu session-flag-submenu"
        role="menu"
        aria-label={t('sessionFlagMenuLabel')}
        style={submenuStyle(submenuPanelWidth(260))}
      >
        <p class="user-message-submenu-hint">{t('sessionFlagMenuHint')}</p>
        {#if sessionFlagMenuError}
          <div class="user-message-submenu-status error">{sessionFlagMenuError}</div>
        {/if}
        <div class="user-message-list session-flag-list" role="group" aria-label={t('sessionFlagMenuLabel')}>
          <button
            class="session-flag-item {menuSessionFlag() === '' ? 'active' : ''}"
            type="button"
            role="menuitemradio"
            aria-checked={menuSessionFlag() === ''}
            title={t('sessionFlagNone')}
            disabled={sessionFlagMenuBusy}
            onclick={(e) => handleSessionFlagPick(e, '')}
          >
            <span class="session-flag-item-label">{t('sessionFlagNone')}</span>
            {#if menuSessionFlag() === ''}<span class="menu-check">✓</span>{/if}
          </button>
          {#each SESSION_FLAGS as flag (flag)}
            <button
              class="session-flag-item {sameFlag(menuSessionFlag(), flag) ? 'active' : ''}"
              type="button"
              role="menuitemradio"
              aria-checked={sameFlag(menuSessionFlag(), flag)}
              title={t('sessionFlagTip')}
              disabled={sessionFlagMenuBusy}
              onclick={(e) => handleSessionFlagPick(e, flag)}
            >
              <span class="session-flag-item-emoji">{flag}</span>
              {#if sameFlag(menuSessionFlag(), flag)}<span class="menu-check">✓</span>{/if}
            </button>
          {/each}
        </div>
      </div>
    {/if}
    <div class="session-dropdown-submenu-row" class:active={sessionLabelsMenuOpen}>
      <button
        class="session-dropdown-item session-dropdown-action-label"
        role="menuitem"
        onclick={(e) => handleOpenLabelsDialog(e, menuOpenId)}
      >
        <span class="menu-emoji">🏷️</span>
        {t('sessionSetLabels')}
      </button>
      <button
        class="submenu-more-btn"
        aria-label={t('sessionLabelsMoreAria')}
        aria-haspopup="menu"
        aria-expanded={sessionLabelsMenuOpen}
        onclick={(e) => openSessionLabelsMenu(e, menuOpenId)}
        onmouseenter={(e) => openSessionLabelsMenu(e, menuOpenId)}
      >···</button>
    </div>
    {#if sessionLabelsMenuOpen}
      <div
        class="user-message-submenu session-label-submenu"
        role="menu"
        aria-label={t('sessionLabelsMenuLabel')}
        style={submenuStyle(submenuPanelWidth(320))}
      >
        <p class="user-message-submenu-hint">{t('sessionLabelsMenuHint')}</p>
        {#if sessionLabelsMenuError}
          <div class="user-message-submenu-status error">{sessionLabelsMenuError}</div>
        {/if}
        {#if labelCandidates.length === 0}
          <div class="user-message-submenu-status">
            {categoryTreeLoading ? t('loading') : t('labelCandidatesEmpty')}
          </div>
        {:else}
          <div class="user-message-list">
            {#each labelCandidates as label (label)}
              {@const held = menuSessionLabelSet().has(label)}
              <button
                class="user-message-item session-label-item {held ? 'active' : ''}"
                type="button"
                role="menuitemcheckbox"
                aria-checked={held}
                title={t('sessionLabelPickTitle')}
                disabled={sessionLabelsMenuBusy}
                onclick={(e) => handleSessionLabelPick(e, label)}
              >
                <span class="session-label-name">{label}</span>
                {#if held}<span class="menu-check">✓</span>{/if}
              </button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleRegenerateSummary(e, menuOpenId)}
    >
     <span class="menu-emoji">📝</span>
      {t('regenerateSummary')}
    </button>
    <div class="session-dropdown-submenu-row" class:active={sessionTerminalMenuOpen}>
      <div class="session-dropdown-item session-dropdown-submenu-label">
        <span class="menu-emoji">💻</span>
        <span>{t('openTerminal')}</span>
      </div>
      <button
        class="submenu-more-btn"
        aria-label={t('openTerminal')}
        aria-haspopup="menu"
        aria-expanded={sessionTerminalMenuOpen}
        onclick={(e) => openSessionTerminalMenu(e, menuOpenId)}
        onmouseenter={(e) => openSessionTerminalMenu(e, menuOpenId)}
      >···</button>
    </div>
    {#if sessionTerminalMenuOpen}
      <div
        class="session-directory-submenu session-terminal-submenu"
        role="menu"
        style={submenuStyle(submenuPanelWidth(420))}
      >
        {#if sessionTerminalLoading}
          <div class="user-message-submenu-status">{t('loading')}</div>
        {:else if sessionTerminalError}
          <div class="user-message-submenu-status error">{sessionTerminalError}</div>
        {:else if sessionTerminalTarget}
          <div class="user-message-list">
            <SessionCategoryNode
              node={{ ...sessionTerminalTarget.node, category: sessionTerminalTarget.path }}
              depth={0}
              expandedPaths={sessionTerminalExpandedPaths}
              selectedCategory=""
              onToggle={handleSessionTerminalToggle}
              onSelect={() => {}}
              onOpenTerminalNode={handleOpenSessionTerminalNode}
              onNodeContextmenu={(node, path, event) => {
                // 先关闭会话菜单与面板（避免面板对象指向已刷新的旧树），
                // 再弹终端层的新建/编辑/删除菜单；操作后树刷新，重开面板即最新。
                closeMenu()
                handleTerminalNodeContextmenu(node, path, event)
              }}
            />
          </div>
        {:else}
          <div class="user-message-submenu-status">{t('sessionTerminalPanelEmpty')}</div>
          <button
            class="session-dropdown-item"
            role="menuitem"
            onclick={(e) => handleOpenTerminal(e, menuOpenId)}
          >
            <span class="menu-emoji">💻</span>{t('sessionTerminalOpenDirect')}
          </button>
        {/if}
      </div>
    {/if}
    <div class="session-dropdown-submenu-row" class:active={executionAnalysisOpen}>
      <div class="session-dropdown-item session-dropdown-submenu-label">
      <span class="menu-emoji">📊</span>
        <span>{t('executionAnalysis')}</span>
      </div>
      <button
        class="submenu-more-btn"
        aria-label={t('executionAnalysis')}
        aria-haspopup="menu"
        aria-expanded={executionAnalysisOpen}
        onclick={(e) => openExecutionAnalysis(e, menuOpenId)}
        onmouseenter={(e) => openExecutionAnalysis(e, menuOpenId)}
      >···</button>
    </div>
    {#if executionAnalysisOpen}
      <div
        class="execution-analysis-submenu"
        role="menu"
        style={submenuStyle(submenuPanelWidth(520))}
      >
        {#if executionAnalysisLoading}
          <div class="user-message-submenu-status">{t('loading')}</div>
        {:else if executionAnalysisError}
          <div class="user-message-submenu-status error">{executionAnalysisError}</div>
        {:else if executionAnalysisData}
          <div class="execution-analysis-content">
            <div class="analysis-summary-list">
              <div class="analysis-summary-row"><span>{t('totalExecutionNetTime')}</span><strong>{formatDuration(executionAnalysisData.summary?.total_execution_net_ms)}</strong></div>
              <div class="analysis-summary-row"><span>{t('modelExecutionTotalTime')}</span><strong>{formatDuration(executionAnalysisData.summary?.model_execution_total_ms)}</strong></div>
              <div class="analysis-summary-row"><span>{t('toolExecutionTotalTime')}</span><strong>{formatDuration(executionAnalysisData.summary?.tool_execution_total_ms)}</strong></div>
            </div>
            <div class="analysis-section">
              <div class="analysis-section-title">{t('analysisByAgent')}</div>
              {#if (executionAnalysisData.by_agent || []).length === 0}
                <div class="analysis-empty">{t('analysisNoData')}</div>
              {:else}
                {#each executionAnalysisData.by_agent as item}
                  <div class="analysis-list-row">
                    <span class="analysis-name" title={analysisName(item, 'agent')}>{analysisName(item, 'agent')}</span>
                    <span class="analysis-meta">M {formatDuration(item.model_duration_ms)} · T {formatDuration(item.tool_duration_ms)}</span>
                    <strong>{formatDuration(item.total_duration_ms)}</strong>
                  </div>
                {/each}
              {/if}
            </div>
            <div class="analysis-section">
              <div class="analysis-section-title">{t('analysisByModel')}</div>
              {#if (executionAnalysisData.by_model || []).length === 0}
                <div class="analysis-empty">{t('analysisNoData')}</div>
              {:else}
                {#each executionAnalysisData.by_model as item}
                  <div class="analysis-list-row">
                    <span class="analysis-name" title={analysisName(item, 'model')}>{analysisName(item, 'model')}</span>
                    <span class="analysis-meta">{item.calls} {t('analysisCalls')} · {item.input_tokens}/{item.output_tokens} tok</span>
                    <strong>{formatDuration(item.duration_ms)}</strong>
                  </div>
                {/each}
              {/if}
            </div>
            <div class="analysis-section">
              <div class="analysis-section-title">{t('analysisByTool')}</div>
              {#if (executionAnalysisData.by_tool || []).length === 0}
                <div class="analysis-empty">{t('analysisNoData')}</div>
              {:else}
                {#each executionAnalysisData.by_tool as item}
                  <div class="analysis-list-row">
                    <span class="analysis-name" title={analysisName(item, 'tool')}>{analysisName(item, 'tool')}</span>
                    <span class="analysis-meta">{item.calls} {t('analysisCalls')}</span>
                    <strong>{formatDuration(item.duration_ms)}</strong>
                  </div>
                {/each}
              {/if}
            </div>
          </div>
        {/if}
      </div>
    {/if}
    <button
      class="session-dropdown-item"
      role="menuitem"
      onclick={(e) => handleOpenSessionLogDir(e, menuOpenId)}
    >
    <span class="menu-emoji">📂</span>
      {t('openSessionLogDir')}
    </button>
    <button
      class="session-dropdown-item session-dropdown-danger"
      role="menuitem"
      onclick={(e) => handleDeleteSession(e, menuOpenId)}
    >
    <span class="menu-emoji">🗑️</span>
      {t('deleteSession')}
    </button>
  </div>
{/if}

<!-- 标题设定/生成对话框 -->
{#if titleDialogOpen}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="title-dialog-overlay" onclick={(e) => { e.stopPropagation(); closeTitleDialog() }}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="title-dialog" role="dialog" aria-modal="true" onclick={(e) => e.stopPropagation()}>
      <h3 class="title-dialog-title">🔔 {t('generateTitle')}</h3>
      <p class="title-dialog-hint">{t('titleDialogHint')}</p>
      <input
        class="title-dialog-input"
        type="text"
        bind:this={titleDialogInput}
        bind:value={titleDialogValue}
        placeholder={t('titleDialogPlaceholder')}
        maxlength="100"
        onkeydown={handleTitleDialogKeydown}
      />

      {#if titleDialogError}
        <div class="title-dialog-error">{titleDialogError}</div>
      {/if}
      <div class="title-dialog-actions">
        <button class="title-dialog-btn title-dialog-cancel" disabled={titleDialogLoading} onclick={closeTitleDialog}>
          {t('cancel')}
        </button>
        <button class="title-dialog-btn title-dialog-confirm" disabled={titleDialogLoading} onclick={confirmTitleDialog}>
          {titleDialogLoading ? t('loading') : t('confirm')}
        </button>
      </div>
    </div>
  </div>
{/if}

<!-- 标签设定对话框：只有输入框 + 保存/取消。候选标签在「设置标签」项末尾 ···
     的悬停面板里点选即追加；这里保存 = 整组替换，删除标签只能改输入框后保存。 -->
{#if labelsDialogOpen}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="title-dialog-overlay" onclick={(e) => { e.stopPropagation(); closeLabelsDialog() }}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="title-dialog" role="dialog" aria-modal="true" onclick={(e) => e.stopPropagation()}>
      <h3 class="title-dialog-title">🏷️ {t('sessionSetLabels')}</h3>
      <p class="title-dialog-hint">{t('titleDialogLabelsHint')}</p>
      <input
        class="title-dialog-input"
        type="text"
        bind:this={labelsDialogInput}
        bind:value={labelsDialogValue}
        placeholder={t('titleDialogLabelsPlaceholder')}
        onkeydown={handleLabelsDialogKeydown}
      />

      {#if labelsDialogError}
        <div class="title-dialog-error">{labelsDialogError}</div>
      {/if}
      <div class="title-dialog-actions">
        <button class="title-dialog-btn title-dialog-cancel" disabled={labelsDialogLoading} onclick={closeLabelsDialog}>
          {t('cancel')}
        </button>
        <button class="title-dialog-btn title-dialog-confirm" disabled={labelsDialogLoading} onclick={confirmLabelsDialog}>
          {labelsDialogLoading ? t('loading') : t('confirm')}
        </button>
      </div>
    </div>
  </div>
{/if}

<aside class="sidebar" class:collapsed={sidebarWidth.collapsed} style="width: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px">
  <nav class="nav">
    <div class="nav-row">
      <button
        class="nav-action-btn new-session-top-btn"
        class:active={router.current === '#/chat' && !currentSession.sessionId}
        onclick={handleNewSession}
        title={t('newSession')}
        aria-label={t('newSession')}
      >
        <span>{t('nav_chat')}</span>
        <span class="plus-mark">✚</span>
      </button>
      <button
        class="nav-action-btn search-toggle-btn"
        class:active={searchOpen}
        onclick={toggleSearch}
        title={t('searchSessionsTooltip')}
        aria-label={t('searchSessions')}
      >🔍</button>
      <a
        href="#/setup?tab=models"
        class="nav-action-btn setup-top-btn"
        class:active={router.current.split('?')[0] === '#/setup'}
        onclick={handleSetupClick}
        title={t('nav_setup')}
        aria-label={t('nav_setup')}
      >⚙️</a>
    </div>
    {#if searchOpen}
      <input
        class="session-search-input"
        type="text"
        bind:this={searchInput}
        bind:value={searchText}
        onkeydown={handleSearchKeydown}
        placeholder={t('searchSessions')}
        title={t('searchSessionsTooltip')}
      />
    {/if}
  </nav>
  <!-- 会话面板：标题行是一组模式图标（最近/主题/历史/工作区 + 右侧终端），
       森林根节点不再进入树框；树框只渲染当前模式根的子级。 -->
  <div class="session-panel">
    <div class="session-panel-heading">
      <button
        class="mode-btn"
        class:active={viewMode === 'recent'}
        onclick={showRecentSessions}
        title={t('sessionModeRecent')}
        aria-label={t('sessionModeRecent')}
      ><span class="mode-glyph" aria-hidden="true">{'\u{1F550}\u{FE0F}'}</span></button>
      <button
        class="mode-btn"
        class:active={viewMode === 'category'}
        onclick={() => showModeState('category')}
        title={t('sessionModeCategory')}
        aria-label={t('sessionModeCategory')}
      ><span class="mode-glyph" aria-hidden="true">{'\u{1F4DA}'}</span></button>
      <button
        class="mode-btn"
        class:active={viewMode === 'history'}
        onclick={() => showModeState('history')}
        title={t('sessionModeHistory')}
        aria-label={t('sessionModeHistory')}
      ><span class="mode-glyph" aria-hidden="true">{'\u{1F4C5}'}</span></button>
      <button
        class="mode-btn"
        class:active={viewMode === 'workspace'}
        onclick={() => showModeState('workspace')}
        title={t('sessionModeWorkspace')}
        aria-label={t('sessionModeWorkspace')}
      ><span class="mode-glyph" aria-hidden="true">{'\u{1F5C2}\u{FE0F}'}</span></button>
      <button
        class="mode-btn"
        class:active={viewMode === 'labels'}
        onclick={() => showModeState('labels')}
        title={t('sessionModeLabels')}
        aria-label={t('sessionModeLabels')}
      ><span class="mode-glyph" aria-hidden="true">{'\u{1F3F7}\u{FE0F}'}</span></button>
      <button
        class="mode-btn mode-btn-terminal"
        class:active={viewMode === 'terminal'}
        onclick={() => showModeState('terminal')}
        title={t('sessionModeTerminal')}
        aria-label={t('sessionModeTerminal')}
      ><span class="mode-glyph" aria-hidden="true">{'\u{1F4BB}'}</span></button>
    </div>
    {#if labelAction || labelStatus}
      <div class="label-action-status" class:error={labelStatus?.error} role="status">
        {#if labelAction}
          <span class="category-tree-building-dot" aria-hidden="true"></span>
          {labelAction.kind === 'summarize' ? t('labelSummarizing') : t('labelPreparingNewSession')}「{labelAction.label}」
        {:else}
          {labelStatus.text}
        {/if}
      </div>
    {/if}
    {#if directoryOpen}
      <div class="category-tree" class:full={viewMode === 'terminal'} bind:this={categoryTreeElement} onscroll={(e) => { directoryTreeScrollTop = e.currentTarget.scrollTop }}>
        {#if categoryTreeBuilding}
          <div class="category-tree-building" role="status">
            <span class="category-tree-building-dot" aria-hidden="true"></span>
            {t('sessionTreeBuilding')}
          </div>
        {/if}
        {#if categoryTreeLoading && categoryTree.length === 0}
          <div class="session-loading">{t('loading')}</div>
        {:else if categoryTreeError && categoryTree.length === 0}
          <div class="session-error">{categoryTreeError}</div>
        {:else if treeTopNodes.length === 0 && !categoryTreeBuilding}
          <div class="session-empty">{t('sessionDirectoryEmpty')}</div>
        {:else}
          {#each treeTopNodes as node (node.category)}
            <SessionCategoryNode
              node={node}
              expandedPaths={categoryExpandedPaths}
              {selectedCategory}
              onToggle={toggleCategoryPath}
              onSelect={selectCategory}
              onOpenTerminalNode={handleOpenTerminalFromTree}
              onNodeContextmenu={handleTerminalNodeContextmenu}
            />
          {/each}
        {/if}
      </div>
    {/if}
    {#if !directoryOpen || (selectedCategory && viewMode !== 'terminal')}
      <div class="session-list" bind:this={sessionListElement} onscroll={handleSessionScroll}>
        {#if sessionLoading && sessionList.length === 0}
          <div class="session-loading">{t('loading')}</div>
        {:else if sessionError && sessionList.length === 0}
          <div class="session-error">{sessionError}</div>
        {:else if sessionList.length === 0}
          <div class="session-empty">{t('noSessions')}</div>
        {:else}
          {#each sessionList as entry (entry.session_id)}
            <div
              class="session-row"
              data-session-id={entry.session_id}
              ontouchstart={(e) => handleRowTouchStart(e, entry.session_id)}
              ontouchmove={handleRowTouchMove}
              ontouchend={(e) => handleRowTouchEnd(e, entry.session_id)}
            >
              <button
                class="session-item {getSessionStatusClass(entry)}"
                class:active={entry.session_id === currentSession.sessionId}
                onclick={() => handleSessionClick(entry.session_id)}
                ontouchend={(e) => { e.preventDefault(); handleSessionClick(entry.session_id) }}
                title={getSessionDisplay(entry).tooltip}
              >
                {#if forkParents.has(entry.session_id)}
                  <!-- 衍生出来的会话：悬停可以看到它是从哪条会话复制出来的 -->
                  <span class="session-fork" title={t('forkSource').replace('{id}', forkParents.get(entry.session_id))}>⑂</span>
                {/if}
                {#if sessionFlagLabel(entry.flag)}
                  <!-- 旗标读的是 index.json 里的会话级字段：会话在几个分类下都有副本
                       也只有一份，所以切换分类 / 刷新页面显示一致 -->
                  <span class="session-row-flag" title={t('sessionFlagTip')}>{sessionFlagLabel(entry.flag)}</span>
                {/if}
                {getSessionDisplay(entry).display}
                {#if awaitingUserAnswer.sessionIds.includes(entry.session_id)}
                  <span class="session-awaiting" title={t('awaitingUserAnswer')}>❓</span>
                {/if}
              </button>
              <button
                class="session-menu-btn"
                onclick={(e) => openMenu(e, entry.session_id)}
                onmouseenter={(e) => handleMenuBtnEnter(e, entry.session_id)}
                onmouseleave={handleMenuBtnLeave}
                aria-label={t('deleteSession')}
              >···</button>
            </div>
          {/each}
          {#if sessionLoadingMore}
            <div class="session-loading session-loading-more">{t('loading')}</div>
          {/if}
          {#if sessionError}
            <div class="session-error">{sessionError}</div>
          {/if}
        {/if}
        {#if restoreError}
          <div class="session-error">{restoreError}</div>
        {/if}
      </div>
    {/if}
  </div>
</aside>

<!-- 统一的收缩/展开按钮，固定在侧边栏右边缘顶部，与主内容顶栏重叠 -->
<button
  class="sidebar-toggle-btn"
  style="left: {sidebarWidth.collapsed ? 0 : sidebarWidth.current}px;"
  onclick={handleToggleClick}
  onmousedown={handleDragStart}
  ontouchstart={handleDragStart}
  aria-label={sidebarWidth.collapsed ? t('expandSidebar') : t('collapseSidebar')}
  title={sidebarWidth.collapsed ? t('expandSidebar') : t('collapseSidebar')}
>
  {#if sidebarWidth.collapsed}
    <svg class="toggle-arrow" viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="1,1 7,7 1,13"/>
    </svg>
  {:else}
    <svg class="toggle-arrow" viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="7,1 1,7 7,13"/>
    </svg>
  {/if}
</button>

<style>
  .sidebar {
    height: 100vh;
    height: 100dvh; /* 移动端动态视口高度，自动适配浏览器地址栏显隐 */
    overflow: hidden;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    transition: width 0.2s ease;
  }
  .sidebar.collapsed {
    border-right: none;
    overflow: hidden;
  }
  .nav {
    display: flex;
    flex-direction: column;
    padding: 12px 10px 8px 10px;
    flex-shrink: 0;
  }
  .nav-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .nav-action-btn {
    min-width: 32px;
    height: 32px;
    padding: 0 9px;
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    line-height: 1;
    font-size: 0.9rem;
    font-weight: 600;
    white-space: nowrap;
    transition: border-color 0.15s, color 0.15s;
  }
  .nav-action-btn:hover {
    background: transparent;
    color: var(--text);
  }
  .nav-action-btn.active {
    background: transparent;
    border-color: rgba(59, 130, 246, 0.45);
    color: var(--text);
  }
  .new-session-top-btn {
    flex: 0 0 auto;
  }
  .plus-mark {
    font-size: 1rem;
  }
  .search-toggle-btn {
    margin-left: auto;
    font-size: 0.95rem;
  }
  .setup-top-btn {
    text-decoration: none;
    font-size: 0.95rem;
  }
  .session-search-input {
    width: 100%;
    box-sizing: border-box;
    margin: 8px 0 0 0;
    padding: 7px 9px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
  }
  .session-search-input:focus {
    outline: none;
    border-color: var(--primary);
  }
  .sidebar-toggle-btn {
    position: fixed;
    top: 12px;
    /* left 由 style 属性动态设置 */
    width: fit-content;
    min-width: 0;
    height: 32px;
    padding: 0 2px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-left: none;
    border-radius: 0 6px 6px 0;
    cursor: ew-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 180;
    transition: background-color 0.15s, left 0.2s ease;
  }
  .sidebar-toggle-btn:hover {
    background: var(--border);
  }
  .toggle-arrow {
    width: 8px;
    height: 14px;
    color: var(--text-secondary);
    display: block;
    pointer-events: none;
  }
  .session-panel {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .session-panel-heading {
    min-height: 28px;
    display: flex;
    align-items: stretch;
    flex-shrink: 0;
  }
  /* 模式切换行：森林根折成一行图标，不占树框高度，也不占水平宽度。 */
  .mode-btn {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    padding: 4px 6px;
    border: none;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
    opacity: 0.55;
    transition: background-color 0.15s, color 0.15s, opacity 0.15s;
  }
  .mode-glyph {
    font-size: 0.92rem;
    line-height: 1;
  }
  .mode-btn:hover { color: var(--text); opacity: 1; }
  .mode-btn.active { color: var(--text); opacity: 1; }
  .mode-btn-terminal { margin-left: auto; }
  /* 标签聚合动作的短暂状态条（生成概要和记忆 / 生成新会话）。 */
  .label-action-status {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    font-size: 0.76rem;
    color: var(--text-secondary);
    flex-shrink: 0;
  }
  .label-action-status.error { color: var(--danger, #d33); }
  .category-tree {
    /* 分类模式：树框约占侧边栏有效高度的三分之一，其余给会话列表。 */
    max-height: 34%;
    min-height: 0;
    overflow-y: auto;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    padding: 3px 0;
    flex-shrink: 1;
  }
  /* 终端模式没有会话列表，终端树整体列表占满剩余高度。 */
  .category-tree.full {
    flex: 1 1 auto;
    max-height: none;
  }
  .category-tree-building {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 0 6px 10px;
    font-size: 0.8rem;
    color: var(--text-secondary);
    flex-shrink: 0;
  }
  .category-tree-building-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent, #4c9aff);
    animation: category-tree-pulse 1.2s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes category-tree-pulse {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 1; }
  }
  .session-list {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .session-row {
    position: relative;
    display: flex;
    align-items: center;
    overflow: hidden;
    flex-shrink: 0; /* 禁止条目高度被压缩，确保超出时触发滚动 */
  }
  .session-item {
    flex: 1;
    min-width: 0;
    padding: 7px 6px 7px 10px;
    text-align: left;
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: 0.82rem;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: background-color 0.15s, color 0.15s;
  }
  .session-item:hover {
    background-color: var(--border);
    color: var(--text);
  }
  .session-item.active {
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  /* Session Status Stream indicator styles */
  .session-item.status-streaming {
    color: var(--warning);
    font-style: italic;
  }
  .session-item.status-done-success-unread {
    color: var(--success);
    font-weight: 600;
  }
  .session-item.status-done-error-unread {
    color: var(--danger, #e53e3e);
    font-weight: 600;
  }
  /* 暂停中的会话：黄色加粗、非斜体（streaming 是橙色斜体），
     保证在列表里一眼可见、不会和"结束已读"混淆。 */
  .session-item.status-paused {
    color: var(--paused);
    font-weight: 600;
  }
  /* 等待客户端工具回传：蓝色加粗非斜体。既不是「推理中」（橙色斜体），也不是
     「成功结束未读」（绿色）——这一轮并没有结束，只是把动作交给了浏览器执行，
     结果回传后同一轮继续；后端也会拒绝 done_success_unread 抹掉这个状态。 */
  .session-item.status-client-tool {
    color: var(--primary);
    font-weight: 600;
  }
  .session-menu-btn {
    position: absolute;
    right: 6px;
    width: 26px;
    height: 26px;
    padding: 0;
    background: none;
    border: none;
    border-radius: 5px;
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    opacity: 0;
    pointer-events: none; /* 默认禁止交互，防止移动端误触吞掉点击事件 */
    transition: opacity 0.15s, background-color 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .session-row:hover .session-menu-btn {
    opacity: 1;
    pointer-events: auto; /* 桌面端 hover 时才恢复交互能力 */
  }
  .session-menu-btn:hover {
    background-color: var(--border);
    color: var(--text);
  }
  .session-dropdown {
    position: fixed;
    z-index: 9999;
    min-width: 180px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
    padding: 4px 0;
    overflow: visible;
  }
  /* 极端矮屏：菜单整体比可视区还高时压缩行高并允许内部滚动，
     这样「删除会话」这类最下面的条目仍然够得着 */
  .session-dropdown.constrained {
    max-height: calc(100vh - 16px);
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .session-dropdown.constrained .session-dropdown-item {
    padding: 5px 14px;
    font-size: 0.82rem;
  }
  .session-dropdown.constrained .submenu-more-btn {
    height: 22px;
  }
  .session-dropdown-item {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 16px;
    text-align: left;
    background: none;
    border: none;
    font-size: 0.88rem;
    cursor: pointer;
    transition: background-color 0.15s;
    color: var(--text);
  }
  .session-dropdown-item:hover,
  .session-dropdown-item.active {
    background-color: var(--border);
  }
  .session-dropdown-submenu-row {
    position: relative;
    display: flex;
    align-items: center;
  }
  .session-dropdown-submenu-row:hover,
  .session-dropdown-submenu-row.active {
    background-color: var(--border);
  }
  .session-dropdown-submenu-label {
    flex: 1;
    min-width: 0;
    padding-right: 4px;
    cursor: default;
  }
  .session-dropdown-submenu-label:hover {
    background: transparent;
  }
  .session-dropdown-submenu-label span {
    min-width: 0;
  }
  .submenu-more-btn {
    flex-shrink: 0;
    width: 30px;
    height: 26px;
    margin-right: 6px;
    padding: 0;
    border: none;
    border-radius: 5px;
    background: transparent;
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
  }
  .submenu-more-btn:hover {
    background: var(--bg);
    color: var(--text);
  }
  .user-message-submenu,
  .session-directory-submenu,
  .execution-analysis-submenu {
    /* fixed：锚点（left/top 或 bottom）与 max-height 由 JS 按可视区算好，
       既不会被菜单容器的滚动裁剪，也能在贴近屏幕底部时向上展开 */
    position: fixed;
    z-index: 10000;
    max-height: min(70vh, 620px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
  }
  .execution-analysis-content {
    min-height: 0;
    overflow-y: auto;
    padding: 10px 12px 12px;
  }
  .analysis-summary-list {
    display: grid;
    gap: 7px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .analysis-summary-row,
  .analysis-list-row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    font-size: 0.82rem;
  }
  .analysis-summary-row span {
    flex: 1;
    color: var(--text-secondary);
  }
  .analysis-summary-row strong,
  .analysis-list-row strong {
    flex-shrink: 0;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .analysis-section {
    padding-top: 11px;
  }
  .analysis-section-title {
    margin-bottom: 6px;
    color: var(--text-secondary);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .analysis-list-row {
    padding: 5px 0;
  }
  .analysis-name {
    flex: 1 1 42%;
    min-width: 0;
    overflow: hidden;
    color: var(--text);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .analysis-meta {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-secondary);
    font-size: 0.75rem;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .analysis-empty {
    padding: 5px 0;
    color: var(--text-secondary);
    font-size: 0.8rem;
  }
  .user-message-list {
    min-height: 0;
    overflow-y: auto;
    padding: 4px 0;
  }
  .user-message-item {
    display: block;
    width: 100%;
    padding: 8px 12px;
    overflow: hidden;
    border: none;
    background: none;
    color: var(--text);
    font: inherit;
    font-size: 0.84rem;
    line-height: 1.25;
    text-align: left;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: pointer;
  }
  .user-message-item:hover {
    background: var(--border);
  }
  .session-directory-item {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .session-directory-item span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .directory-path-arrow {
    flex-shrink: 0;
    color: var(--text-secondary);
  }
  .user-message-submenu-status {
    padding: 14px 12px;
    color: var(--text-secondary);
    font-size: 0.82rem;
  }
  .user-message-submenu-status.error {
    color: var(--danger, #e53e3e);
  }
  .session-dropdown-danger {
    color: var(--danger, #e53e3e);
  }
  .session-dropdown-danger:hover {
    background-color: rgba(229, 62, 62, 0.08);
  }
 .menu-check { margin-left: auto; font-weight: 700; color: #22c55e; }
  /* 这条会话有一个未回答的 AskUser：切走页面时也需要看得见，所以标记要显眼 */
  .session-awaiting {
    margin-left: .35rem;
    font-size: .9em;
    vertical-align: -0.1em;
    color: #d97706;
    animation: awaiting-pulse 1.6s ease-in-out infinite;
  }
  @keyframes awaiting-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  /* 衍生会话的来源标记：放在标题前，悬停显示「衍生自 …」 */
  .session-fork { margin-right: .3rem; font-size: .9em; opacity: .65; vertical-align: -0.1em; }
  /* 会话旗标（index.json 的 flag 字段，单选封闭枚举）：一份数据，多处显示一致 */
  .session-row-flag { margin-right: .35rem; font-size: .9em; vertical-align: -0.05em; }

  .menu-emoji {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    flex-shrink: 0;
    font-size: 14px;
    line-height: 1;
  }
  .session-loading, .session-empty, .session-error {
    padding: 6px 0 6px 10px;
    font-size: 0.82rem;
    color: var(--text-secondary);
    flex-shrink: 0;
  }
  .session-error { color: var(--danger); }
  
  /* 滚动条样式：默认隐藏，悬停时显示 */
  .session-list::-webkit-scrollbar {
    width: 8px;
  }
  .session-list::-webkit-scrollbar-track {
    background: transparent;
  }
  .session-list::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 4px;
    transition: background 0.2s;
  }
  .session-list:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
  .session-list:hover::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }

  /* 标题设定/生成对话框 */
  .title-dialog-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
  }
  .title-dialog {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px 24px;
    width: min(440px, 90vw);
  }
  .title-dialog-title {
    margin: 0 0 8px;
    font-size: 1.05rem;
    color: var(--text);
  }
  .title-dialog-hint {
    margin: 0 0 12px;
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }
  .title-dialog-input {
    width: 100%;
    box-sizing: border-box;
    padding: 8px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 0.9rem;
    outline: none;
  }
  .title-dialog-input:focus {
    border-color: var(--text-secondary);
  }
  /* 「设置标题」项末尾 ··· 的旗标面板：竖向列表，点选即落盘（不再经过对话框） */
  .session-dropdown-submenu-row > .session-dropdown-item.session-dropdown-action-label {
    flex: 1;
    min-width: 0;
  }
  .user-message-submenu-hint {
    flex-shrink: 0;
    margin: 0;
    padding: 10px 12px 8px;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
    font-size: 0.75rem;
    line-height: 1.4;
  }
  .session-flag-list {
    display: flex;
    flex-direction: column;
  }
  .session-flag-item {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 7px 12px;
    border: none;
    background: none;
    color: var(--text);
    font: inherit;
    font-size: 0.86rem;
    text-align: left;
    cursor: pointer;
  }
  .session-flag-item:hover { background: var(--border); }
  .session-flag-item.active {
    background: var(--bg-hover);
    box-shadow: inset 2px 0 0 var(--accent, var(--text-secondary));
  }
  .session-flag-item:disabled { cursor: default; opacity: .55; }
  .session-flag-item-emoji { font-size: 1.15rem; line-height: 1.2; }
  /* 「设置标签」项末尾 ··· 的标签候选面板：竖向列表，点选即追加；已持有的打勾 */
  .session-label-item {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .session-label-item.active {
    background: var(--bg-hover);
    box-shadow: inset 2px 0 0 var(--accent, var(--text-secondary));
  }
  .session-label-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .session-label-item:disabled { cursor: default; opacity: .55; }
  .title-dialog-error {
    margin-top: 10px;
    font-size: 0.8rem;
    color: var(--danger);
  }
  .title-dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    margin-top: 16px;
  }
  .title-dialog-btn {
    padding: 7px 18px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    font-size: 0.9rem;
  }
  .title-dialog-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .title-dialog-cancel {
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .title-dialog-confirm {
    background: var(--danger);
    color: #fff;
  }

  /* 终端节点右键菜单与新建子终端对话框 */
  .terminal-node-menu {
    position: fixed;
    z-index: 1200;
    min-width: 160px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.25);
    padding: 4px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .terminal-node-menu-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 10px;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    border-radius: 6px;
    font-size: 0.82rem;
    text-align: left;
    cursor: pointer;
  }
  .terminal-node-menu-item:hover { background: var(--border); color: var(--text); }
  .terminal-node-menu-item.danger { color: #c0392b; }
  .terminal-node-menu-item.danger:hover { background: rgba(192, 57, 43, 0.12); }
  .terminal-layer-backdrop {
    position: fixed;
    inset: 0;
    z-index: 1300;
    background: rgba(0, 0, 0, 0.4);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .terminal-layer-dialog {
    width: min(460px, calc(100vw - 40px));
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  }
  .terminal-layer-title { font-weight: 600; color: var(--text); font-size: 0.95rem; }
  .terminal-layer-field { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; color: var(--text-secondary); }
  .terminal-layer-field input {
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary, var(--bg));
    color: var(--text);
    font-family: 'courier-new', courier, monospace;
    font-size: 0.85rem;
  }
  .terminal-layer-error { color: #c0392b; font-size: 0.8rem; }
  /* 对话框里的辅助说明（标签重命名等）：次要文字，不参与提交 */
  .terminal-layer-hint { color: var(--text-secondary); font-size: 0.75rem; line-height: 1.45; }
  .terminal-layer-actions { display: flex; justify-content: flex-end; gap: 8px; }
  .terminal-layer-btn {
    padding: 7px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: transparent;
    color: var(--text-secondary);
    font-size: 0.82rem;
    cursor: pointer;
  }
  .terminal-layer-btn.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
  .terminal-layer-btn:disabled { opacity: 0.6; cursor: default; }
</style>
