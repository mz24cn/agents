<script>
  // 模拟 ChatPage 在「同一条会话」上流式追加消息：MessageList 实例不换，只有 messages
  // 换引用。折叠快照住在 MessageList 实例里，所以只有这种挂载方式才能复现推理中的多帧更新。
  import MessageList from '../components/chat/MessageList.svelte'

  let { streamRef, initial = [] } = $props()
  let messages = $state(initial)
  streamRef.set = (next) => { messages = next }
</script>

<MessageList {messages} />
