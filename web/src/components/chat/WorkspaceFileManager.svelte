<script>
  import { tick } from 'svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { workspace as workspaceApi } from '../../lib/api.js'
  import { marked } from 'marked'
  import { highlight, escapeHtml, getFileLang, isMarkdownFile } from '../../lib/highlight.js'
  import { copyToClipboard } from '../../lib/clipboard.js'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import DocumentPreview from './DocumentPreview.svelte'

  /**
   * 工作区文件管理器面板
   * 
   * Props:
   * - open: 面板是否打开
   * - workspacePath: 当前工作区路径
   * - onWorkspaceChange(path): 工作区路径变更回调
   * - onSelectFiles(files): 选择文件回调
   * - onClose(): 关闭面板回调
   */
  let { 
    open = $bindable(false),
    workspacePath = $bindable(''),
    navigateTarget = $bindable(null),
    onWorkspaceChange,
    onSelectFiles,
    onClose
  } = $props()

  const SORT_TIME_DESC_STORAGE_KEY = 'workspace_file_manager_sort_time_desc'
  const NAME_FILTER_STORAGE_KEY = 'workspace_file_manager_name_filter'

  function readLocalStorage(key, fallback = '') {
    try {
      if (typeof localStorage === 'undefined') return fallback
      return localStorage.getItem(key) ?? fallback
    } catch {
      return fallback
    }
  }

  function writeLocalStorage(key, value) {
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(key, value)
      }
    } catch {}
  }

  // 视图模式：list（列表）/ grid（网格大图）/ preview（满幅预览）
  let viewMode = $state('list')
  // 当前目录路径
  let currentPath = $state('')
  // 文件列表
  let files = $state([])
  // 目录树节点（扁平列表，带 depth）
  let treeNodes = $state([])
  // 加载状态
  let loading = $state(false)
  // 手动刷新状态
  let refreshing = $state(false)
  // 错误信息
  let error = $state('')
  // 搜索状态
  let searchMode = $state(false)
  let searchOpen = $state(false)
  let searchQuery = $state('')
  let searchResults = $state([])
  let searchInputEl = $state(null)
  let nameFilterQuery = $state(readLocalStorage(NAME_FILTER_STORAGE_KEY, ''))
  let sortByTimeDesc = $state(readLocalStorage(SORT_TIME_DESC_STORAGE_KEY, '0') === '1')
  let nameFilterTimer = null
  // 预览相关
  let previewFile = $state(null)
  let previewContent = $state('')
  let previewReturnView = $state('list')
  let previewSearchQuery = $state('')
  let previewSearchCurrent = $state(-1)
  let previewSearchTotal = $state(0)
  let textPreviewEl = $state(null)
  let previewSearchMatches = []
  // 选中的文件文件名加粗，前面的目录部分保持普通字重
  function getPreviewPathParts(file) {
    const path = file?.path || file?.name || ''
    const lastSeparator = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
    if (lastSeparator < 0) return { directory: '', name: path }
    return {
      directory: path.slice(0, lastSeparator + 1),
      name: path.slice(lastSeparator + 1),
    }
  }
  // 选中的文件
  let selectedFiles = $state(new Set())
  // 右键菜单
  let contextMenu = $state({ visible: false, x: 0, y: 0, file: null })
  // 滚动加载相关
  let page = $state(1)
  let hasMore = $state(true)
  let pageSize = 50
  // 树是否已初始化
  let treeInitialized = false
  // 跟踪上次的工作区路径，用于检测外部变化
  let trackedWorkspace = $state('')
  let uploadTasks = $state([])
  let uploadQueueRunning = false
  
  // 拖放相关状态
  let dragState = $state({
    isDragging: false,
    dragPaths: [],      // 正在拖拽的文件路径列表
    isCopyMode: false,  // 是否按住 Ctrl (复制模式)
    dropTargetPath: null, // 当前悬停的目录树节点路径
  })
  
  // 确认对话框状态
  let confirmDialog = $state({
    open: false,
    title: '',
    message: '',
    confirmText: '',
    onConfirm: null,
    onCancel: null,
  })

  function normalizePathForCompare(path) {
    return String(path || '').replace(/\\/g, '/').replace(/\/$/, '')
  }

  // Detect Windows-style paths (e.g. C:\, D:/) for case-insensitive comparison.
  // Windows file systems are case-insensitive, so we must lowercase when
  // comparing to avoid false negatives from os.environ vs os.listdir casing.
  function isWindowsStylePath(p) {
    return /^[A-Za-z]:[\\/]/.test(p)
  }

  function pathsEqual(a, b) {
    const na = normalizePathForCompare(a)
    const nb = normalizePathForCompare(b)
    if (isWindowsStylePath(na) || isWindowsStylePath(nb)) {
      return na.toLowerCase() === nb.toLowerCase()
    }
    return na === nb
  }

  function pathStartsWith(base, child) {
    const nb = normalizePathForCompare(base)
    const nc = normalizePathForCompare(child)
    if (isWindowsStylePath(nb) || isWindowsStylePath(nc)) {
      return nc.toLowerCase().startsWith(nb.toLowerCase() + '/')
    }
    return nc.startsWith(nb + '/')
  }

  function isInsideWorkspacePath(path) {
    return pathsEqual(path, workspacePath) || pathStartsWith(workspacePath, path)
  }
  /**
   * 计算相对于工作区的路径，支持 ../ 表示工作区外的路径
   * 工作区根目录本身返回 '.'
   */
  function relativeWorkspacePath(targetPath) {
    const normalizedTarget = normalizePathForCompare(targetPath)
    const normalizedBase = normalizePathForCompare(workspacePath)

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

  function isSelectableFile(file) {
    return !!file && !file.is_dir && isInsideWorkspacePath(file.path)
  }

  // PDF / DOCX 文件判断（按扩展名，无需后端支持）
  function isPdfFile(filename) {
    return /\.pdf$/i.test(filename || '')
  }

  function isDocxFile(filename) {
    return /\.docx$/i.test(filename || '')
  }

  // 是否支持预览：文本/图片/音视频（后端标记）+ PDF/DOCX（前端扩展名）
  function isPreviewable(file) {
    if (!file || file.is_dir) return false
    return (
      file.is_text ||
      file.is_image ||
      file.is_audio ||
      file.is_video ||
      isPdfFile(file.name) ||
      isDocxFile(file.name)
    )
  }

  function getSortMode() {
    return sortByTimeDesc ? 'recent' : 'name'
  }

  function topLevelNameUnderCurrentPath(filePath, fallbackName = '') {
    const normalizedFilePath = normalizePathForCompare(filePath)
    const normalizedCurrentPath = normalizePathForCompare(currentPath)
    const isWin = isWindowsStylePath(normalizedFilePath) || isWindowsStylePath(normalizedCurrentPath)
    const cmpFilePath = isWin ? normalizedFilePath.toLowerCase() : normalizedFilePath
    const cmpCurrentPath = isWin ? normalizedCurrentPath.toLowerCase() : normalizedCurrentPath
    if (cmpCurrentPath && cmpFilePath.startsWith(cmpCurrentPath + '/')) {
      return normalizedFilePath.slice(normalizedCurrentPath.length + 1).split('/')[0] || fallbackName
    }
    if (!cmpCurrentPath && cmpFilePath.startsWith('/')) {
      return normalizedFilePath.slice(1).split('/')[0] || fallbackName
    }
    return fallbackName
  }

  function matchesNameFilter(file, isSearchList) {
    const filterText = nameFilterQuery.trim().toLowerCase()
    if (!filterText) return true
    // For search results the backend has already applied the name filter;
    // for the defensive frontend fallback, match against the file basename
    // (search results span subdirectories, so we need the actual filename,
    // not the top-level component under currentPath).
    const nameToCheck = isSearchList ? (file.name || '') : file.name
    return (nameToCheck || '').toLowerCase().includes(filterText)
  }

  function filterAndSortFiles(fileList, isSearchList = false) {
    let result = Array.isArray(fileList) ? [...fileList] : []
    result = result.filter((file) => matchesNameFilter(file, isSearchList))

    if (sortByTimeDesc) {
      result.sort((a, b) => {
        const timeDiff = (b.modified || 0) - (a.modified || 0)
        if (timeDiff !== 0) return timeDiff
        return (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' })
      })
    } else {
      result.sort((a, b) => {
        if (!!a.is_dir !== !!b.is_dir) return a.is_dir ? -1 : 1
        return (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' })
      })
    }
    return result
  }

  let displayedFiles = $derived(filterAndSortFiles(searchMode ? searchResults : files, searchMode))

  // 当前目录是否在工作区内
  let isCurrentPathInWorkspace = $derived(isInsideWorkspacePath(currentPath))


  // 跟踪上次的面板打开状态，用于打开时滚动工作区节点
  let prevOpen = false

  // 初始化：加载工作区根目录
  $effect(() => {
    // 检测工作区路径是否从外部变化
    if (workspacePath !== trackedWorkspace) {
      trackedWorkspace = workspacePath
      if (treeInitialized && workspacePath) {
        // 工作区变化：增量导航到新路径，复用已加载的树节点
        currentPath = ''
        navigateTreeToPath(workspacePath)
      }
    }

    if (open && workspacePath) {
      if (!currentPath) {
        currentPath = workspacePath
        loadFiles(currentPath)
      }
      if (!treeInitialized) {
        treeInitialized = true
        ensureTreeInit(workspacePath)
      }
    }

    // 面板从关闭变为打开时，已初始化的树需要滚动到工作区节点
    // （navigateTreeToPath 在面板关闭期间执行时 DOM 不存在，scrollIntoView 无效）
    if (open && !prevOpen && treeInitialized && workspacePath) {
      requestAnimationFrame(() => {
        const wsNode = document.querySelector('.tree-node.workspace')
        if (wsNode) {
          wsNode.scrollIntoView({ behavior: 'smooth', block: 'start' })
        }
      })
    }
    prevOpen = open

    // 面板关闭时不再重置 treeInitialized，保留树状态以便再次打开时即时显示
  })

  // 加载文件列表（带请求序号守卫，避免快速导航时旧响应覆盖新目录）
  let loadSeq = 0
  async function loadFiles(dirPath, append = false) {
    const seq = ++loadSeq
    loading = true
    error = ''
    try {
      const data = await workspaceApi.list(dirPath, page, pageSize, false, {
        sort: getSortMode(),
        nameFilter: nameFilterQuery.trim(),
      })
      if (seq !== loadSeq) return  // 过期响应，丢弃
      
      if (append) {
        files = [...files, ...data.files]
      } else {
        files = data.files
      }
      hasMore = data.has_more
    } catch (err) {
      if (seq !== loadSeq) return
      error = err.message
    } finally {
      if (seq === loadSeq) loading = false
    }
  }

  // 构造子路径：parentPath + name
  function childPath(parentPath, name) {
    if (parentPath === '/') return `/${name}`
    const sep = parentPath.includes('\\') ? '\\' : '/'
    // 如果父路径已以分隔符结尾（如 Windows 根 "D:\"），不再重复添加
    if (parentPath.endsWith(sep)) return `${parentPath}${name}`
    return `${parentPath}${sep}${name}`
  }

  // 初始化目录树：从根节点逐级展开到工作区路径
  let treeInitPromise = null
  async function initTree(wsPath) {
    treeNodes = []
    // 1. 加载根节点（Windows 下可能是多个盘符，Unix 下是 ['/']）
    let roots
    try {
      roots = await workspaceApi.children('')
    } catch (err) {
      console.error('Failed to load roots:', err)
      return
    }
    if (!roots || roots.length === 0) return

    // 为所有根节点创建树节点（depth=0）
    const normalizedWs = wsPath.replace(/\\/g, '/').toLowerCase()
    for (const root of roots) {
      const rootPath = root.name
      const normalizedRoot = rootPath.replace(/\\/g, '/').toLowerCase().replace(/\/$/, '')
      // 判断工作区是否在该根节点下
      const isUnderThisRoot = normalizedWs === normalizedRoot || normalizedWs.startsWith(normalizedRoot + '/')
      treeNodes.push({
        path: rootPath,
        name: rootPath,
        depth: 0,
        expanded: false,  // 先折叠，后面再展开需要的
        loading: false,
        isWorkspace: pathsEqual(rootPath, wsPath)
      })
    }

    // 2. 找到包含工作区路径的根节点，逐级展开
    const wsRootIdx = treeNodes.findIndex(n => {
      const normalizedRoot = n.path.replace(/\\/g, '/').toLowerCase().replace(/\/$/, '')
      return normalizedWs === normalizedRoot || normalizedWs.startsWith(normalizedRoot + '/')
    })

    if (wsRootIdx === -1) {
      // 工作区不在任何根下，展开第一个根
      const firstRoot = treeNodes[0]
      firstRoot.expanded = true
      try {
        const children = await workspaceApi.children(firstRoot.path)
        insertChildren(0, firstRoot.path, children, wsPath)
      } catch {}
      treeNodes = [...treeNodes]
      return
    }

    // 展开工作区所在的根节点
    const wsRoot = treeNodes[wsRootIdx]
    wsRoot.expanded = true

    // 加载根的子目录
    let children
    try {
      children = await workspaceApi.children(wsRoot.path)
    } catch {
      treeNodes = [...treeNodes]
      return
    }
    insertChildren(wsRootIdx, wsRoot.path, children, wsPath)

    // 从根到工作区的路径段，逐级展开
    const normalizedRootPath = wsRoot.path.replace(/\\/g, '/').replace(/\/$/, '')
    const relative = normalizedWs.startsWith(normalizedRootPath.toLowerCase())
      ? wsPath.replace(/\\/g, '/').slice(normalizedRootPath.length)
      : wsPath.replace(/\\/g, '/')
    const segments = relative.split('/').filter(Boolean)

    let parentPath = wsRoot.path
    let parentIdx = wsRootIdx
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]
      const segPath = childPath(parentPath, seg)

      // 确保该段在树中并展开
      const nodeIdx = treeNodes.findIndex(n => pathsEqual(n.path, segPath))
      if (nodeIdx === -1) break

      treeNodes[nodeIdx].expanded = true
      treeNodes[nodeIdx].isWorkspace = (i === segments.length - 1)

      // 加载该段的子目录
      try {
        const subChildren = await workspaceApi.children(segPath)
        insertChildren(nodeIdx, segPath, subChildren, wsPath)
      } catch {
        break
      }
      parentPath = segPath
      parentIdx = nodeIdx
    }

    treeNodes = [...treeNodes] // 触发响应式

    // 自动滚动工作区节点到顶部
    requestAnimationFrame(() => {
      const wsNode = document.querySelector('.tree-node.workspace')
      if (wsNode) {
        wsNode.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    })
  }

  // 确保目录树已初始化（并发去重：同一批次多次触发只执行一次 initTree）
  function ensureTreeInit(wsPath) {
    if (treeInitialized && treeNodes.length > 0) return Promise.resolve()
    if (treeInitPromise) return treeInitPromise
    treeInitPromise = (async () => {
      try {
        await initTree(wsPath)
      } finally {
        treeInitPromise = null
      }
    })()
    return treeInitPromise
  }

  // 判断节点是否已有子节点（已加载过）
  function hasChildrenLoaded(nodeIdx) {
    if (nodeIdx >= treeNodes.length - 1) return false
    const node = treeNodes[nodeIdx]
    for (let i = nodeIdx + 1; i < treeNodes.length; i++) {
      if (treeNodes[i].depth <= node.depth) return false
      if (treeNodes[i].depth === node.depth + 1) return true
    }
    return false
  }

  // 增量导航到新工作区路径：复用已加载的树节点，仅加载缺失的层级
  async function navigateTreeToPath(wsPath) {
    if (treeNodes.length === 0) {
      return ensureTreeInit(wsPath)
    }

    const normalizedWs = wsPath.replace(/\\/g, '/').toLowerCase()

    // 找到包含工作区路径的根节点
    const wsRootIdx = treeNodes.findIndex(n => {
      const normalizedRoot = n.path.replace(/\\/g, '/').toLowerCase().replace(/\/$/, '')
      return normalizedWs === normalizedRoot || normalizedWs.startsWith(normalizedRoot + '/')
    })

    if (wsRootIdx === -1) {
      // 工作区不在任何已加载的根下（如新挂载的盘符），全量重建
      return initTree(wsPath)
    }

    // 清除旧的 isWorkspace 标记
    for (const node of treeNodes) {
      if (node.isWorkspace) node.isWorkspace = false
    }

    // 确保工作区根已展开且子节点已加载
    const wsRoot = treeNodes[wsRootIdx]
    wsRoot.expanded = true
    if (!hasChildrenLoaded(wsRootIdx)) {
      try {
        const children = await workspaceApi.children(wsRoot.path)
        insertChildren(wsRootIdx, wsRoot.path, children, wsPath)
      } catch { return }
    }

    // 从根到工作区逐级展开
    const normalizedRootPath = wsRoot.path.replace(/\\/g, '/').replace(/\/$/, '')
    const relative = normalizedWs.startsWith(normalizedRootPath.toLowerCase())
      ? wsPath.replace(/\\/g, '/').slice(normalizedRootPath.length)
      : wsPath.replace(/\\/g, '/')
    const segments = relative.split('/').filter(Boolean)

    let parentPath = wsRoot.path
    let parentIdx = wsRootIdx
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]
      const segPath = childPath(parentPath, seg)

      let nodeIdx = treeNodes.findIndex(n => pathsEqual(n.path, segPath))

      if (nodeIdx === -1) {
        // 节点不在树中，重新加载父节点的子节点
        try {
          const subChildren = await workspaceApi.children(parentPath)
          insertChildren(parentIdx, parentPath, subChildren, wsPath)
        } catch { break }
        nodeIdx = treeNodes.findIndex(n => pathsEqual(n.path, segPath))
        if (nodeIdx === -1) break
      }

      treeNodes[nodeIdx].expanded = true
      treeNodes[nodeIdx].isWorkspace = (i === segments.length - 1)

      // 加载子节点（如果尚未加载）
      if (!hasChildrenLoaded(nodeIdx)) {
        try {
          const subChildren = await workspaceApi.children(segPath)
          insertChildren(nodeIdx, segPath, subChildren, wsPath)
        } catch { break }
      }

      parentPath = segPath
      parentIdx = nodeIdx
    }

    treeNodes = [...treeNodes]

    requestAnimationFrame(() => {
      const wsNode = document.querySelector('.tree-node.workspace')
      if (wsNode) {
        wsNode.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    })
  }

  // 展开目录树到任意目录（不改变 workspace 标记，仅展开路径节点）
  // 用于从会话日志目录等外部路径进入时，让左侧列表与右侧树保持一致
  async function expandTreeToPath(dirPath) {
    if (!dirPath) return
    if (treeNodes.length === 0) {
      await ensureTreeInit(workspacePath)
      if (treeNodes.length === 0) return
    }

    const normalizedDir = dirPath.replace(/\\/g, '/').toLowerCase()

    // 找到包含目标路径的根节点
    const rootIdx = treeNodes.findIndex(n => {
      const normalizedRoot = n.path.replace(/\\/g, '/').toLowerCase().replace(/\/$/, '')
      return normalizedDir === normalizedRoot || normalizedDir.startsWith(normalizedRoot + '/')
    })
    if (rootIdx === -1) return

    // 确保根节点已展开且子节点已加载
    const root = treeNodes[rootIdx]
    root.expanded = true
    if (!hasChildrenLoaded(rootIdx)) {
      try {
        const children = await workspaceApi.children(root.path)
        insertChildren(rootIdx, root.path, children, workspacePath)
      } catch { return }
    }

    const normalizedRootPath = root.path.replace(/\\/g, '/').replace(/\/$/, '')
    const relative = normalizedDir.startsWith(normalizedRootPath.toLowerCase())
      ? dirPath.replace(/\\/g, '/').slice(normalizedRootPath.length)
      : dirPath.replace(/\\/g, '/')
    const segments = relative.split('/').filter(Boolean)

    let parentPath = root.path
    let parentIdx = rootIdx
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]
      const segPath = childPath(parentPath, seg)

      let nodeIdx = treeNodes.findIndex(n => pathsEqual(n.path, segPath))
      if (nodeIdx === -1) {
        // 节点不在树中，重新加载父节点的子节点
        try {
          const subChildren = await workspaceApi.children(parentPath)
          insertChildren(parentIdx, parentPath, subChildren, workspacePath)
        } catch { break }
        nodeIdx = treeNodes.findIndex(n => pathsEqual(n.path, segPath))
        if (nodeIdx === -1) break
      }

      treeNodes[nodeIdx].expanded = true

      // 加载子节点（如果尚未加载）
      if (!hasChildrenLoaded(nodeIdx)) {
        try {
          const subChildren = await workspaceApi.children(segPath)
          insertChildren(nodeIdx, segPath, subChildren, workspacePath)
        } catch { break }
      }

      parentPath = segPath
      parentIdx = nodeIdx
    }

    treeNodes = [...treeNodes]

    // 滚动到当前激活节点
    requestAnimationFrame(() => {
      const activeNode = document.querySelector('.tree-node.active')
      if (activeNode) {
        activeNode.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    })
  }

  // 导航到指定目录：切换左侧列表并展开右侧目录树
  function navigateToDirectory(dirPath) {
    if (!dirPath) return
    closePreview()
    searchMode = false
    searchOpen = false
    searchQuery = ''
    searchResults = []
    selectedFiles.clear()
    selectedFiles = new Set(selectedFiles)
    currentPath = dirPath
    page = 1
    hasMore = true
    loadFiles(dirPath)
    expandTreeToPath(dirPath)
  }

  // 外部导航请求：{ path, token }，token 变化时触发导航（支持重复打开同一目录）
  let lastNavigateToken = 0
  $effect(() => {
    const target = navigateTarget
    if (!target || !target.path || target.token === lastNavigateToken) return
    lastNavigateToken = target.token
    navigateToDirectory(target.path)
  })

  // 在父节点后插入子节点
  function insertChildren(parentIdx, parentPath, children, wsPath) {
    const parentNode = treeNodes[parentIdx]
    const depth = parentNode.depth + 1
    const childNodes = children.map(c => {
      const path = childPath(parentPath, c.name)
      return {
        path,
        name: c.name,
        depth,
        expanded: false,
        loading: false,
        isWorkspace: pathsEqual(path, wsPath),
        ...(c.symlink_target ? { symlink_target: c.symlink_target } : {}),
      }
    })
    // 移除该父节点旧的子节点（如果有）
    let removeCount = 0
    for (let i = parentIdx + 1; i < treeNodes.length; i++) {
      if (treeNodes[i].depth > parentNode.depth) {
        removeCount++
      } else {
        break
      }
    }
    treeNodes.splice(parentIdx + 1, removeCount, ...childNodes)
  }

  // 切换树节点展开/折叠
  async function toggleTreeNode(node) {
    const idx = treeNodes.findIndex(n => n.path === node.path)
    if (idx === -1) return

    if (node.expanded) {
      // 折叠：移除所有子节点
      let removeCount = 0
      for (let i = idx + 1; i < treeNodes.length; i++) {
        if (treeNodes[i].depth > node.depth) {
          removeCount++
        } else {
          break
        }
      }
      treeNodes.splice(idx + 1, removeCount)
      treeNodes[idx].expanded = false
      treeNodes = [...treeNodes]
    } else {
      // 展开：加载子目录
      treeNodes[idx].loading = true
      treeNodes = [...treeNodes]
      try {
        const children = await workspaceApi.children(node.path)
        insertChildren(idx, node.path, children, workspacePath)
        treeNodes[idx].expanded = true
        treeNodes[idx].loading = false
        treeNodes = [...treeNodes]
      } catch (err) {
        treeNodes[idx].loading = false
        treeNodes = [...treeNodes]
        console.error('Failed to load children:', err)
      }
    }
  }

  // 设置目录为新工作区
  function setAsWorkspace(dirPath) {
    trackedWorkspace = dirPath   // 防止 $effect 重复触发
    workspacePath = dirPath
    currentPath = dirPath
    page = 1
    hasMore = true
    loadFiles(dirPath)
    onWorkspaceChange?.(dirPath)
    // 重新初始化树
    treeInitPromise = null
    treeInitialized = false
    treeNodes = []
  }

  async function reloadCurrentDirectory() {
    if (!currentPath) return
    page = 1
    hasMore = true
    await loadFiles(currentPath)
  }

  // 手动刷新左侧文件列表（外部修改文件后使用）
  async function handleRefresh() {
    if (refreshing) return
    refreshing = true
    try {
      if (searchMode) {
        // 搜索结果模式下，重新执行当前搜索（同时携带文件名过滤）
        await handleSearch()
      } else {
        await reloadCurrentDirectory()
      }
      // 顺带刷新右侧目录树中当前目录的子节点（若已展开）
      await refreshTreeNodeChildren(currentPath)
    } finally {
      refreshing = false
    }
  }

  async function refreshTreeNodeChildren(dirPath) {
    const idx = treeNodes.findIndex(n => pathsEqual(n.path, dirPath))
    if (idx === -1 || !treeNodes[idx].expanded) return
    try {
      const children = await workspaceApi.children(dirPath)
      insertChildren(idx, dirPath, children, workspacePath)
      treeNodes = [...treeNodes]
    } catch (err) {
      console.error('Failed to refresh tree children:', err)
    }
  }

  function handleNameFilterInput(e) {
    nameFilterQuery = e.currentTarget.value
    writeLocalStorage(NAME_FILTER_STORAGE_KEY, nameFilterQuery)
    if (nameFilterTimer) clearTimeout(nameFilterTimer)
    nameFilterTimer = setTimeout(() => {
      // In search mode the displayed list is driven by searchResults
      // (filtered on the frontend via matchesNameFilter); avoid a
      // redundant directory reload.
      if (!searchMode) reloadCurrentDirectory()
    }, 200)
  }

  function handleNameFilterKeydown(e) {
    if (e.key === 'Enter') {
      if (nameFilterTimer) clearTimeout(nameFilterTimer)
      if (nameFilterQuery.trim()) {
        // Enter with non-empty filter → recursive filename search (or content+name)
        handleSearch()
      } else {
        reloadCurrentDirectory()
      }
    } else if (e.key === 'Escape') {
      nameFilterQuery = ''
      writeLocalStorage(NAME_FILTER_STORAGE_KEY, '')
      if (nameFilterTimer) clearTimeout(nameFilterTimer)
      searchMode = false
      searchResults = []
      reloadCurrentDirectory()
    }
  }

  function toggleTimeSort() {
    sortByTimeDesc = !sortByTimeDesc
    writeLocalStorage(SORT_TIME_DESC_STORAGE_KEY, sortByTimeDesc ? '1' : '0')
    reloadCurrentDirectory()
  }

  // 搜索文件
  async function handleSearch() {
    if (!searchQuery.trim() && !nameFilterQuery.trim()) {
      searchMode = false
      searchResults = []
      return
    }
    
    searchMode = true
    loading = true
    try {
      const results = await workspaceApi.search(currentPath, searchQuery.trim(), nameFilterQuery.trim())
      searchResults = results
    } catch (err) {
      error = err.message
    } finally {
      loading = false
    }
  }

  // 切换搜索
  function toggleSearch() {
    if (searchOpen) {
      searchOpen = false
      searchMode = false
      searchQuery = ''
      searchResults = []
    } else {
      searchOpen = true
      setTimeout(() => { searchInputEl?.focus() }, 50)
    }
  }

  function handleSearchKeydown(e) {
    if (e.key === 'Enter') {
      handleSearch()
    } else if (e.key === 'Escape') {
      searchOpen = false
      searchMode = false
      searchQuery = ''
      searchResults = []
    }
  }

  // 进入目录
  function enterDirectory(dirPath) {
    closePreview()
    currentPath = dirPath
    page = 1
    hasMore = true
    loadFiles(dirPath)
    selectedFiles.clear()
  }

  // 返回上级目录
  function goUp() {
    const parentPath = currentPath.replace(/[/\\][^/\\]+$/, '') || '/'
    enterDirectory(parentPath)
  }

  // 预览文件
  async function previewFileContent(file) {
    if (!isPreviewable(file)) {
      return // 不支持预览的文件类型
    }
    
    resetPreviewSearch()
    previewReturnView = viewMode
    previewFile = {
      ...file,
      is_pdf: isPdfFile(file.name),
      is_docx: isDocxFile(file.name),
    }
    viewMode = 'preview'
    previewContent = ''
    
    if (file.is_text) {
      try {
        const response = await fetch(workspaceApi.content(file.path, false))
        if (!response.ok) throw new Error('Failed to load file content')
        previewContent = await response.text()
      } catch (err) {
        error = err.message
      }
    }
  }

  /**
   * 强制按ASCII文本模式打开文件（忽略文件类型判断）
   */
  async function openAsTextFile(file) {
    if (!file || file.is_dir) return
    
    resetPreviewSearch()
    previewReturnView = viewMode
    previewFile = { ...file, is_text: true, is_image: false, is_audio: false, is_video: false, forcePlainText: true }
    viewMode = 'preview'
    previewContent = ''
    
    try {
      const response = await fetch(workspaceApi.content(file.path, false))
      if (!response.ok) throw new Error('Failed to load file content')
      previewContent = await response.text()
    } catch (err) {
      error = err.message
    }
  }

  function clearPreviewSearchHighlights() {
    if (!textPreviewEl) return
    const marks = textPreviewEl.querySelectorAll('mark.preview-search-match')
    for (const mark of marks) {
      mark.replaceWith(document.createTextNode(mark.textContent || ''))
    }
    textPreviewEl.normalize()
    previewSearchMatches = []
    previewSearchCurrent = -1
    previewSearchTotal = 0
  }

  async function updatePreviewSearch() {
    await tick()
    clearPreviewSearchHighlights()
    const query = previewSearchQuery
    if (!query || !textPreviewEl) return

    const lowerQuery = query.toLocaleLowerCase()
    const walker = document.createTreeWalker(textPreviewEl, NodeFilter.SHOW_TEXT)
    const textNodes = []
    let node
    while ((node = walker.nextNode())) textNodes.push(node)

    for (const textNode of textNodes) {
      const text = textNode.nodeValue || ''
      const lowerText = text.toLocaleLowerCase()
      const indexes = []
      let fromIndex = 0
      while (fromIndex <= lowerText.length - lowerQuery.length) {
        const index = lowerText.indexOf(lowerQuery, fromIndex)
        if (index < 0) break
        indexes.push(index)
        fromIndex = index + Math.max(query.length, 1)
      }
      if (!indexes.length) continue

      const fragment = document.createDocumentFragment()
      let cursor = 0
      for (const index of indexes) {
        fragment.append(document.createTextNode(text.slice(cursor, index)))
        const mark = document.createElement('mark')
        mark.className = 'preview-search-match'
        mark.textContent = text.slice(index, index + query.length)
        fragment.append(mark)
        previewSearchMatches.push(mark)
        cursor = index + query.length
      }
      fragment.append(document.createTextNode(text.slice(cursor)))
      textNode.replaceWith(fragment)
    }

    previewSearchTotal = previewSearchMatches.length
    if (previewSearchTotal > 0) {
      previewSearchCurrent = 0
      focusPreviewSearchMatch()
    }
  }

  function focusPreviewSearchMatch() {
    for (const [index, match] of previewSearchMatches.entries()) {
      match.classList.toggle('current', index === previewSearchCurrent)
    }
    previewSearchMatches[previewSearchCurrent]?.scrollIntoView({ block: 'center', inline: 'nearest' })
  }

  function movePreviewSearch(direction) {
    if (!previewSearchTotal) return
    previewSearchCurrent = (previewSearchCurrent + direction + previewSearchTotal) % previewSearchTotal
    focusPreviewSearchMatch()
  }

  function handlePreviewSearchKeydown(event) {
    if (event.key === 'Enter') {
      event.preventDefault()
      movePreviewSearch(event.shiftKey ? -1 : 1)
    } else if (event.key === 'Escape') {
      previewSearchQuery = ''
      updatePreviewSearch()
    }
  }

  function resetPreviewSearch() {
    clearPreviewSearchHighlights()
    previewSearchQuery = ''
  }

  function closePreview() {
    resetPreviewSearch()
    viewMode = previewReturnView
    previewFile = null
    previewContent = ''
  }

  // 渲染预览 HTML
  // filePath: 当前预览文件的绝对路径，用于解析 markdown 中相对路径的图片/资源
  function renderPreviewHtml(content, filename, forcePlainText = false, filePath = '') {
    if (!content) return ''
    // Normalize line endings: CRLF / CR → LF, so \r does not leak into <pre> as extra line breaks
    content = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n')

    const renderCodeWithLines = (rawLines) => {
      // Drop trailing empty string caused by final newline (keeps gutter line-count accurate)
      if (rawLines.length && rawLines[rawLines.length - 1] === '') rawLines.pop()
      const lineCount = rawLines.length
      const digits = Math.max(String(lineCount).length, 3)
      const gutter = rawLines.map((_, i) => `<span>${i + 1}</span>`).join('')
      const body = rawLines.join('\n')
      return `<div class="code-container" style="--digits:${digits}"><div class="line-gutter">${gutter}</div><pre class="code-body"><code>${body}</code></pre></div>`
    }

    // 解析 markdown 文件所在目录，用于解决相对路径引用
    function getMarkdownDir() {
      if (!filePath) return ''
      const normalized = filePath.replace(/\\/g, '/')
      const lastSep = normalized.lastIndexOf('/')
      return lastSep >= 0 ? normalized.slice(0, lastSep) : ''
    }

    // 将 markdown 中的图片/资源 src 解析为可访问的 API URL
    function resolveResourceUrl(src) {
      if (!src) return ''
      // 外部链接（http/https）保持不变
      if (/^https?:\/\//i.test(src)) return src
      // data URI 保持不变
      if (/^data:/i.test(src)) return src
      // 绝对路径（以 / 开头）直接用 workspace API
      if (src.startsWith('/')) return workspaceApi.content(src, false)
      // 相对路径：相对于 markdown 文件所在目录解析
      const dir = getMarkdownDir()
      if (!dir) return src // 无法解析目录，保持原样
      // 拼接并规范化路径
      const parts = (dir + '/' + src).split('/')
      const resolved = []
      for (const p of parts) {
        if (p === '.' || p === '') continue
        if (p === '..') { resolved.pop(); continue }
        resolved.push(p)
      }
      // Unix 绝对路径需要前导 /，Windows 路径（如 C:）已包含在 resolved[0]
      const absolutePath = (filePath.startsWith('/') ? '/' : '') + resolved.join('/')
      return workspaceApi.content(absolutePath, false)
    }

    if (forcePlainText) {
      return renderCodeWithLines(content.split('\n').map(l => escapeHtml(l)))
    }
    if (isMarkdownFile(filename)) {
      try {
        const renderer = new marked.Renderer()
        renderer.code = function({ text, lang }) {
          const normalizedLang = lang || ''
          const highlightedHtml = highlight(text, normalizedLang)
          return `<div class="code-block"><pre><code class="${normalizedLang ? 'language-' + normalizedLang : ''}">${highlightedHtml}</code></pre></div>`
        }
        renderer.link = function({ href, title, text }) {
          const titleAttr = title ? ` title="${title}"` : ''
          return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`
        }
        renderer.image = function({ href, title, text }) {
          const titleAttr = title ? ` title="${title}"` : ''
          const src = resolveResourceUrl(href)
          return `<img src="${src}" alt="${text}"${titleAttr} />`
        }
        return marked.parse(content, { renderer, gfm: true, breaks: true })
      } catch {
        return escapeHtml(content)
      }
    }
    const lang = getFileLang(filename)
    const lines = content.split('\n')
    if (lang) {
      return renderCodeWithLines(lines.map(line => highlight(line, lang)))
    }
    return renderCodeWithLines(lines.map(l => escapeHtml(l)))
  }

  // 下载文件
  function downloadFile(file) {
    const link = document.createElement('a')
    link.href = workspaceApi.download(file.path, false)
    link.download = file.name
    link.click()
  }

  // 记录最后选中的文件，用于 Shift 范围选择
  let lastSelectedFile = $state(null)

  // 选择文件（符合 Windows 资源管理器习惯）
  // - 单击：选中当前文件，取消其他
  // - Ctrl+点击：切换选中状态（添加/移除）
  // - Shift+点击：范围选择（从上次选中到当前）
  function handleFileClick(file, event) {
    if (file.is_dir) return
    
    if (event.shiftKey && lastSelectedFile) {
      // Shift+点击：范围选择
      const currentFiles = displayedFiles.filter(f => !f.is_dir)
      const lastIdx = currentFiles.findIndex(f => f.path === lastSelectedFile.path)
      const currentIdx = currentFiles.findIndex(f => f.path === file.path)
      if (lastIdx !== -1 && currentIdx !== -1) {
        const start = Math.min(lastIdx, currentIdx)
        const end = Math.max(lastIdx, currentIdx)
        // 保留已有的选中，添加范围内的
        for (let i = start; i <= end; i++) {
          selectedFiles.add(currentFiles[i].path)
        }
        selectedFiles = new Set(selectedFiles)
      }
    } else if (event.ctrlKey || event.metaKey) {
      // Ctrl+点击（Mac: Cmd+点击）：切换选中状态
      if (selectedFiles.has(file.path)) {
        selectedFiles.delete(file.path)
      } else {
        selectedFiles.add(file.path)
      }
      selectedFiles = new Set(selectedFiles)
      lastSelectedFile = file
    } else {
      // 普通点击：单选
      selectedFiles = new Set([file.path])
      lastSelectedFile = file
    }
  }

  // 确认选择文件到输入框
  function confirmSelectFiles() {
    const selected = displayedFiles
      .filter(f => selectedFiles.has(f.path) && isSelectableFile(f))
      .map(f => ({ ...f, relative_path: relativeWorkspacePath(f.path) }))
    if (selected.length === 0) return
    onSelectFiles?.(selected)
    selectedFiles.clear()
    selectedFiles = new Set(selectedFiles)
  }

  // 获取当前选中的文件对象列表
  function getSelectedFiles() {
    return displayedFiles.filter(f => selectedFiles.has(f.path))
  }

  // 判断是否有多选
  function hasMultiSelection() {
    return selectedFiles.size > 1
  }

  // 批量下载
  function downloadSelectedFiles() {
    const files = getSelectedFiles().filter(f => !f.is_dir)
    for (const file of files) {
      const link = document.createElement('a')
      link.href = workspaceApi.download(file.path, false)
      link.download = file.name
      link.click()
    }
  }

  // 批量复制绝对路径
  function copySelectedPaths() {
    const paths = getSelectedFiles().map(f => f.path)
    if (paths.length > 0) {
      copyToClipboard(paths.join('\n'))
    }
  }
  
  // 批量复制相对路径（相对于工作区）
  function copySelectedRelativePaths() {
    const files = getSelectedFiles()
    if (files.length > 0) {
      const relativePaths = files.map(f => relativeWorkspacePath(f.path))
      copyToClipboard(relativePaths.join('\n'))
    }
  }

  // 批量创建副本
  async function duplicateSelectedFiles() {
    const files = getSelectedFiles().filter(f => !f.is_dir)
    for (const file of files) {
      try {
        await workspaceApi.duplicate(file.path)
      } catch (err) {
        error = err.message
      }
    }
    loadFiles(currentPath)
  }

  // 批量删除
  async function deleteSelectedFiles() {
    const files = getSelectedFiles()
    const names = files.map(f => f.name).join(', ')
    if (!confirm(`${t('confirmDeleteFile')} (${files.length} ${t('files')}: ${names})`)) return
    for (const file of files) {
      try {
        await workspaceApi.delete(file.path)
      } catch (err) {
        error = err.message
      }
    }
    selectedFiles.clear()
    selectedFiles = new Set(selectedFiles)
    loadFiles(currentPath)
  }

  // 右键菜单操作
  function showContextMenu(e, file) {
    e.preventDefault()
    const menuHeight = 250 // estimated before render; refined after tick
    let x = e.clientX
    let y = e.clientY
    // Pre-clamp: if click is in the bottom region, start higher
    if (y + menuHeight > window.innerHeight) {
      y = Math.max(0, window.innerHeight - menuHeight - 8)
    }
    contextMenu = { visible: true, x, y, file }
    // After DOM update, measure real height and fine-tune
    tick().then(() => {
      const el = document.querySelector('.context-menu')
      if (!el) return
      const rect = el.getBoundingClientRect()
      let newY = y
      if (rect.bottom > window.innerHeight) {
        newY = Math.max(0, window.innerHeight - rect.height - 8)
      }
      if (rect.right > window.innerWidth) {
        const newX = Math.max(0, window.innerWidth - rect.width - 8)
        contextMenu = { ...contextMenu, x: newX, y: newY }
      } else if (newY !== y) {
        contextMenu = { ...contextMenu, y: newY }
      }
    })
  }

  function hideContextMenu() {
    contextMenu = { visible: false, x: 0, y: 0, file: null }
  }

  // 重命名文件
  async function renameFile(file) {
    const newName = prompt(t('enterNewName'), file.name)
    if (!newName || newName === file.name) return
    
    try {
      await workspaceApi.rename(file.path, newName)
      loadFiles(currentPath)
    } catch (err) {
      console.error('Rename error:', err)
      error = err.message
    }
    hideContextMenu()
  }

  // 创建副本
  async function duplicateFile(file) {
    try {
      await workspaceApi.duplicate(file.path)
      loadFiles(currentPath)
    } catch (err) {
      error = err.message
    }
    hideContextMenu()
  }

  // 删除文件
  async function deleteFile(file) {
    if (!confirm(t('confirmDeleteNamedFile').replace('{name}', file.name))) return
    
    try {
      await workspaceApi.delete(file.path)
      loadFiles(currentPath)
    } catch (err) {
      error = err.message
    }
    hideContextMenu()
  }

  // ==================== 拖放功能 ====================
  
  /**
   * 文件列表项开始拖拽
   */
  function handleFileDragStart(e, file) {
    // 如果拖拽的文件不在选中列表中，先选中它
    let dragPaths = []
    if (selectedFiles.has(file.path)) {
      // 拖拽已选中的文件，移动所有选中的
      dragPaths = [...selectedFiles]
    } else {
      // 拖拽未选中的文件，只移动这一个
      dragPaths = [file.path]
    }
    
    dragState.isDragging = true
    dragState.dragPaths = dragPaths
    dragState.isCopyMode = e.ctrlKey || e.metaKey
    
    // 设置拖拽数据
    e.dataTransfer.effectAllowed = dragState.isCopyMode ? 'copy' : 'move'
    e.dataTransfer.setData('application/json', JSON.stringify({
      paths: dragPaths,
      isCopy: dragState.isCopyMode,
    }))
    
    // 创建自定义拖拽图像（显示文件数量）
    const ghost = document.createElement('div')
    ghost.className = 'drag-ghost'
    ghost.textContent = dragPaths.length > 1 ? `${dragPaths.length} ${t('files')}` : file.name
    ghost.style.cssText = 'position: absolute; top: -1000px; padding: 8px 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,0.15);'
    document.body.appendChild(ghost)
    e.dataTransfer.setDragImage(ghost, 0, 0)
    
    // 清理 ghost 元素
    requestAnimationFrame(() => {
      document.body.removeChild(ghost)
    })
  }

  /**
   * 文件列表项拖拽中（更新 Ctrl 状态）
   */
  function handleFileDrag(e) {
    // 更新复制模式状态
    dragState.isCopyMode = e.ctrlKey || e.metaKey
  }

  /**
   * 文件列表项结束拖拽
   */
  function handleFileDragEnd() {
    dragState.isDragging = false
    dragState.dragPaths = []
    dragState.dropTargetPath = null
  }

  /**
   * 目录树节点 dragover
   */
  function handleTreeDragOver(e, node) {
    // 只允许拖放到目录节点
    if (!node.path) return
    
    // 检查是否拖放到自身或子目录
    const isSelfOrChild = dragState.dragPaths.some(p => 
      pathsEqual(p, node.path) || pathStartsWith(p, node.path)
    )
    if (isSelfOrChild) {
      e.dataTransfer.dropEffect = 'none'
      return
    }
    
    e.preventDefault()
    e.dataTransfer.dropEffect = dragState.isCopyMode ? 'copy' : 'move'
    dragState.dropTargetPath = node.path
  }

  /**
   * 目录树节点 dragleave
   */
  function handleTreeDragLeave(e, node) {
    // 只有离开当前节点时才清除（避免子元素触发）
    if (dragState.dropTargetPath === node.path) {
      // 检查 relatedTarget 是否还在当前节点内
      const rect = e.currentTarget.getBoundingClientRect()
      const x = e.clientX
      const y = e.clientY
      if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) {
        dragState.dropTargetPath = null
      }
    }
  }

  /**
   * 目录树节点 drop
   */
  async function handleTreeDrop(e, node) {
    e.preventDefault()
    dragState.dropTargetPath = null
    
    const destPath = node.path
    if (!destPath || dragState.dragPaths.length === 0) return
    
    const isCopy = e.ctrlKey || e.metaKey
    const operation = isCopy ? 'copy' : 'move'
    
    // 执行操作
    await executeMoveOrCopy(dragState.dragPaths, destPath, operation)
  }

  /**
   * 执行移动或复制操作
   */
  async function executeMoveOrCopy(paths, destPath, operation, overwrite = false) {
    try {
      const apiMethod = operation === 'copy' ? workspaceApi.copy : workspaceApi.move
      const result = await apiMethod(paths, destPath, overwrite)
      
      // 检查是否有冲突
      if (result.errors && result.errors.length > 0) {
        const conflicts = result.errors.filter(e => e.conflict)
        if (conflicts.length > 0) {
          // 显示覆盖确认对话框
          showOverwriteDialog(paths, destPath, operation, conflicts)
          return
        }
      }
      
      // 操作完成，刷新列表
      const successCount = operation === 'copy' 
        ? (result.copied?.length || 0) 
        : (result.moved?.length || 0)
      
      if (successCount > 0) {
        loadFiles(currentPath)
        // 如果是移动操作，清除选中状态
        if (operation === 'move') {
          selectedFiles.clear()
          selectedFiles = new Set(selectedFiles)
        }
      }
      
      // 显示错误信息（如果有非冲突错误）
      const otherErrors = (result.errors || []).filter(e => !e.conflict)
      if (otherErrors.length > 0) {
        error = otherErrors.map(e => e.error).join('\n')
      }
    } catch (err) {
      error = err.message
    }
  }

  /**
   * 显示覆盖确认对话框
   */
  function showOverwriteDialog(paths, destPath, operation, conflicts) {
    const conflictNames = conflicts.map(c => {
      const path = c.path
      const parts = path.replace(/\\/g, '/').split('/')
      return parts[parts.length - 1] || path
    }).join('\n  • ')
    
    confirmDialog = {
      open: true,
      title: t('moveOrCopyConflict'),
      message: t('moveOrCopyConflictMessage', { files: conflictNames }),
      confirmText: t('overwrite'),
      onConfirm: async () => {
        confirmDialog.open = false
        // 重新执行，这次带 overwrite 标志
        await executeMoveOrCopy(paths, destPath, operation, true)
      },
      onCancel: () => {
        confirmDialog.open = false
      },
    }
  }

  function canWriteCurrentDirectory() {
    return Boolean(currentPath && isInsideWorkspacePath(currentPath))
  }

  async function handleCreateFolder() {
    if (!canWriteCurrentDirectory()) return
    const targetDirPath = currentPath
    const name = prompt(t('enterFolderName'), t('newFolderDefaultName'))?.trim()
    if (!name) return

    try {
      await workspaceApi.mkdir(targetDirPath, name)
      if (pathsEqual(currentPath, targetDirPath)) reloadCurrentDirectory()
      await refreshTreeNodeChildren(targetDirPath)
    } catch (err) {
      console.error('Create folder error:', err)
      error = err.message
    }
  }

  function normalizeUploadPath(path) {
    return String(path || '')
      .replace(/\\/g, '/')
      .split('/')
      .filter((part) => part && part !== '.' && part !== '..')
      .join('/')
  }

  function makeUploadEntries(files, useRelativePath = false, targetDirPath = currentPath) {
    // target_dir_path 已作为 base_dir 发送到后端，target_path 只需文件名即可
    if (!isInsideWorkspacePath(targetDirPath)) return []
    return files.map((file) => {
      const relativeName = useRelativePath && file.webkitRelativePath ? file.webkitRelativePath : file.name
      return {
        file,
        targetPath: normalizeUploadPath(relativeName),
      }
    }).filter((entry) => entry.targetPath)
  }

  async function enqueueUploads(entries, targetDirPath = currentPath) {
    if (!workspacePath || !isInsideWorkspacePath(targetDirPath)) return
    const tasks = entries.map((entry) => ({
      client_id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      upload_id: null,
      file: entry.file,
      file_name: entry.file.name,
      file_size: entry.file.size,
      target_dir_path: targetDirPath,
      target_path: entry.targetPath,
      chunks: [],
      status: 'queued',
      error: '',
      parallel_max_threads: 1,
    }))
    uploadTasks = [...uploadTasks, ...tasks]
    if (!uploadQueueRunning) runUploadQueue()
  }

  async function runUploadQueue() {
    uploadQueueRunning = true
    try {
      while (true) {
        const task = uploadTasks.find((item) => item.status === 'queued')
        if (!task) break
        await startUploadTask(task)
      }
    } finally {
      uploadQueueRunning = false
    }
  }

  function refreshUploads() {
    uploadTasks = [...uploadTasks]
  }

  async function startUploadTask(task) {
    try {
      task.status = 'initializing'
      task.error = ''
      refreshUploads()
      const init = await workspaceApi.uploadInit({
        workspace_id: 'default',
        file_name: task.file_name,
        file_size: task.file_size,
        target_dir_path: task.target_dir_path,
        target_path: task.target_path,
      })
      task.upload_id = init.upload_id
      task.parallel_max_threads = init.parallel_max_threads || 1
      task.chunks = init.chunks.map((chunk) => ({
        ...chunk,
        file_size: task.file_size,
        uploaded: 0,
        status: 'pending',
        request: null,
      }))
      refreshUploads()

      if (task.chunks.length > 0) {
        const completed = await uploadPendingChunks(task)
        if (!completed) return
      }
      if (task.status === 'paused' || task.status === 'cancelled') return

      task.status = 'completing'
      refreshUploads()
      await workspaceApi.uploadComplete(task.upload_id)
      task.status = 'completed'
      task.chunks.forEach((chunk) => {
        chunk.uploaded = chunk.size
        chunk.status = 'completed'
      })
      refreshUploads()
      if (pathsEqual(currentPath, task.target_dir_path)) loadFiles(currentPath)
      await refreshTreeNodeChildren(task.target_dir_path)
    } catch (err) {
      if (err?.name === 'AbortError' || task.status === 'paused' || task.status === 'cancelled') return
      task.status = 'failed'
      task.error = err.message || String(err)
      refreshUploads()
    }
  }

  async function uploadPendingChunks(task) {
    task.status = 'uploading'
    refreshUploads()
    const pending = task.chunks.filter((chunk) => chunk.status !== 'completed')
    try {
      await Promise.all(pending.map((chunk) => uploadChunk(task, chunk)))
      return true
    } catch (err) {
      if (err?.name === 'AbortError' || task.status === 'paused' || task.status === 'cancelled') {
        return false
      }
      task.status = 'failed'
      task.error = err.message || String(err)
      refreshUploads()
      return false
    }
  }

  async function uploadChunk(task, chunk) {
    if (task.status === 'paused' || task.status === 'cancelled') return
    chunk.status = 'uploading'
    chunk.uploaded = 0
    const body = task.file.slice(chunk.offset, chunk.offset + chunk.size)
    const request = workspaceApi.uploadChunk(task.upload_id, chunk, body, (uploaded) => {
      chunk.uploaded = uploaded
      refreshUploads()
    })
    chunk.request = request
    refreshUploads()
    await request.promise
    chunk.uploaded = chunk.size
    chunk.status = 'completed'
    chunk.request = null
    refreshUploads()
  }

  function pauseUpload(task) {
    task.status = 'paused'
    for (const chunk of task.chunks) {
      if (chunk.status === 'uploading') {
        chunk.request?.abort()
        chunk.status = 'paused'
      }
    }
    refreshUploads()
  }

  async function resumeUpload(task) {
    if (!task.upload_id) {
      task.status = 'queued'
      refreshUploads()
      if (!uploadQueueRunning) runUploadQueue()
      return
    }
    for (const chunk of task.chunks) {
      if (chunk.status !== 'completed') {
        chunk.status = 'pending'
        chunk.uploaded = 0
      }
    }
    const completed = await uploadPendingChunks(task)
    if (!completed) return
    task.status = 'completing'
    refreshUploads()
    try {
      await workspaceApi.uploadComplete(task.upload_id)
      task.status = 'completed'
      refreshUploads()
      if (pathsEqual(currentPath, task.target_dir_path)) loadFiles(currentPath)
      await refreshTreeNodeChildren(task.target_dir_path)
    } catch (err) {
      task.status = 'failed'
      task.error = err.message || String(err)
      refreshUploads()
    }
  }

  async function cancelUpload(task) {
    task.status = 'cancelled'
    for (const chunk of task.chunks) {
      chunk.request?.abort()
    }
    refreshUploads()
    if (task.upload_id) {
      try {
        await workspaceApi.uploadCancel(task.upload_id)
      } catch (err) {
        console.warn('Failed to cancel upload:', err)
      }
    }
    uploadTasks = uploadTasks.filter((item) => item.client_id !== task.client_id)
  }

  function retryUpload(task) {
    task.status = task.upload_id ? 'paused' : 'queued'
    task.error = ''
    if (!task.upload_id) {
      refreshUploads()
      if (!uploadQueueRunning) runUploadQueue()
      return
    }
    resumeUpload(task)
  }

  function getUploadProgress(task) {
    if (task.file_size === 0) return 100
    const uploaded = task.chunks.reduce((sum, chunk) => sum + Math.min(chunk.uploaded || 0, chunk.size || 0), 0)
    return Math.min(100, Math.floor((uploaded / task.file_size) * 100))
  }

  function getUploadStatusLabel(task) {
    const labels = {
      queued: t('uploadQueued'),
      initializing: t('uploadStarting'),
      uploading: t('uploading'),
      paused: t('uploadPaused'),
      completing: t('uploadCompleting'),
      completed: t('uploadCompleted'),
      failed: t('uploadFailed'),
      cancelled: t('uploadCancelled'),
    }
    return labels[task.status] || task.status
  }

  function handleUploadFile() {
    if (!canWriteCurrentDirectory()) return
    const targetDirPath = currentPath
    const input = document.createElement('input')
    input.type = 'file'
    input.multiple = true
    input.onchange = (e) => enqueueUploads(makeUploadEntries(Array.from(e.target.files || []), false, targetDirPath), targetDirPath)
    input.click()
  }

  function handleUploadFolder() {
    if (!canWriteCurrentDirectory()) return
    const targetDirPath = currentPath
    const input = document.createElement('input')
    input.type = 'file'
    input.webkitdirectory = true
    input.onchange = (e) => enqueueUploads(makeUploadEntries(Array.from(e.target.files || []), true, targetDirPath), targetDirPath)
    input.click()
  }

  async function handlePasteUpload() {
    if (!canWriteCurrentDirectory()) return
    const targetDirPath = currentPath
    try {
      if (navigator.clipboard.read) {
        const clipboardItems = await navigator.clipboard.read()
        for (const item of clipboardItems) {
          for (const type of item.types) {
            if (type.startsWith('image/')) {
              const blob = await item.getType(type)
              const ext = type.split('/')[1] || 'png'
              const file = new File([blob], `pasted-image-${Date.now()}.${ext}`, { type })
              enqueueUploads(makeUploadEntries([file], false, targetDirPath), targetDirPath)
              return
            }
          }
        }
      }
      const text = await navigator.clipboard.readText()
      if (!text) {
        error = t('clipboardEmpty')
        return
      }
      const file = new File([text], `pasted-text-${Date.now()}.txt`, { type: 'text/plain' })
      enqueueUploads(makeUploadEntries([file], false, targetDirPath), targetDirPath)
    } catch (err) {
      error = `${t('clipboardUploadFailed')}: ${err.message || err}`
    }
  }

  // 滚动到底部加载更多
  function handleScroll(e) {
    const { scrollTop, scrollHeight, clientHeight } = e.target
    if (!searchMode && scrollHeight - scrollTop - clientHeight < 50 && hasMore && !loading) {
      page++
      loadFiles(currentPath, true)
    }
  }

  function getTreeIcon(node) {
    if (node.symlink_target) return '🔗'
    return node.depth === 0 ? '💾' : '📁'
  }

  function getTreeIconLabel(node) {
    if (node.symlink_target) return `${t('linkTarget')}: ${node.symlink_target}`
    return node.depth === 0 ? t('rootDirectory') : t('directory')
  }

  function getFileSecondaryText(file) {
    if (file.symlink_target) return file.symlink_target
    return file.is_dir ? '' : formatSize(file.size)
  }

  function getFileSecondaryTitle(file) {
    if (file.symlink_target) return `${t('linkTarget')}: ${file.symlink_target}`
    return ''
  }

  // 双击文件处理
  function handleDoubleClick(file) {
    if (file.is_dir) {
      enterDirectory(file.path)
    } else if (isPreviewable(file)) {
      previewFileContent(file)
    }
  }

  // 获取文件图标
  function getFileIcon(file) {
    if (file.symlink_target) return '🔗'
    if (file.is_dir) return '📁'
    if (file.is_image) return '🖼️'
    if (file.is_audio) return '🎵'
    if (file.is_video) return '🎬'
    if (isPdfFile(file.name)) return '📕'
    if (isDocxFile(file.name)) return '📘'
    if (file.is_text) return '📄'
    return '📎'
  }

  // 格式化文件时间 MM/DD HH:mm:ss
  function formatFileTime(timestamp) {
    if (!timestamp) return ''
    try {
      const date = new Date(timestamp)
      if (isNaN(date.getTime())) return ''
      const year = String(date.getFullYear()).slice(-2)
      const month = String(date.getMonth() + 1).padStart(2, '0')
      const day = String(date.getDate()).padStart(2, '0')
      const hours = String(date.getHours()).padStart(2, '0')
      const minutes = String(date.getMinutes()).padStart(2, '0')
      const seconds = String(date.getSeconds()).padStart(2, '0')
      return `${year}/${month}/${day} ${hours}:${minutes}:${seconds}`
    } catch {
      return ''
    }
  }

  // 格式化文件大小
  function formatSize(bytes) {
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
  }
</script>

<div class="workspace-panel" style="display: {open ? '' : 'none'}">
    <!-- 顶栏 -->
    <div class="panel-header">
      <div class="header-left">
        <span class="header-icon">📂</span>
        <span class="header-title">{t('workspaceFileManager')}</span>
        {#if currentPath}
          <button class="header-btn refresh-btn" onclick={handleRefresh} disabled={refreshing} title={t('refreshCurrentTab')}>
            {refreshing ? '⏳' : '🔄'}
          </button>
          <span class="current-path">{currentPath}</span>
        {/if}
      </div>
      
      <div class="header-actions">
        <!-- 文件名过滤器 + 内容搜索 -->
        <input
          class="inline-search-input filename-filter-input"
          class:has-value={nameFilterQuery.trim()}
          type="text"
          bind:value={nameFilterQuery}
          oninput={handleNameFilterInput}
          onkeydown={handleNameFilterKeydown}
          placeholder={t('filterFileNames')}
          title={t('filterFileNames') + ' — Enter: ' + t('searchFiles')}
        />
        {#if searchOpen}
            <input
              class="inline-search-input"
              class:has-value={searchQuery.trim()}
              type="text"
              bind:this={searchInputEl}
              bind:value={searchQuery}
              onkeydown={handleSearchKeydown}
              placeholder={t('searchFiles')}
            />
        {/if}
        <button class="header-btn" class:active={searchOpen} onclick={toggleSearch} title={t('search')}>
          🔍
        </button>
        <button class="header-btn" class:active={sortByTimeDesc} onclick={toggleTimeSort} title={t('sortByTimeDesc')}>
          🕒
        </button>
        
        <!-- 视图切换 -->
        <button class="header-btn" class:active={viewMode === 'list'} onclick={() => viewMode = 'list'} title={t('listView')}>
          ☰
        </button>
        <button class="header-btn" class:active={viewMode === 'grid'} onclick={() => viewMode = 'grid'} title={t('gridView')}>
          ⊞
        </button>
        
        <!-- 上传按钮组 -->
        <div class="upload-group">
          <button class="header-btn" onclick={handleCreateFolder} disabled={!canWriteCurrentDirectory()} title={t('newFolder')}>
            📁+
          </button>
          <button class="header-btn" onclick={handleUploadFile} disabled={!canWriteCurrentDirectory()} title={t('uploadFile')}>
            📤
          </button>
          <button class="header-btn" onclick={handleUploadFolder} disabled={!canWriteCurrentDirectory()} title={t('uploadFolder')}>
            📂
          </button>
          <button class="header-btn" onclick={handlePasteUpload} disabled={!canWriteCurrentDirectory()} title={t('pasteUpload')}>
            📋
          </button>
        </div>
        
        <!-- 选择确认按钮 -->
        {#if selectedFiles.size > 0}
          <button 
            class="header-btn primary" 
            onclick={confirmSelectFiles} 
            disabled={!isCurrentPathInWorkspace}
            title={!isCurrentPathInWorkspace ? t('onlyWorkspaceFilesCanBeSelected') : ''}
          >
            {t('selectFiles')} ({selectedFiles.size})
          </button>
        {/if}
        
        <button class="panel-close" onclick={() => onClose?.()} title={t('close')}>✕</button>
      </div>
    </div>

    <!-- 主体区域 -->
    <div class="panel-body">
      <!-- 左侧：文件列表 -->
      <div class="panel-left">
        {#if error}
          <div class="error-message">{error}</div>
        {/if}

        {#if uploadTasks.length > 0}
          <div class="upload-queue">
            {#each uploadTasks as task (task.client_id)}
              <div class="upload-task" class:failed={task.status === 'failed'}>
                <div class="upload-task-main">
                  <div class="upload-task-title" title={task.target_path}>{task.file_name}</div>
                  <div class="upload-task-meta">
                    {getUploadStatusLabel(task)} · {getUploadProgress(task)}% · {formatSize(task.file_size)}
                  </div>
                  {#if task.error}
                    <div class="upload-task-error">{task.error}</div>
                  {/if}
                  <div class="upload-progress">
                    <div class="upload-progress-fill" style="width: {getUploadProgress(task)}%"></div>
                  </div>
                </div>
                <div class="upload-task-actions">
                  {#if task.status === 'uploading'}
                    <button onclick={() => pauseUpload(task)}>{t('pauseUpload')}</button>
                  {:else if task.status === 'paused'}
                    <button onclick={() => resumeUpload(task)}>{t('resumeUpload')}</button>
                  {:else if task.status === 'failed'}
                    <button onclick={() => retryUpload(task)}>{t('retryUpload')}</button>
                  {/if}
                  {#if task.status !== 'completed'}
                    <button onclick={() => cancelUpload(task)}>{t('cancelUpload')}</button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}

        {#if loading && files.length === 0}
          <div class="loading">{t('loading')}</div>
        {:else}
          <div class="file-list" onscroll={handleScroll}>
            <!-- 返回上级按钮 -->
            {#if currentPath && currentPath !== '/' && !(/^[A-Z]:\\?$/i.test(currentPath))}
              <button class="file-item" onclick={goUp}>
                <span class="file-icon">⬆️</span>
                <span class="file-name">..</span>
              </button>
            {/if}

            <!-- 文件列表视图 -->
            {#if viewMode === 'list'}
              {#each displayedFiles as file (file.path)}
                <button 
                  class="file-item"
                  class:selected={selectedFiles.has(file.path)}
                  class:directory={file.is_dir}
                  class:outside-workspace={!file.is_dir && !isInsideWorkspacePath(file.path)}
                  class:dragging={dragState.isDragging && dragState.dragPaths.includes(file.path)}
                  draggable="true"
                  onclick={(e) => file.is_dir ? enterDirectory(file.path) : handleFileClick(file, e)}
                  ondblclick={() => handleDoubleClick(file)}
                  oncontextmenu={(e) => showContextMenu(e, file)}
                  ondragstart={(e) => handleFileDragStart(e, file)}
                  ondrag={handleFileDrag}
                  ondragend={handleFileDragEnd}
                >
                  <span class="file-icon" role="img" aria-label={file.symlink_target ? getFileSecondaryTitle(file) : undefined} title={file.symlink_target ? getFileSecondaryTitle(file) : undefined}>{getFileIcon(file)}</span>
                  <span class="file-name">{file.name}</span>
                  <span class="file-size" title={getFileSecondaryTitle(file)}>{getFileSecondaryText(file)}</span>
                  <span class="file-date">{formatFileTime(file.modified)}</span>
                </button>
              {/each}
            
            <!-- 网格视图 -->
            {:else if viewMode === 'grid' && !previewFile}
              <div class="grid-container">
                {#each displayedFiles as file (file.path)}
                  <button 
                    class="grid-item"
                    class:selected={selectedFiles.has(file.path)}
                    class:outside-workspace={!file.is_dir && !isInsideWorkspacePath(file.path)}
                    class:dragging={dragState.isDragging && dragState.dragPaths.includes(file.path)}
                    draggable="true"
                    onclick={(e) => file.is_dir ? enterDirectory(file.path) : handleFileClick(file, e)}
                    ondblclick={() => handleDoubleClick(file)}
                    oncontextmenu={(e) => showContextMenu(e, file)}
                    ondragstart={(e) => handleFileDragStart(e, file)}
                    ondrag={handleFileDrag}
                    ondragend={handleFileDragEnd}
                  >
                    {#if file.is_image}
                      <div class="grid-thumbnail" style="background-image: url({workspaceApi.thumbnail(file.path, false)})"></div>
                    {:else}
                      <div class="grid-icon">{getFileIcon(file)}</div>
                    {/if}
                    <div class="grid-info">
                      <span class="grid-name">{file.name}</span>
                      <span class="grid-size" title={getFileSecondaryTitle(file)}>{getFileSecondaryText(file)}</span>
                    </div>
                  </button>
                {/each}
              </div>
            {/if}

            {#if loading && files.length > 0}
              <div class="loading-more">{t('loading')}</div>
            {/if}
          </div>
        {/if}
      </div>

      <!-- 右侧：目录树 -->
      <div class="panel-right">
        <div class="tree-container">
          {#each treeNodes as node (node.path)}
            <div 
              class="tree-node"
              class:active={pathsEqual(currentPath, node.path)}
              class:workspace={node.isWorkspace}
              class:drop-target={dragState.isDragging && pathsEqual(dragState.dropTargetPath, node.path)}
              class:drop-invalid={dragState.isDragging && dragState.dragPaths.some(p => pathsEqual(p, node.path) || pathStartsWith(p, node.path))}
              style="padding-left: {8 + node.depth * 16}px"
              ondragover={(e) => handleTreeDragOver(e, node)}
              ondragleave={(e) => handleTreeDragLeave(e, node)}
              ondrop={(e) => handleTreeDrop(e, node)}
            >
              <button 
                class="tree-toggle"
                onclick={(e) => { e.stopPropagation(); toggleTreeNode(node) }}
              >
                {#if node.loading}
                  <span class="tree-spinner">⏳</span>
                {:else}
                  <span class="tree-arrow" class:expanded={node.expanded}>
                    {node.expanded ? '▼' : '▶'}
                  </span>
                {/if}
              </button>
              <button 
                class="tree-label"
                onclick={() => enterDirectory(node.path)}
                ondblclick={() => setAsWorkspace(node.path)}
                title={node.path}
              >
                <span class="tree-icon" role="img" aria-label={getTreeIconLabel(node)} title={getTreeIconLabel(node)}>{getTreeIcon(node)}</span>
                <span class="tree-name">{node.name}</span>
                {#if node.isWorkspace}
                  <span class="ws-tag">{t('currentWorkspace')}</span>
                {/if}
              </button>
            </div>
          {/each}
        </div>
      </div>
    </div>

    <!-- 预览模式 -->
    {#if previewFile}
      {@const previewPathParts = getPreviewPathParts(previewFile)}
      <div class="preview-overlay">
        <div class="preview-header">
          <div class="preview-file-path" title={previewFile.path || previewFile.name}>
            <span>{previewPathParts.directory}</span><strong>{previewPathParts.name}</strong>
          </div>
          <div class="preview-header-actions">
            {#if previewFile.is_text}
              <div class="preview-search-controls">
                <button
                  class="preview-search-nav"
                  onclick={() => movePreviewSearch(-1)}
                  disabled={!previewSearchTotal}
                  title="上一个匹配项"
                  aria-label="上一个匹配项"
                >←</button>
                <div class="preview-search-input-wrap">
                  <input
                    class="preview-search-input"
                    type="text"
                    placeholder={t('search')}
                    bind:value={previewSearchQuery}
                    oninput={updatePreviewSearch}
                    onkeydown={handlePreviewSearchKeydown}
                    aria-label="搜索预览内容"
                  />
                  {#if previewSearchQuery}
                    <span class="preview-search-count">
                      {previewSearchTotal ? previewSearchCurrent + 1 : 0}/{previewSearchTotal}
                    </span>
                  {/if}
                </div>
                <button
                  class="preview-search-nav"
                  onclick={() => movePreviewSearch(1)}
                  disabled={!previewSearchTotal}
                  title="下一个匹配项"
                  aria-label="下一个匹配项"
                >→</button>
              </div>
              <button class="preview-copy-btn" onclick={() => copyToClipboard(previewContent)} title={t('copy')}>📋</button>
            {/if}
            <button class="preview-download-btn" onclick={() => downloadFile(previewFile)} title={t('download')} aria-label={t('download')}>⬇</button>
            <button onclick={() => closePreview()}>✕</button>
          </div>
        </div>
        {#if previewFile.is_image}
          <div class="preview-content"><img src={workspaceApi.content(previewFile.path, false)} alt={previewFile.name} /></div>
        {:else if previewFile.is_video}
          <div class="preview-content"><video src={workspaceApi.content(previewFile.path, false)} controls></video></div>
        {:else if previewFile.is_audio}
          <div class="preview-content"><audio src={workspaceApi.content(previewFile.path, false)} controls></audio></div>
        {:else if previewFile.is_pdf || previewFile.is_docx}
          <DocumentPreview file={previewFile} url={workspaceApi.content(previewFile.path, false)} />
        {:else if previewFile.is_text}
          <div class="text-preview" bind:this={textPreviewEl}>{@html renderPreviewHtml(previewContent, previewFile.name, previewFile.forcePlainText, previewFile.path)}</div>
        {/if}
      </div>
    {/if}
  </div>

<!-- 右键菜单和背景层：放在组件根级别，避免被 panel 的层叠上下文限制 -->
{#if contextMenu.visible}
  {@const menuFile = contextMenu.file}
  {@const isMulti = selectedFiles.size > 1 && selectedFiles.has(menuFile?.path)}
  <div class="context-menu-backdrop" onmousedown={hideContextMenu}></div>
  <div
    class="context-menu"
    style="left: {contextMenu.x}px; top: {contextMenu.y}px"
    onmousedown={(e) => e.stopPropagation()}
  >
    <!-- 预览：多选时置灰 -->
    {#if isPreviewable(menuFile)}
      <button 
        disabled={isMulti}
        onmousedown={() => { if (!isMulti) { previewFileContent(menuFile); hideContextMenu() } }}
      >
        {t('preview')}
      </button>
    {/if}
    <!-- 按ASCII文本打开：多选时置灰，非目录时可用 -->
    {#if !menuFile?.is_dir}
      <button 
        disabled={isMulti}
        onmousedown={() => { if (!isMulti) { openAsTextFile(menuFile); hideContextMenu() } }}
      >
        {t('openAsText')}
      </button>
    {/if}
    <!-- 下载：支持多选 -->
    <button onmousedown={() => { 
      if (isMulti) { downloadSelectedFiles() } else { downloadFile(menuFile) }
      hideContextMenu() 
    }}>
      {t('download')}{isMulti ? ` (${selectedFiles.size})` : ''}
    </button>
    <!-- 复制绝对路径：支持多选 -->
    <button onmousedown={() => { 
      if (isMulti) { copySelectedPaths() } else { copyToClipboard(menuFile?.path || '') }
      hideContextMenu() 
    }}>
      {t('copyAbsolutePath')}{isMulti ? ` (${selectedFiles.size})` : ''}
    </button>
    <!-- 复制相对路径：支持多选 -->
    <button onmousedown={() => { 
      if (isMulti) { copySelectedRelativePaths() } else { 
        const relativePath = relativeWorkspacePath(menuFile?.path || '')
        copyToClipboard(relativePath)
      }
      hideContextMenu() 
    }}>
      {t('copyRelativePath')}{isMulti ? ` (${selectedFiles.size})` : ''}
    </button>
    <!-- 重命名：多选时置灰 -->
    <button 
      disabled={isMulti}
      onmousedown={() => { if (!isMulti) renameFile(menuFile) }}
    >
      {t('rename')}
    </button>
    <!-- 创建副本：支持多选 -->
    <button onmousedown={() => { 
      if (isMulti) { duplicateSelectedFiles() } else { duplicateFile(menuFile) }
      hideContextMenu() 
    }}>
      {t('duplicate')}{isMulti ? ` (${selectedFiles.size})` : ''}
    </button>
    <!-- 删除：支持多选 -->
    <button class="danger" onmousedown={() => { 
      if (isMulti) { deleteSelectedFiles() } else { deleteFile(menuFile) }
      hideContextMenu() 
    }}>
      {t('delete')}{isMulti ? ` (${selectedFiles.size})` : ''}
    </button>
  </div>
{/if}

<!-- 确认对话框 -->
<ConfirmDialog
  open={confirmDialog.open}
  title={confirmDialog.title}
  message={confirmDialog.message}
  confirmText={confirmDialog.confirmText}
  cancelText={t('cancel')}
  onConfirm={confirmDialog.onConfirm}
  onCancel={confirmDialog.onCancel}
/>

<style>
  .workspace-panel {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: calc(100% - 40px); /* 减去系统提示词行高 */
    background: var(--bg);
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.12);
    z-index: 10;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 16px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    background: var(--bg-secondary);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .header-icon {
    font-size: 1.2rem;
  }

  .header-title {
    font-weight: 600;
    font-size: 0.9rem;
  }

  .current-path {
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-family: monospace;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .refresh-btn {
    width: 26px;
    height: 24px;
    padding: 0;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 0.85rem;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .refresh-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  .refresh-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }

  .header-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 8px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.85rem;
    transition: all 0.2s;
  }

  .header-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  .header-btn:disabled {
    color: var(--text-secondary);
    opacity: 0.6;
    cursor: not-allowed;
  }

  .header-btn.active {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  .header-btn.primary {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  .header-btn.primary:disabled {
    background: none;
    color: var(--text-secondary);
    border-color: var(--border);
    opacity: 0.6;
    cursor: not-allowed;
  }

  .header-btn.primary:disabled:hover {
    background: none;
    color: var(--text-secondary);
    border-color: var(--border);
  }

  .upload-group {
    display: flex;
    gap: 2px;
    margin-left: 8px;
    padding-left: 8px;
    border-left: 1px solid var(--border);
  }

  .panel-close {
    background: none;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 1rem;
    padding: 2px 6px;
    border-radius: 4px;
    margin-left: 8px;
    line-height: 1;
    flex-shrink: 0;
  }

  .panel-close:hover {
    background: var(--border);
    color: var(--text);
  }

  .inline-search-input {
    width: 160px;
    padding: 4px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    font-size: 0.82rem;
    background: var(--bg-secondary);
    color: var(--text);
    outline: none;
    transition: border-color 0.15s, width 0.2s;
  }

  .inline-search-input:focus {
    border-color: var(--primary);
  }

  .inline-search-input.has-value {
    border-color: var(--danger, #e74c3c);
  }

  .inline-search-input.has-value:focus {
    border-color: var(--danger, #e74c3c);
  }

  .filename-filter-input {
    width: 150px;
  }

  .upload-queue {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 8px;
    background: var(--bg-secondary);
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 6px;
    z-index: 5;
    max-height: 50%;
    overflow-y: auto;
  }

  .upload-task {
    display: flex;
    gap: 8px;
    align-items: center;
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
  }

  .upload-task.failed {
    border-color: #d44;
  }

  .upload-task-main {
    flex: 1;
    min-width: 0;
  }

  .upload-task-title {
    font-size: 0.82rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .upload-task-meta, .upload-task-error {
    font-size: 0.72rem;
    color: var(--text-secondary);
  }

  .upload-task-error {
    color: #d44;
  }

  .upload-progress {
    height: 4px;
    margin-top: 4px;
    background: var(--border);
    border-radius: 999px;
    overflow: hidden;
  }

  .upload-progress-fill {
    height: 100%;
    background: var(--primary);
    transition: width 0.2s;
  }

  .upload-task-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
  }

  .upload-task-actions button {
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg-secondary);
    color: var(--text);
    cursor: pointer;
    font-size: 0.72rem;
    padding: 3px 6px;
  }

  .panel-body {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  .panel-left {
    flex: 1;
    overflow-y: auto;
    min-width: 0;
    position: relative;
  }

  .panel-right {
    width: 260px;
    flex-shrink: 0;
    border-left: 1px solid var(--border);
    overflow: hidden;
    background: var(--bg-secondary);
    display: flex;
    flex-direction: column;
  }

  .file-list {
    height: 100%;
    overflow-y: auto;
    padding: 4px;
  }

  .file-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    border: 1px solid transparent;
    border-radius: 3px;
    background: none;
    color: var(--text);
    cursor: pointer;
    width: 100%;
    text-align: left;
    transition: all 0.15s;
    font-size: 0.82rem;
  }

  .file-item:hover {
    background: var(--bg-secondary);
    border-color: var(--border);
  }

  .file-item.selected {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  .file-item.outside-workspace,
  .grid-item.outside-workspace {
    opacity: 0.75;
    border-left: 3px solid var(--warning, #f0ad4e);
  }

  .file-item.outside-workspace:hover,
  .grid-item.outside-workspace:hover {
    opacity: 1;
  }

  .file-item.directory .file-name {
    font-weight: 500;
  }

  .file-icon {
    font-size: 1.1rem;
    flex-shrink: 0;
  }

  .file-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.85rem;
  }

  .file-size {
    font-size: 0.75rem;
    color: var(--text-secondary);
    flex-shrink: 0;
    min-width: 60px;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: right;
  }

  .file-date {
    font-size: 0.75rem;
    color: var(--text-secondary);
    flex-shrink: 0;
    min-width: 130px;
    text-align: right;
  }

  .grid-container {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 8px;
    padding: 8px;
  }

  .grid-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg);
    cursor: pointer;
    transition: all 0.2s;
  }

  .grid-item:hover {
    border-color: var(--primary);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  }

  .grid-item.selected {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  .grid-thumbnail {
    width: 80px;
    height: 80px;
    background-size: cover;
    background-position: center;
    border-radius: 4px;
    margin-bottom: 8px;
  }

  .grid-icon {
    font-size: 2rem;
    margin-bottom: 8px;
  }

  .grid-info {
    text-align: center;
    width: 100%;
  }

  .grid-name {
    display: block;
    font-size: 0.8rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .grid-size {
    display: block;
    font-size: 0.7rem;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tree-container {
    padding: 4px;
    overflow-y: auto;
    flex: 1;
  }

  .tree-node {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 4px 8px;
    border-radius: 3px;
    background: none;
    transition: background 0.15s;
  }

  .tree-node:hover {
    background: var(--bg-secondary);
  }

  .tree-node.active {
    background: var(--bg-secondary);
  }

  .tree-node.workspace {
    background: color-mix(in srgb, var(--primary) 12%, transparent);
  }

  .tree-node.workspace:hover {
    background: color-mix(in srgb, var(--primary) 20%, transparent);
  }

  .tree-toggle {
    background: none;
    border: none;
    cursor: pointer;
    padding: 1px;
    font-size: 0.55rem;
    color: var(--text-secondary);
    width: 16px;
    height: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    border-radius: 2px;
  }

  .tree-toggle:hover {
    background: var(--border);
  }

  .tree-arrow {
    transition: transform 0.15s;
    font-size: 0.55rem;
  }

  .tree-spinner {
    font-size: 0.65rem;
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  .tree-label {
    display: flex;
    align-items: center;
    gap: 4px;
    background: none;
    border: none;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 3px;
    color: var(--text);
    font-size: 0.82rem;
    flex: 1;
    min-width: 0;
    text-align: left;
  }

  .tree-label:hover {
    background: var(--border);
  }

  .tree-node.workspace .tree-label {
    font-weight: 600;
    color: var(--primary);
  }

  .tree-icon {
    font-size: 0.85rem;
    flex-shrink: 0;
  }

  .tree-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ws-tag {
    font-size: 0.6rem;
    background: var(--primary);
    color: #fff;
    padding: 1px 5px;
    border-radius: 3px;
    flex-shrink: 0;
    margin-left: 2px;
  }

  .preview-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--bg);
    z-index: 20;
    display: flex;
    flex-direction: column;
  }

  .preview-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
  }

  .preview-file-path {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-secondary);
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.82rem;
  }

  .preview-file-path strong {
    color: var(--text);
    font-weight: 700;
  }

  .preview-header button {
    background: none;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    color: var(--text);
  }

  .preview-header button:disabled {
    opacity: 0.35;
    cursor: default;
  }

  .preview-header-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
    margin-left: 12px;
  }

  .preview-search-controls {
    display: flex;
    align-items: center;
    gap: 3px;
  }

  .preview-search-input-wrap {
    position: relative;
    display: flex;
    align-items: center;
  }

  .preview-search-input {
    width: 180px;
    height: 26px;
    box-sizing: border-box;
    padding: 3px 48px 3px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--text);
    font-size: 0.8rem;
    outline: none;
  }

  .preview-search-input:focus {
    border-color: var(--primary);
    box-shadow: 0 0 0 1px var(--primary);
  }

  .preview-search-count {
    position: absolute;
    right: 7px;
    color: var(--text-secondary);
    font-size: 0.7rem;
    pointer-events: none;
  }

  .preview-header .preview-search-nav {
    width: 24px;
    height: 24px;
    padding: 0;
    border-radius: 4px;
    font-size: 0.9rem;
    line-height: 1;
  }

  .preview-header .preview-search-nav:hover:not(:disabled) {
    background: var(--bg-tertiary, rgba(0,0,0,0.1));
  }

  .preview-copy-btn,
  .preview-download-btn {
    font-size: 0.85rem !important;
    padding: 2px 6px;
    border-radius: 4px;
    transition: background 0.15s;
  }
  .preview-copy-btn:hover,
  .preview-download-btn:hover {
    background: var(--bg-tertiary, rgba(0,0,0,0.1));
  }

  .preview-content {
    flex: 1;
    overflow: auto;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }

  .preview-content img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }

  .preview-content video {
    max-width: 100%;
    max-height: 100%;
  }

  .text-preview {
    flex: 1;
    overflow: auto;
    background: var(--bg);
    color: var(--text);
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
    word-break: break-word;
  }

  .text-preview :global(mark.preview-search-match) {
    background: #ffe066;
    color: #1f2328;
    border-radius: 2px;
    padding: 0;
  }

  .text-preview :global(mark.preview-search-match.current) {
    background: #ff922b;
    color: #111;
    outline: 1px solid #e8590c;
  }

  /* Two-column code layout */
  .text-preview :global(.code-container) {
    display: flex;
    min-height: 100%;
  }
  .text-preview :global(.line-gutter) {
    flex-shrink: 0;
    width: calc(var(--digits, 3) * 0.7em + 1.2em);
    padding: 12px 0.4em 12px 0.6em;
    text-align: right;
    background: var(--bg-secondary);
    color: var(--text-secondary, #888);
    font-size: inherit;
    line-height: inherit;
    user-select: none;
    border-right: 1px solid var(--border, rgba(0,0,0,0.08));
  }
  .text-preview :global(.line-gutter span) {
    display: block;
  }
  .text-preview :global(.code-body) {
    flex: 1;
    margin: 0;
    padding: 12px 16px;
    overflow-x: auto;
    white-space: pre;
    word-wrap: normal;
    background: transparent;
  }
  .text-preview :global(.code-body code) {
    font-family: inherit;
    font-size: inherit;
    background: transparent;
    padding: 0;
    border-radius: 0;
  }

  /* Markdown rendering */
  .text-preview :global(h1),
  .text-preview :global(h2),
  .text-preview :global(h3),
  .text-preview :global(h4),
  .text-preview :global(h5),
  .text-preview :global(h6) {
    margin: 0.8em 0 0.4em;
    font-weight: 600;
    line-height: 1.3;
    font-family: inherit;
  }
  .text-preview :global(h1) { font-size: 1.4em; }
  .text-preview :global(h2) { font-size: 1.25em; }
  .text-preview :global(h3) { font-size: 1.1em; }
  .text-preview :global(p) { margin: 0.4em 0; font-family: inherit; }
  .text-preview :global(ul),
  .text-preview :global(ol) { margin: 0.4em 0; padding-left: 1.5em; }
  .text-preview :global(li) { margin: 0.2em 0; }
  .text-preview :global(a) { color: var(--primary, #4a9eff); text-decoration: underline; }
  .text-preview :global(code) {
    background: var(--bg-tertiary, rgba(0,0,0,0.1));
    padding: 0.15em 0.35em;
    border-radius: 3px;
    font-size: 0.88em;
    font-family: 'Fira Code', 'Consolas', monospace;
  }
  .text-preview :global(.code-block) {
    position: relative;
    margin: 0.6em 0;
  }
  .text-preview :global(.code-block pre) {
    background: var(--bg-tertiary, rgba(0,0,0,0.08));
    padding: 0.8em 1em;
    border-radius: 4px;
    overflow-x: auto;
    margin: 0;
  }
  .text-preview :global(.code-block pre code) {
    background: none;
    padding: 0;
    font-size: 0.85em;
    line-height: 1.5;
  }
  .text-preview :global(blockquote) {
    margin: 0.5em 0;
    padding: 0.3em 0.8em;
    border-left: 3px solid var(--primary, #4a9eff);
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.04));
    border-radius: 0 4px 4px 0;
  }
  .text-preview :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 0.6em 0;
    font-size: 0.88em;
  }
  .text-preview :global(th),
  .text-preview :global(td) {
    border: 1px solid var(--border, #ddd);
    padding: 0.4em 0.7em;
    text-align: left;
  }
  .text-preview :global(th) {
    background: var(--bg-tertiary, rgba(0,0,0,0.06));
    font-weight: 600;
  }
  .text-preview :global(hr) {
    border: none;
    border-top: 1px solid var(--border, #ddd);
    margin: 0.8em 0;
  }
  .text-preview :global(strong) { font-weight: 600; }
  .text-preview :global(img) { max-width: 100%; border-radius: 4px; }

  /* Syntax highlighting - dark theme */
  .text-preview :global(.hl-keyword)   { color: #c792ea; }
  .text-preview :global(.hl-string)    { color: #c3e88d; }
  .text-preview :global(.hl-comment)   { color: #546e7a; font-style: italic; }
  .text-preview :global(.hl-number)    { color: #f78c6c; }
  .text-preview :global(.hl-boolean)   { color: #ff5874; }
  .text-preview :global(.hl-null)      { color: #ff5874; }
  .text-preview :global(.hl-key)       { color: #82aaff; }
  .text-preview :global(.hl-variable)  { color: #f07178; }
  .text-preview :global(.hl-type)      { color: #ffcb6b; }
  .text-preview :global(.hl-decorator) { color: #ffcb6b; }
  .text-preview :global(.hl-tag)       { color: #f07178; }
  .text-preview :global(.hl-attribute) { color: #c792ea; }

  /* Syntax highlighting - light theme */
  :root[data-theme="light"] .text-preview :global(.hl-keyword)   { color: #7c3aed; }
  :root[data-theme="light"] .text-preview :global(.hl-string)    { color: #16a34a; }
  :root[data-theme="light"] .text-preview :global(.hl-comment)   { color: #6b7280; font-style: italic; }
  :root[data-theme="light"] .text-preview :global(.hl-number)    { color: #c2410c; }
  :root[data-theme="light"] .text-preview :global(.hl-boolean)   { color: #dc2626; }
  :root[data-theme="light"] .text-preview :global(.hl-null)      { color: #dc2626; }
  :root[data-theme="light"] .text-preview :global(.hl-key)       { color: #1d4ed8; }
  :root[data-theme="light"] .text-preview :global(.hl-variable)  { color: #b45309; }
  :root[data-theme="light"] .text-preview :global(.hl-type)      { color: #b45309; }
  :root[data-theme="light"] .text-preview :global(.hl-decorator) { color: #b45309; }
  :root[data-theme="light"] .text-preview :global(.hl-tag)       { color: #dc2626; }
  :root[data-theme="light"] .text-preview :global(.hl-attribute) { color: #7c3aed; }

  .context-menu {
    position: fixed;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    z-index: 1000;
    min-width: 150px;
    padding: 4px;
  }

  .context-menu button {
    display: block;
    width: 100%;
    padding: 8px 12px;
    border: none;
    background: none;
    color: var(--text);
    cursor: pointer;
    text-align: left;
    font-size: 0.85rem;
    border-radius: 4px;
  }

  .context-menu button:hover {
    background: var(--bg-secondary);
  }

  .context-menu button.danger {
    color: var(--danger);
  }

  .context-menu button.danger:hover {
    background: var(--danger);
    color: #fff;
  }

  .context-menu button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .context-menu button:disabled:hover {
    background: none;
  }

  .context-menu-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 999;
  }

  .error-message {
    padding: 12px;
    background: var(--danger);
    color: #fff;
    margin: 8px;
    border-radius: 4px;
    font-size: 0.85rem;
  }

  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .loading-more {
    padding: 16px;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
  }

  /* 拖拽相关样式 */
  .file-item.dragging,
  .grid-item.dragging {
    opacity: 0.5;
  }

  .tree-node.drop-target {
    background: var(--primary-bg, rgba(59, 130, 246, 0.15));
    outline: 2px dashed var(--primary, #3b82f6);
    outline-offset: -2px;
    border-radius: 4px;
  }

  .tree-node.drop-invalid {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
