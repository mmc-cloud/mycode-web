<script setup>
defineProps({
  entries: { type: Array, required: true },
  selected: { type: String, default: "" },
})
defineEmits(["open", "delete"])
</script>

<template>
  <ul class="file-tree">
    <li v-for="entry in entries" :key="entry.path">
      <details v-if="entry.kind === 'directory'" open>
        <summary>
          <span>📁 {{ entry.name }}</span>
          <button class="tree-delete" title="删除目录" @click.prevent.stop="$emit('delete', entry)">×</button>
        </summary>
        <FileTree
          :entries="entry.children || []"
          :selected="selected"
          @open="$emit('open', $event)"
          @delete="$emit('delete', $event)"
        />
      </details>
      <div v-else-if="entry.kind === 'file'" class="file-row">
        <button class="file-button" :class="{ selected: selected === entry.path }" @click="$emit('open', entry.path)">📄 {{ entry.name }} <small>{{ entry.size }} B</small></button>
        <button class="tree-delete" title="删除文件" @click="$emit('delete', entry)">×</button>
      </div>
      <span v-else class="unsafe-entry">⛔ {{ entry.name }}</span>
    </li>
  </ul>
</template>
