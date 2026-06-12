<script>
  /**
   * AppLogo 组件 - 应用 Logo，支持从 store 读取配置
   * @param {string} [src] - 图标源，如果提供则覆盖 store 值
   * @param {string} [alt='应用Logo'] - 替代文本
   * @param {string} [class=''] - CSS 类名
   * @param {number} [size] - 尺寸（像素）
   * @param {boolean} [lazy=true] - 是否懒加载图片
   */
  import { getContext, onDestroy } from 'svelte'
  import IconDisplay from './IconDisplay.svelte'
  
  let { 
    src = undefined, 
    alt = '应用Logo', 
    class: className = '', 
    size = undefined,
    lazy = true 
  } = $props()
  
  // 从 context 获取 store
  const appLogoStore = getContext('appLogoStore')
  
  // 订阅 store
  let storeValue = ''
  let unsubscribe
  
  if (appLogoStore) {
    unsubscribe = appLogoStore.subscribe(value => {
      storeValue = value
    })
  }
  
  onDestroy(() => {
    if (unsubscribe) unsubscribe()
  })
  
  // 获取配置值
  let configValue = $derived(src ?? storeValue ?? '')
  
  // 判断是否为图片 URL（用于 favicon）
  function isImageUrl(val) {
    if (!val || val.trim() === '') return false
    if (val.startsWith('data:')) return true
    if (val.startsWith('http') || val.startsWith('//')) return true
    if (val.startsWith('/')) return true
    if (val.startsWith('./') || val.startsWith('../')) return true
    if (/\.(png|jpg|jpeg|gif|svg|ico|webp|bmp)(\?.*)?$/i.test(val)) return true
    return false
  }
  
  // 生成 favicon URL
  let faviconUrl = $derived((() => {
    if (!configValue || configValue.trim() === '') return '/favicon.svg'
    
    // 如果是图片 URL，直接使用
    if (isImageUrl(configValue)) return configValue
    
    // 如果是 emoji，生成 SVG data URI
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <text y=".9em" font-size="90">${configValue}</text>
    </svg>`
    return `data:image/svg+xml,${encodeURIComponent(svg)}`
  })())
  
  // 更新 favicon
  $effect(() => {
    if (typeof document !== 'undefined' && faviconUrl) {
      let link = document.querySelector("link[rel*='icon']")
      if (!link) {
        link = document.createElement('link')
        link.rel = 'shortcut icon'
        document.head.appendChild(link)
      }
      link.href = faviconUrl
    }
  })
</script>

<IconDisplay 
  value={configValue}
  {alt}
  class={className}
  {size}
  {lazy}
  fallback="💎"
/>
