<script>
  /**
   * IconDisplay 组件 - 通用图标显示组件
   * 支持多种格式：emoji、data URI、URL、路径、文件名
   * 
   * @param {string} [value] - 图标值
   * @param {string} [alt=''] - 替代文本
   * @param {string} [class=''] - CSS 类名
   * @param {number} [size] - 尺寸（像素）
   * @param {boolean} [lazy=true] - 是否懒加载图片
   * @param {string} [fallback=''] - 空值时的 fallback
   */
  let { 
    value = '',
    alt = '',
    class: className = '',
    size = undefined,
    lazy = true,
    fallback = ''
  } = $props()
  
  // 图片加载状态
  let loaded = $state(false)
  let error = $state(false)
  
  // 重置状态当值变化
  $effect(() => {
    value // 依赖追踪
    loaded = false
    error = false
  })
  
  // 处理后的配置
  let processed = $derived(processValue(value))
  
  // 生成样式：emoji 不设宽高让其自然继承字号水平展开，图片用固定宽高
  let style = $derived.by(() => {
    if (!size) return ''
    if (processed.isEmoji) return `width: auto; height: auto;`
    return `width: ${size}px; height: ${size}px;`
  })
  
  // 判断是否为 URL/图片格式
  function isImageUrl(val) {
    if (!val || val.trim() === '') return false
    
    // data URI
    if (val.startsWith('data:')) return true
    
    // http/https URL
    if (val.startsWith('http') || val.startsWith('//')) return true
    
    // 绝对路径
    if (val.startsWith('/')) return true
    
    // 相对路径
    if (val.startsWith('./') || val.startsWith('../')) return true
    
    // 文件名（带图片扩展名）
    if (/\.(png|jpg|jpeg|gif|svg|ico|webp|bmp)(\?.*)?$/i.test(val)) return true
    
    return false
  }
  
  // 判断是否为 emoji（纯 emoji 字符组成的短文本，含变体选择器和零宽连接符）
  function isEmoji(val) {
    if (!val || val.trim() === '') return false
    if (isImageUrl(val)) return false
    
    // 全部由 emoji 相关字符组成（Emoji、修饰符、变体选择器、零宽连接符等）
    // 长度放宽到 32 以支持多字符 emoji 序列（如 👨‍👩‍👧‍👦 等 ZWJ 序列可达 11 个码点）
    if (val.length <= 32 && /^[\p{Emoji}\p{Emoji_Modifier}\p{Emoji_Component}\p{Emoji_Presentation}\u200D\uFE0F\u20E3]+$/u.test(val)) return true
    
    return false
  }
  
  // 处理值，返回显示配置
  function processValue(val) {
    // 空值
    if (!val || val.trim() === '') {
      return {
        type: 'empty',
        display: fallback,
        isImage: false,
        isEmoji: false,
        isDisplay: !!fallback
      }
    }
    
    // URL/图片格式
    if (isImageUrl(val)) {
      return {
        type: 'image',
        display: val,
        isImage: true,
        isEmoji: false,
        isDisplay: true
      }
    }
    
    // Emoji 格式
    if (isEmoji(val)) {
      return {
        type: 'emoji',
        display: val,
        isImage: false,
        isEmoji: true,
        isDisplay: true
      }
    }
    
    // 其他文本（当作文本显示）
    return {
      type: 'text',
      display: val,
      isImage: false,
      isEmoji: false,
      isDisplay: true
    }
  }
  
  function handleLoad() {
    loaded = true
    error = false
  }
  
  function handleError() {
    error = true
  }
</script>

{#if processed.isDisplay}
  {#if processed.isImage}
    <img 
      src={processed.display} 
      {alt} 
      class="{className} {loaded ? 'loaded' : 'loading'} {error ? 'error' : ''}"
      {style}
      loading={lazy ? 'lazy' : 'eager'}
      on:load={handleLoad}
      on:error={handleError}
    />
  {:else if processed.isEmoji}
    <span 
      class="icon-emoji {className}"
      {style}
      role="img"
      aria-label={alt}
    >{processed.display}</span>
  {:else}
    <span 
      class="icon-text {className}"
      {style}
    >{processed.display}</span>
  {/if}
{/if}

<style>
  .icon-emoji {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    white-space: nowrap;
    overflow: visible;
  }
  
  .icon-text {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  
  .loading {
    opacity: 0.5;
    transition: opacity 0.3s;
  }
  
  .loaded {
    opacity: 1;
    transition: opacity 0.3s;
  }
  
  .error {
    opacity: 0.7;
    filter: grayscale(50%);
  }
</style>
