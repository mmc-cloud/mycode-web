<script setup>
defineProps({
  entries: { type: Array, required: true },
  selected: { type: String, default: "" },
})
defineEmits(["open"])
</script>

<template>
  <ul class="file-tree">
    <li v-for="entry in entries" :key="entry.path">
      <details v-if="entry.kind === 'directory'" open>
        <summary>📁 {{ entry.name }}</summary>
        <FileTree
          :entries="entry.children || []"
          :selected="selected"
          @open="$emit('open', $event)"
        />
      </details>
      <button
        v-else-if="entry.kind === 'file'"
        class="file-button"
        :class="{ selected: selected === entry.path }"
        @click="$emit('open', entry.path)"
      >
        📄 {{ entry.name }} <small>{{ entry.size }} B</small>
      </button>
      <span v-else class="unsafe-entry">⛔ {{ entry.name }}</span>
    </li>
  </ul>
</template>
