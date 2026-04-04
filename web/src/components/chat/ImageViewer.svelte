<script>
  import { t } from '../../lib/i18n.svelte.js'
  let { images = [] } = $props()
  let modalImage = $state(null)

  function openModal(img) { modalImage = img }
  function closeModal() { modalImage = null }

  function handleOverlayClick(e) {
    if (e.target === e.currentTarget) closeModal()
  }

  function handleKeydown(e) {
    if (e.key === 'Escape' && modalImage) closeModal()
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if images && images.length > 0}
  <div class="image-viewer">
    {#each images as img}
      <button class="thumbnail-btn" onclick={() => openModal(img)} type="button">
        <img class="thumbnail" src="data:image/png;base64,{img}" alt={t('imageAlt')} />
      </button>
    {/each}
  </div>
{/if}

{#if modalImage}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" onclick={handleOverlayClick} onkeydown={(e) => e.key === 'Escape' && closeModal()} aria-label={t('imagePreview')}>
    <button class="modal-close" onclick={closeModal} type="button" aria-label={t('closeImage')}>✕</button>
    <img class="modal-image" src="data:image/png;base64,{modalImage}" alt={t('imageFullAlt')} />
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
