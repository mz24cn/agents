// i18n.svelte.js — 零依赖多语言方案
// 新增语言：在 translations 里加一个 key，并在 LANGS 里注册即可

export const LANGS = [
  { code: 'zh', label: '中文' },
  { code: 'en', label: 'English' },
]

const translations = {
  zh: {
    // Layout
    openMenu: '打开菜单',

    // Sidebar
    nav_chat: '对话',
    nav_models: '模型管理',
    nav_tools: '工具管理',
    nav_prompts: '提示词管理',

    // ThemeToggle
    toggleTheme: '切换主题',

    // ConfirmDialog
    cancel: '取消',
    confirm: '确认',

    // Common
    loading: '加载中...',
    edit: '编辑',
    delete: '删除',
    save: '保存',
    register: '注册',
    create: '创建',
    submitting: '提交中...',
    operationFailed: '操作失败',
    jsonInvalid: 'JSON 格式无效',
    required: '*',
    close: '关闭',
    builtin: '内置',
    actions: '操作',
    name: '名称',
    description: '描述',
    type: '类型',
    id: 'ID',

    // AudioPlayer
    audioNotSupported: '浏览器不支持音频播放',

    // AudioRecorder
    audioUnavailable: '🎙️ 录音功能不可用',
    stopRecording: '停止录音',
    startRecording: '开始录音',

    // ChatInput
    inputPlaceholder: '输入消息...',
    clearChat: '清除对话',

    // ChatPage
    systemPromptLabel: '系统提示词:',
    streamError: '流式响应出错',

    // CopyButton / MarkdownRenderer
    copy: '复制',

    // FileUpload
    uploadFile: '上传文件',
    removeFile: '移除 {name}',

    // ImageViewer
    imageAlt: '图片',
    imageFullAlt: '全尺寸图片',
    imagePreview: '图片预览',
    closeImage: '关闭',

    // MessageBubble
    roleUser: '👤 用户',
    roleAssistant: '🤖 助手',
    roleSystem: '⚙️ 系统',
    roleFunction: '🔧 工具结果',
    collapseResult: '▾ 收起结果',
    expandResult: '▸ 展开结果',

    // MessageList
    startChat: '开始对话吧 💬',

    // ModelSelector
    modelLabel: '模型',
    fetchModelsFailed: '获取模型列表失败',
    selectModelPlaceholder: '请先选择模型',
    selectModelHint: '请先选择模型',

    // PlaceholderInputs
    templatePreview: '模板预览',
    inputPlaceholderValue: '输入 {name} 的值',
    applyTemplate: '应用模板',

    // PromptTemplateSelector
    promptTemplateLabel: '提示词模板',
    fetchTemplatesFailed: '获取模板列表失败',
    selectTemplatePlaceholder: '选择提示词模板',
    applyToSystem: '应用到系统',

    // ThinkingBlock
    collapseThinking: '收起',
    expandThinking: '展开',
    thinkingProcess: '思考过程',

    // ToolCallCard
    callingTool: '🔧 调用工具: {name}',
    unknownTool: '未知',

    // ToolSelector
    toolsButton: '工具 ({count})',
    fetchToolsFailed: '获取工具列表失败',
    noTools: '暂无可用工具',
    expand: '展开',
    collapse: '折叠',
    functionTools: '函数工具',
    skillTools: '技能工具',

    // ModelForm
    editModel: '编辑模型',
    registerModel: '注册模型',
    modelId: '模型 ID',
    modelName: '模型名称',
    apiBase: 'API 地址',
    apiKey: 'API 密钥',
    modelType: '模型类型',
    apiProtocol: 'API 协议',
    generateParams: '生成参数 (JSON)',
    modelIdRequired: '模型 ID 不能为空',
    apiBaseRequired: 'API 地址不能为空',
    modelNameRequired: '模型名称不能为空',
    modelIdPlaceholder: '例如: qwen-7b',
    modelNamePlaceholder: '例如: Qwen-7B-Chat',
    apiBasePlaceholder: '例如: http://localhost:11434',
    apiKeyPlaceholder: '选填',
    generateParamsPlaceholder: '{"temperature": 0.7}',

    // ModelsPage
    modelsPageTitle: '模型管理',
    noModels: '暂无已注册模型',
    fetchModelListFailed: '获取模型列表失败',
    deleteModelFailed: '删除模型失败',
    modelIdHeader: '模型 ID',
    modelNameHeader: '模型名称',
    apiBaseHeader: 'API 地址',
    typeHeader: '类型',
    protocolHeader: '协议',
    confirmDeleteModel: '确定要删除模型「{id}」吗？',

    // PromptForm
    editTemplate: '编辑模板',
    newTemplate: '新建模板',
    templateName: '模板名称',
    templateContent: '模板内容',
    detectedPlaceholders: '检测到的占位符变量：',
    templateNameRequired: '模板名称不能为空',
    templateContentRequired: '模板内容不能为空',
    templateNamePlaceholder: '例如: 代码审查助手',
    templateContentPlaceholder: '输入提示词内容，可使用 {variable_name} 作为占位符',

    // PromptsPage
    promptsPageTitle: '提示词管理',
    noTemplates: '暂无提示词模板',
    fetchTemplateListFailed: '获取提示词模板列表失败',
    deleteTemplateFailed: '删除模板失败',
    confirmDeleteTemplate: '确定要删除模板「{name}」吗？',

    // ToolDetail
    toolDetail: '工具详情',
    toolId: '工具 ID',
    toolName: '名称',
    toolType: '类型',
    toolDescription: '描述',
    functionFilePath: '函数文件路径',
    functionName: '函数名',

    // ToolForm
    editMcpServer: '编辑 MCP Server',
    editTool: '编辑工具',
    registerTool: '注册工具',
    toolTypeLabel: '工具类型',
    mcpServerConfig: 'MCP 服务器配置 (JSON)',
    skillDirLabel: '技能文件夹路径',
    mcpConfigRequired: 'MCP 配置不能为空',
    mcpServersMissing: '必须包含 "mcpServers" 对象',
    skillDirRequired: '技能文件夹路径不能为空',
    toolIdRequired: '工具 ID 不能为空',
    toolNameRequired: '工具名称不能为空',
    toolDescRequired: '工具描述不能为空',
    parametersRequired: 'Parameters 不能为空',
    functionFileRequired: '文件路径不能为空',
    functionNameRequired: '函数名不能为空',
    toolIdPlaceholder: '例如: bash',
    toolNamePlaceholder: '例如: Bash 命令执行',
    toolDescPlaceholder: '工具功能描述',
    parametersPlaceholder: '{"type": "object", "properties": {}}',
    functionFilePathPlaceholder: '例如: /home/user/tools/my_tools.py 或 ./tools/my_tools.py',
    functionFilePathHint: '支持绝对路径或相对路径（相对于服务器工作目录）',
    functionNamePlaceholder: '例如: searxng_search',
    functionNameHint: 'py 文件中的函数名，注册时将从文件动态加载',
    skillDirPlaceholder: '例如: /home/user/skills/my_skill 或 ./skills/my_skill',
    skillDirHint: '文件夹内须包含 SKILL.md，名称和描述将从其 front-matter 自动读取',
    mcpConfigHint: '支持一次配置多个 MCP server，工具将在首次调用时自动启动',

    // ToolsPage
    toolsPageTitle: '工具管理',
    noTools2: '暂无已注册工具',
    fetchToolListFailed: '获取工具列表失败',
    deleteToolFailed: '删除工具失败',
    fetchMcpConfigFailed: '获取 MCP Server 配置失败',
    toolIdHeader: '工具 ID',
    toolNameHeader: '名称',
    toolDescHeader: '描述',
    toolCount: '{n} 个工具',
    editMcpServerTitle: '编辑此 MCP Server 配置',
    deleteMcpServerTitle: '删除整个 MCP Server 及其所有工具',
    confirmDeleteMcpServer: '确定要删除 MCP Server「{label}」的全部 {count} 个工具吗？',
    confirmDeleteTool: '确定要删除工具「{id}」吗？',
  },

  en: {
    // Layout
    openMenu: 'Open menu',

    // Sidebar
    nav_chat: 'Chat',
    nav_models: 'Models',
    nav_tools: 'Tools',
    nav_prompts: 'Prompts',

    // ThemeToggle
    toggleTheme: 'Toggle theme',

    // ConfirmDialog
    cancel: 'Cancel',
    confirm: 'Confirm',

    // Common
    loading: 'Loading...',
    edit: 'Edit',
    delete: 'Delete',
    save: 'Save',
    register: 'Register',
    create: 'Create',
    submitting: 'Submitting...',
    operationFailed: 'Operation failed',
    jsonInvalid: 'Invalid JSON',
    required: '*',
    close: 'Close',
    builtin: 'Built-in',
    actions: 'Actions',
    name: 'Name',
    description: 'Description',
    type: 'Type',
    id: 'ID',

    // AudioPlayer
    audioNotSupported: 'Audio playback not supported',

    // AudioRecorder
    audioUnavailable: '🎙️ Recording unavailable',
    stopRecording: 'Stop recording',
    startRecording: 'Start recording',

    // ChatInput
    inputPlaceholder: 'Type a message...',
    clearChat: 'Clear chat',

    // ChatPage
    systemPromptLabel: 'System prompt:',
    streamError: 'Streaming error',

    // CopyButton / MarkdownRenderer
    copy: 'Copy',

    // FileUpload
    uploadFile: 'Upload file',
    removeFile: 'Remove {name}',

    // ImageViewer
    imageAlt: 'Image',
    imageFullAlt: 'Full-size image',
    imagePreview: 'Image preview',
    closeImage: 'Close',

    // MessageBubble
    roleUser: '👤 User',
    roleAssistant: '🤖 Assistant',
    roleSystem: '⚙️ System',
    roleFunction: '🔧 Tool result',
    collapseResult: '▾ Collapse',
    expandResult: '▸ Expand',

    // MessageList
    startChat: 'Start a conversation 💬',

    // ModelSelector
    modelLabel: 'Model',
    fetchModelsFailed: 'Failed to fetch models',
    selectModelPlaceholder: 'Select a model',
    selectModelHint: 'Please select a model first',

    // PlaceholderInputs
    templatePreview: 'Template preview',
    inputPlaceholderValue: 'Enter value for {name}',
    applyTemplate: 'Apply template',

    // PromptTemplateSelector
    promptTemplateLabel: 'Prompt template',
    fetchTemplatesFailed: 'Failed to fetch templates',
    selectTemplatePlaceholder: 'Select a template',
    applyToSystem: 'Apply to system',

    // ThinkingBlock
    collapseThinking: 'Collapse',
    expandThinking: 'Expand',
    thinkingProcess: 'thinking',

    // ToolCallCard
    callingTool: '🔧 Tool call: {name}',
    unknownTool: 'unknown',

    // ToolSelector
    toolsButton: 'Tools ({count})',
    fetchToolsFailed: 'Failed to fetch tools',
    noTools: 'No tools available',
    expand: 'Expand',
    collapse: 'Collapse',
    functionTools: 'Functions',
    skillTools: 'Skills',

    // ModelForm
    editModel: 'Edit Model',
    registerModel: 'Register Model',
    modelId: 'Model ID',
    modelName: 'Model Name',
    apiBase: 'API Base URL',
    apiKey: 'API Key',
    modelType: 'Model Type',
    apiProtocol: 'API Protocol',
    generateParams: 'Generate Params (JSON)',
    modelIdRequired: 'Model ID is required',
    apiBaseRequired: 'API base URL is required',
    modelNameRequired: 'Model name is required',
    modelIdPlaceholder: 'e.g. qwen-7b',
    modelNamePlaceholder: 'e.g. Qwen-7B-Chat',
    apiBasePlaceholder: 'e.g. http://localhost:11434',
    apiKeyPlaceholder: 'Optional',
    generateParamsPlaceholder: '{"temperature": 0.7}',

    // ModelsPage
    modelsPageTitle: 'Models',
    noModels: 'No models registered',
    fetchModelListFailed: 'Failed to fetch model list',
    deleteModelFailed: 'Failed to delete model',
    modelIdHeader: 'Model ID',
    modelNameHeader: 'Model Name',
    apiBaseHeader: 'API Base',
    typeHeader: 'Type',
    protocolHeader: 'Protocol',
    confirmDeleteModel: 'Delete model "{id}"?',

    // PromptForm
    editTemplate: 'Edit Template',
    newTemplate: 'New Template',
    templateName: 'Template Name',
    templateContent: 'Template Content',
    detectedPlaceholders: 'Detected placeholders:',
    templateNameRequired: 'Template name is required',
    templateContentRequired: 'Template content is required',
    templateNamePlaceholder: 'e.g. Code Review Assistant',
    templateContentPlaceholder: 'Enter prompt content. Use {variable_name} as placeholders.',

    // PromptsPage
    promptsPageTitle: 'Prompts',
    noTemplates: 'No templates yet',
    fetchTemplateListFailed: 'Failed to fetch template list',
    deleteTemplateFailed: 'Failed to delete template',
    confirmDeleteTemplate: 'Delete template "{name}"?',

    // ToolDetail
    toolDetail: 'Tool Detail',
    toolId: 'Tool ID',
    toolName: 'Name',
    toolType: 'Type',
    toolDescription: 'Description',
    functionFilePath: 'Function File Path',
    functionName: 'Function Name',

    // ToolForm
    editMcpServer: 'Edit MCP Server',
    editTool: 'Edit Tool',
    registerTool: 'Register Tool',
    toolTypeLabel: 'Tool Type',
    mcpServerConfig: 'MCP Server Config (JSON)',
    skillDirLabel: 'Skill Directory',
    mcpConfigRequired: 'MCP config is required',
    mcpServersMissing: 'Must contain "mcpServers" object',
    skillDirRequired: 'Skill directory is required',
    toolIdRequired: 'Tool ID is required',
    toolNameRequired: 'Tool name is required',
    toolDescRequired: 'Tool description is required',
    parametersRequired: 'Parameters are required',
    functionFileRequired: 'File path is required',
    functionNameRequired: 'Function name is required',
    toolIdPlaceholder: 'e.g. bash',
    toolNamePlaceholder: 'e.g. Bash Command Runner',
    toolDescPlaceholder: 'Describe what this tool does',
    parametersPlaceholder: '{"type": "object", "properties": {}}',
    functionFilePathPlaceholder: 'e.g. /home/user/tools/my_tools.py or ./tools/my_tools.py',
    functionFilePathHint: 'Supports absolute or relative paths (relative to server working directory)',
    functionNamePlaceholder: 'e.g. searxng_search',
    functionNameHint: 'Function name in the .py file, loaded dynamically on registration',
    skillDirPlaceholder: 'e.g. /home/user/skills/my_skill or ./skills/my_skill',
    skillDirHint: 'Directory must contain SKILL.md; name and description are read from its front-matter',
    mcpConfigHint: 'Multiple MCP servers can be configured at once; tools start automatically on first call',

    // ToolsPage
    toolsPageTitle: 'Tools',
    noTools2: 'No tools registered',
    fetchToolListFailed: 'Failed to fetch tool list',
    deleteToolFailed: 'Failed to delete tool',
    fetchMcpConfigFailed: 'Failed to fetch MCP server config',
    toolIdHeader: 'Tool ID',
    toolNameHeader: 'Name',
    toolDescHeader: 'Description',
    toolCount: '{n} tools',
    editMcpServerTitle: 'Edit this MCP server config',
    deleteMcpServerTitle: 'Delete entire MCP server and all its tools',
    confirmDeleteMcpServer: 'Delete MCP Server "{label}" and all {count} tools?',
    confirmDeleteTool: 'Delete tool "{id}"?',
  },
}

// 从 localStorage 读取初始语言，默认中文
const saved = typeof localStorage !== 'undefined' ? localStorage.getItem('lang') : null
export const i18n = $state({ lang: saved || 'zh' })

/**
 * 翻译函数，支持简单插值：t('removeFile', { name: 'foo.png' })
 * @param {string} key
 * @param {Record<string, string|number>} [vars]
 */
export function t(key, vars) {
  const dict = translations[i18n.lang] ?? translations.zh
  let str = dict[key] ?? translations.zh[key] ?? key
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      str = str.replaceAll(`{${k}}`, v)
    }
  }
  return str
}

export function setLang(code) {
  i18n.lang = code
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem('lang', code)
  }
}
