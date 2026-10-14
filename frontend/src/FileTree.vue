<script setup>
defineProps({
  entries: { type: Array, required: true },
  selected: { type: String, default: "" },
  expanded: { type: Set, required: true },
})
defineEmits(["open", "delete", "toggle"])
</script>

<template>
  <ul class="file-tree">
    <li v-for="entry in entries" :key="entry.path">
      <details v-if="entry.kind === 'directory'" :open="expanded.has(entry.path)" @toggle="$emit('toggle', entry.path, $event.target.open)">
        <summary>
          <svg class="tree-icon folder-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V5h6l2 2h10v13H3z" /></svg>
          <span class="entry-name">{{ entry.name }}</span>
          <button class="tree-delete" title="删除目录" :aria-label="`删除目录 ${entry.name}`" @click.prevent.stop="$emit('delete', entry)">×</button>
        </summary>
        <FileTree
          :entries="entry.children || []"
          :selected="selected"
          :expanded="expanded"
          @toggle="(path, open) => $emit('toggle', path, open)"
          @open="$emit('open', $event)"
          @delete="$emit('delete', $event)"
        />
      </details>
      <div v-else-if="entry.kind === 'file'" class="file-row">
        <button class="file-button" :title="entry.path" :class="{ selected: selected === entry.path }" :aria-pressed="selected === entry.path" @click="$emit('open', entry.path)"><svg class="tree-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h9l5 5v13H5z M14 3v6h5 M8 13h8 M8 17h6" /></svg><span class="entry-name">{{ entry.name }}</span><small>{{ entry.size }} B</small></button>
        <button class="tree-delete" title="删除文件" :aria-label="`删除文件 ${entry.name}`" @click="$emit('delete', entry)">×</button>
      </div>
      <span v-else-if="entry.kind === 'symlink'" class="link-entry" title="符号链接：出于安全考虑，不支持预览目标">
        <svg class="tree-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" /></svg>
        <span class="entry-name">{{ entry.name }}</span><small>符号链接</small>
      </span>
      <span v-else class="unsafe-entry">⛔ {{ entry.name }}</span>
    </li>
  </ul>
</template>
