<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { workspace as workspaceApi } from '../../lib/api.js'
  import { remoteWorkspace, resolvePanelRemoteEnv } from '../../lib/remote-execution.svelte.js'
  import { currentSession } from '../../lib/session-state.svelte.js'
  import { resolveImageSource } from '../../lib/file-ref.js'

  let { images = [] } = $props()
  let modalImage = $state(null)
  // index -> true once the browser failed to load that source (file moved or
  // deleted, child environment unreachable). A broken <img> otherwise leaves an
  // unlabelled hole in the transcript.
  let broken = $state({})

  // msg.images is polymorphic: raw base64 (legacy upload), a data/https URI, or —
  // what <file> references actually persist — a *path*. Paths live in the
  // environment that ran the message: the parent for a local session, the bound
  // child for a remote one, reached through the parent's same-origin bridge.
  const wsApi = $derived(
    resolvePanelRemoteEnv(currentSession.sessionId) ? remoteWorkspace : workspaceApi
  )
  const sources = $derived(images.map(img => resolveImageSource(img, wsApi)))

  // A different attachment set (session switch, history reload) starts clean.
  $effect(() => {
    images.length
    broken = {}
  })

  function openModal(src) { modalImage = src }
  function closeModal() { modalImage = null }

  function handleOverlayClick(e) {
    if (e.target === e.currentTarget) closeModal()
  }

  function handleKeydown(e) {
    if (e.key === 'Escape' && modalImage) closeModal()
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if sources.length > 0}
  <div class="image-viewer">
    {#each sources as src, i}
      {#if src && !broken[i]}
        <button class="thumbnail-btn" onclick={() => openModal(src)} type="button">
          <img class="thumbnail" src={src} alt={t('imageAlt')}
               loading="lazy" onerror={() => { broken[i] = true }} />
        </button>
      {:else}
        <span class="image-missing" title={String(images[i] ?? '')}>{t('imageUnavailable')}</span>
      {/if}
    {/each}
  </div>
{/if}

{#if modalImage}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" onclick={handleOverlayClick} onkeydown={(e) => e.key === 'Escape' && closeModal()} aria-label={t('imagePreview')}>
    <button class="modal-close" onclick={closeModal} type="button" aria-label={t('closeImage')}>✕</button>
    <img class="modal-image" src={modalImage} alt={t('imageFullAlt')}
         onerror={() => { broken = {}; closeModal() }} />
  </div>
{/if}

<style>
  .image-viewer {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 6px;
  }
  .thumbnail-btn {
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
  }
  .thumbnail {
    max-width: 200px;
    max-height: 150px;
    border-radius: 4px;
    border: 1px solid var(--border);
    transition: opacity 0.15s;
  }
  .thumbnail:hover {
    opacity: 0.8;
  }
  .image-missing {
    display: inline-flex;
    align-items: center;
    padding: 6px 10px;
    border: 1px dashed var(--border);
    border-radius: 4px;
    font-size: 0.78rem;
    opacity: 0.7;
  }
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.8);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .modal-close {
    position: absolute;
    top: 16px;
    right: 16px;
    background: rgba(255, 255, 255, 0.2);
    border: none;
    color: #fff;
    font-size: 1.5rem;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s;
  }
  .modal-close:hover {
    background: rgba(255, 255, 255, 0.4);
  }
  .modal-image {
    max-width: 90vw;
    max-height: 90vh;
    object-fit: contain;
    border-radius: 4px;
  }
</style>
