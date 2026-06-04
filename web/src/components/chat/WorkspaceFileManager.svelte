<script>
  import { tick } from 'svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { workspace as workspaceApi } from '../../lib/api.js'
  import { marked } from 'marked'
  import { highlight, escapeHtml, getFileLang, isMarkdownFile } from '../../lib/highlight.js'

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
  let uploadTasks = $state([])
  let uploadQueueRunning = false

  function normalizePathForCompare(path) {
    return String(path || '').replace(/\\/g, '/').replace(/\/$/, '')
  }

  function isInsideWorkspacePath(path) {
    const normalizedPath = normalizePathForCompare(path)
    const normalizedWorkspace = normalizePathForCompare(workspacePath)
    return normalizedPath === normalizedWorkspace || normalizedPath.startsWith(normalizedWorkspace + '/')
  }

  function relativeWorkspacePath(path) {
    const normalizedPath = normalizePathForCompare(path)
    const normalizedWorkspace = normalizePathForCompare(workspacePath)
    if (normalizedPath === normalizedWorkspace) return ''
    if (!normalizedPath.startsWith(normalizedWorkspace + '/')) return ''
    return normalizedPath.slice(normalizedWorkspace.length + 1)
  }

  function isSelectableFile(file) {
    return !!file && !file.is_dir && isInsideWorkspacePath(file.path)
  }

  function getSortMode() {
    return sortByTimeDesc ? 'recent' : 'name'
  }

  function topLevelNameUnderCurrentPath(filePath, fallbackName = '') {
    const normalizedFilePath = normalizePathForCompare(filePath)
    const normalizedCurrentPath = normalizePathForCompare(currentPath)
    if (normalizedCurrentPath && normalizedFilePath.startsWith(normalizedCurrentPath + '/')) {
      return normalizedFilePath.slice(normalizedCurrentPath.length + 1).split('/')[0] || fallbackName
    }
    if (!normalizedCurrentPath && normalizedFilePath.startsWith('/')) {
      return normalizedFilePath.slice(1).split('/')[0] || fallbackName
    }
    return fallbackName
  }

  function matchesNameFilter(file, isSearchList) {
    const filterText = nameFilterQuery.trim().toLowerCase()
    if (!filterText) return true
    const nameToCheck = isSearchList
      ? topLevelNameUnderCurrentPath(file.path, file.name)
      : file.name
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


  // 初始化：加载工作区根目录
  $effect(() => {
    if (open && workspacePath) {
      if (!currentPath) {
        currentPath = workspacePath
        loadFiles(currentPath)
      }
      if (!treeInitialized) {
        treeInitialized = true
        initTree(workspacePath)
      }
    }
    if (!open) {
      treeInitialized = false
    }
  })

  // 加载文件列表
  async function loadFiles(dirPath, append = false) {
    loading = true
    error = ''
    try {
      const data = await workspaceApi.list(dirPath, page, pageSize, false, {
        sort: getSortMode(),
        nameFilter: nameFilterQuery.trim(),
      })
      
      if (append) {
        files = [...files, ...data.files]
      } else {
        files = data.files
      }
      hasMore = data.has_more
    } catch (err) {
      error = err.message
    } finally {
      loading = false
    }
  }

  // 构造子路径：parentPath + name
  function childPath(parentPath, name) {
    if (parentPath === '/') return `/${name}`
    const sep = parentPath.includes('\\') ? '\\' : '/'
    return `${parentPath}${sep}${name}`
  }

  // 初始化目录树：从根节点逐级展开到工作区路径
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
        isWorkspace: rootPath === wsPath
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
      const nodeIdx = treeNodes.findIndex(n => n.path === segPath)
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
        isWorkspace: path === wsPath,
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
    workspacePath = dirPath
    currentPath = dirPath
    page = 1
    hasMore = true
    loadFiles(dirPath)
    onWorkspaceChange?.(dirPath)
    // 重新初始化树
    treeInitialized = false
    treeNodes = []
  }

  function reloadCurrentDirectory() {
    if (!currentPath) return
    page = 1
    hasMore = true
    loadFiles(currentPath)
  }

  function handleNameFilterInput(e) {
    nameFilterQuery = e.currentTarget.value
    writeLocalStorage(NAME_FILTER_STORAGE_KEY, nameFilterQuery)
    if (nameFilterTimer) clearTimeout(nameFilterTimer)
    nameFilterTimer = setTimeout(() => {
      reloadCurrentDirectory()
    }, 200)
  }

  function handleNameFilterKeydown(e) {
    if (e.key === 'Enter') {
      if (nameFilterTimer) clearTimeout(nameFilterTimer)
      reloadCurrentDirectory()
    } else if (e.key === 'Escape') {
      nameFilterQuery = ''
      writeLocalStorage(NAME_FILTER_STORAGE_KEY, '')
      if (nameFilterTimer) clearTimeout(nameFilterTimer)
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
    if (!searchQuery.trim()) {
      searchMode = false
      searchResults = []
      return
    }
    
    searchMode = true
    loading = true
    try {
      const results = await workspaceApi.search(currentPath, searchQuery)
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
    if (!file.is_text && !file.is_image && !file.is_audio && !file.is_video) {
      return // 不支持预览的文件类型
    }
    
    previewReturnView = viewMode
    previewFile = file
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

  function closePreview() {
    viewMode = previewReturnView
    previewFile = null
    previewContent = ''
  }

  // 渲染预览 HTML
  function renderPreviewHtml(content, filename) {
    if (!content) return ''
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
        return marked.parse(content, { renderer, gfm: true, breaks: true })
      } catch {
        return escapeHtml(content)
      }
    }
    const lang = getFileLang(filename)
    if (lang) {
      return `<pre class="code-preview"><code>${highlight(content, lang)}</code></pre>`
    }
    return `<pre class="code-preview"><code>${escapeHtml(content)}</code></pre>`
  }

  // 下载文件
  function downloadFile(file) {
    const link = document.createElement('a')
    link.href = workspaceApi.download(file.path, false)
    link.download = file.name
    link.click()
  }

  // 选择文件
  function toggleFileSelection(file) {
    if (file.is_dir) return
    if (selectedFiles.has(file.path)) {
      selectedFiles.delete(file.path)
    } else {
      selectedFiles.add(file.path)
    }
    selectedFiles = new Set(selectedFiles) // 触发响应式更新
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
    if (!confirm(t('confirmDeleteFile'))) return
    
    try {
      await workspaceApi.delete(file.path)
      loadFiles(currentPath)
    } catch (err) {
      error = err.message
    }
    hideContextMenu()
  }

  function normalizeUploadPath(path) {
    return String(path || '')
      .replace(/\\/g, '/')
      .split('/')
      .filter((part) => part && part !== '.' && part !== '..')
      .join('/')
  }

  function joinUploadPath(...parts) {
    return parts.map(normalizeUploadPath).filter(Boolean).join('/')
  }

  function currentRelativeDir() {
    const normalizedCurrent = currentPath.replace(/\\/g, '/').replace(/\/$/, '')
    const normalizedWorkspace = workspacePath.replace(/\\/g, '/').replace(/\/$/, '')
    if (normalizedCurrent === normalizedWorkspace) return ''
    if (!normalizedCurrent.startsWith(normalizedWorkspace + '/')) return ''
    return normalizeUploadPath(normalizedCurrent.slice(normalizedWorkspace.length + 1))
  }

  function makeUploadEntries(files, useRelativePath = false) {
    const baseDir = currentRelativeDir()
    return files.map((file) => {
      const relativeName = useRelativePath && file.webkitRelativePath ? file.webkitRelativePath : file.name
      return {
        file,
        targetPath: joinUploadPath(baseDir, relativeName),
      }
    }).filter((entry) => entry.targetPath)
  }

  async function enqueueUploads(entries) {
    if (!workspacePath) return
    const tasks = entries.map((entry) => ({
      client_id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      upload_id: null,
      file: entry.file,
      file_name: entry.file.name,
      file_size: entry.file.size,
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
      loadFiles(currentPath)
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
      loadFiles(currentPath)
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
    const input = document.createElement('input')
    input.type = 'file'
    input.multiple = true
    input.onchange = (e) => enqueueUploads(makeUploadEntries(Array.from(e.target.files || [])))
    input.click()
  }

  function handleUploadFolder() {
    const input = document.createElement('input')
    input.type = 'file'
    input.webkitdirectory = true
    input.onchange = (e) => enqueueUploads(makeUploadEntries(Array.from(e.target.files || []), true))
    input.click()
  }

  async function handlePasteUpload() {
    try {
      if (navigator.clipboard.read) {
        const clipboardItems = await navigator.clipboard.read()
        for (const item of clipboardItems) {
          for (const type of item.types) {
            if (type.startsWith('image/')) {
              const blob = await item.getType(type)
              const ext = type.split('/')[1] || 'png'
              const file = new File([blob], `pasted-image-${Date.now()}.${ext}`, { type })
              enqueueUploads(makeUploadEntries([file]))
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
      enqueueUploads(makeUploadEntries([file]))
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

  // 双击文件处理
  function handleDoubleClick(file) {
    if (file.is_dir) {
      enterDirectory(file.path)
    } else if (file.is_text || file.is_image || file.is_audio || file.is_video) {
      previewFileContent(file)
    }
  }

  // 获取文件图标
  function getFileIcon(file) {
    if (file.is_dir) return '📁'
    if (file.is_image) return '🖼️'
    if (file.is_audio) return '🎵'
    if (file.is_video) return '🎬'
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

{#if open}
  <div class="workspace-panel">
    <!-- 顶栏 -->
    <div class="panel-header">
      <div class="header-left">
        <span class="header-icon">📂</span>
        <span class="header-title">{t('workspaceFileManager')}</span>
        {#if currentPath}
          <span class="current-path">{currentPath}</span>
        {/if}
      </div>
      
      <div class="header-actions">
        <!-- 文件名过滤器 + 内容搜索 -->
        <input
          class="inline-search-input filename-filter-input"
          type="text"
          bind:value={nameFilterQuery}
          oninput={handleNameFilterInput}
          onkeydown={handleNameFilterKeydown}
          placeholder={t('filterFileNames')}
          title={t('filterFileNames')}
        />
        {#if searchOpen}
          <input
            class="inline-search-input"
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
          <button class="header-btn" onclick={handleUploadFile} title={t('uploadFile')}>
            📤
          </button>
          <button class="header-btn" onclick={handleUploadFolder} title={t('uploadFolder')}>
            📂
          </button>
          <button class="header-btn" onclick={handlePasteUpload} title={t('pasteUpload')}>
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
        
        <!-- 关闭按钮 -->
        <button class="panel-close" onclick={() => { onClose?.(); hideContextMenu() }}>✕</button>
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
                  onclick={() => file.is_dir ? enterDirectory(file.path) : toggleFileSelection(file)}
                  ondblclick={() => handleDoubleClick(file)}
                  oncontextmenu={(e) => showContextMenu(e, file)}
                >
                  <span class="file-icon">{getFileIcon(file)}</span>
                  <span class="file-name">{file.name}</span>
                  <span class="file-size">{file.is_dir ? '' : formatSize(file.size)}</span>
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
                    onclick={() => file.is_dir ? enterDirectory(file.path) : toggleFileSelection(file)}
                    ondblclick={() => handleDoubleClick(file)}
                    oncontextmenu={(e) => showContextMenu(e, file)}
                  >
                    {#if file.is_image}
                      <div class="grid-thumbnail" style="background-image: url({workspaceApi.thumbnail(file.path, false)})"></div>
                    {:else}
                      <div class="grid-icon">{getFileIcon(file)}</div>
                    {/if}
                    <div class="grid-info">
                      <span class="grid-name">{file.name}</span>
                      <span class="grid-size">{file.is_dir ? '' : formatSize(file.size)}</span>
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
              class:active={currentPath === node.path}
              class:workspace={node.isWorkspace}
              style="padding-left: {8 + node.depth * 16}px"
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
                <span class="tree-icon">{node.depth === 0 ? '💾' : '📁'}</span>
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
      <div class="preview-overlay">
        <div class="preview-header">
          <span>{previewFile.name}</span>
          <button onclick={() => closePreview()}>✕</button>
        </div>
        <div class="preview-content">
        {#if previewFile.is_image}
            <img src={workspaceApi.content(previewFile.path, false)} alt={previewFile.name} />
          {:else if previewFile.is_video}
            <video src={workspaceApi.content(previewFile.path, false)} controls></video>
          {:else if previewFile.is_audio}
            <audio src={workspaceApi.content(previewFile.path, false)} controls></audio>
          {:else if previewFile.is_text}
            <div class="text-preview">{@html renderPreviewHtml(previewContent, previewFile.name)}</div>
          {/if}
        </div>
      </div>
    {/if}
  </div>
{/if}

<!-- 右键菜单和背景层：放在组件根级别，避免被 panel 的层叠上下文限制 -->
{#if contextMenu.visible}
  {@const menuFile = contextMenu.file}
  <div class="context-menu-backdrop" onmousedown={hideContextMenu}></div>
  <div
    class="context-menu"
    style="left: {contextMenu.x}px; top: {contextMenu.y}px"
    onmousedown={(e) => e.stopPropagation()}
  >
    {#if menuFile?.is_text || menuFile?.is_image || menuFile?.is_audio || menuFile?.is_video}
      <button onmousedown={() => { previewFileContent(menuFile); hideContextMenu() }}>
        {t('preview')}
      </button>
    {/if}
    <button onmousedown={() => { downloadFile(menuFile); hideContextMenu() }}>
      {t('download')}
    </button>
    <button onmousedown={() => { navigator.clipboard.writeText(menuFile?.path || ''); hideContextMenu() }}>
      {t('copyPath')}
    </button>
    {#if menuFile && !menuFile.is_dir}
      <button onmousedown={() => { toggleFileSelection(menuFile); hideContextMenu() }}>
        {selectedFiles.has(menuFile?.path) ? t('deselect') : t('select')}
      </button>
    {/if}
    <button onmousedown={() => { renameFile(menuFile) }}>
      {t('rename')}
    </button>
    <button onmousedown={() => { duplicateFile(menuFile) }}>
      {t('duplicate')}
    </button>
    <button class="danger" onmousedown={() => { deleteFile(menuFile) }}>
      {t('delete')}
    </button>
  </div>
{/if}

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

  .header-btn:hover {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
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
    font-size: 0.7rem;
    color: var(--text-secondary);
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

  .preview-header button {
    background: none;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    color: var(--text);
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
    width: 100%;
    height: 100%;
    padding: 16px;
    margin: 0;
    background: var(--bg-secondary);
    color: var(--text);
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
    overflow: auto;
    word-break: break-word;
  }

  /* Code preview */
  .text-preview :global(.code-preview) {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
  }
  .text-preview :global(.code-preview code) {
    font-family: inherit;
    font-size: inherit;
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
</style>
