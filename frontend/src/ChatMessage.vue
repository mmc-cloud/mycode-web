<script setup>
import MessageContent from "./MessageContent.vue"

defineProps({
  role: { type: String, required: true },
  content: { type: String, default: "" },
  streaming: { type: Boolean, default: false },
})
</script>

<template>
  <article class="chat-message" :class="`chat-message-${role}`" :aria-label="role === 'user' ? '用户消息' : 'MyCode 消息'" :aria-busy="streaming">
    <div class="chat-message-body">
      <div class="message-author">
        <span>{{ role === 'user' ? 'You' : 'MyCode' }}</span>
        <span v-if="streaming" class="message-streaming">正在输出…</span>
      </div>
      <p v-if="role === 'user'" class="user-message-text">{{ content }}</p>
      <MessageContent v-else :content="content" :streaming="streaming" />
    </div>
  </article>
</template>
