<script>
  import { t } from '../../lib/i18n.svelte.js'

  let { onAttach = () => {} } = $props()

  let attachedFiles = $state([])
  let isProcessing = $state(false)
  let fileInput

  function openFileDialog() { fileInput?.click() }
  function isImageFile(file) { return file.type.startsWith('image/') }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => { const base64 = reader.result.split(',')[1] || ''; resolve(base64) }
      reader.onerror = () => reject(reader.error)
      reader.readAsDataURL(file)
    })
  }

  function readFileAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result)
      reader.onerror = () => reject(reader.error)
      reader.readAsText(file)
    })
  }

  async function handleFileSelect(e) {
    const files = Array.from(e.target.files || [])
    if (files.length === 0) return
    isProcessing = true
    try {
      const newEntries = []
      for (const file of files) {
        if (isImageFile(file)) {
          const base64 = await readFileAsBase64(file)
          newEntries.push({ id: crypto.randomUUID(), name: file.name, type: 'image', data: base64, preview: `data:${file.type};base64,${base64}` })
        } else {
          const content = await readFileAsText(file)
          newEntries.push({ id: crypto.randomUUID(), name: file.name, type: 'file', data: content, preview: null })
        }
      }
      attachedFiles = [...attachedFiles, ...newEntries]
      dispatchAttach()
    } finally {
      isProcessing = false
      if (fileInput) fileInput.value = ''
    }
  }

  function removeFile(id) {
    attachedFiles = attachedFiles.filter(f => f.id !== id)
    dispatchAttach()
  }

  function dispatchAttach() {
    const images = attachedFiles.filter(f => f.type === 'image').map(f => f.data)
    const files = attachedFiles.filter(f => f.type === 'file').map(f => ({ name: f.name, content: f.data }))
    onAttach({ images, files })
  }
</script>

<div class="file-upload">
  <input type="file" multiple bind:this={fileInput} onchange={handleFileSelect} class="hidden-input" aria-hidden="true" />
  <button class="upload-btn" onclick={openFileDialog} disabled={isProcessing} type="button" title={t('uploadFile')}>
    {#if isProcessing}
      <span class="spinner"></span>
    {:else}
      📎
    {/if}
  </button>

  {#if attachedFiles.length > 0}
    <div class="file-list">
      {#each attachedFiles as file (file.id)}
        <div class="file-item">
          {#if file.type === 'image' && file.preview}
            <img class="file-thumb" src={file.preview} alt={file.name} />
          {:else}
            <span class="file-icon">📄</span>
          {/if}
          <span class="file-name" title={file.name}>{file.name}</span>
          <button class="remove-btn" onclick={() => removeFile(file.id)} type="button" aria-label={t('removeFile', { name: file.name })}>✕</button>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .file-upload {
    display: flex;
    align-items: flex-end;
    gap: 6px;
    flex-wrap: wrap;
  }
  .hidden-input { display: none; }
  .upload-btn {
    width: 36px;
    height: 36px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 1.1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .upload-btn:hover:not(:disabled) { background: var(--border); }
  .upload-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .file-list { display: flex; flex-wrap: wrap; gap: 6px; width: 100%; margin-top: 4px; }
  .file-item {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 3px 6px;
    border-radius: 6px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    font-size: 0.8rem;
    max-width: 180px;
  }
  .file-thumb { width: 24px; height: 24px; object-fit: cover; border-radius: 3px; flex-shrink: 0; }
  .file-icon { font-size: 0.9rem; flex-shrink: 0; }
  .file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .remove-btn {
    background: none;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.75rem;
    padding: 0 2px;
    flex-shrink: 0;
    line-height: 1;
  }
  .remove-btn:hover { color: var(--danger); }
</style>
