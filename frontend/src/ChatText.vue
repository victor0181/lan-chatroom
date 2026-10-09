<script setup>
import { computed } from 'vue';

const props = defineProps({
  content: { type: String, default: '' },
  effect: { type: String, default: '' },
  customEmojis: { type: Object, default: () => ({}) },
});
const colors = ['#bd3434', '#a65d08', '#327b29', '#206baf', '#8545a4'];
const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter('zh', { granularity: 'grapheme' }) : null;
const characters = computed(() => segmenter ? Array.from(segmenter.segment(props.content), item => item.segment) : Array.from(props.content));
const parts = computed(() => {
  const result = [];
  const pattern = /:([A-Za-z0-9_-]{1,40}):/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(props.content))) {
    const src = props.customEmojis[match[1]];
    if (!src) continue;
    if (match.index > cursor) result.push({ type: 'text', value: props.content.slice(cursor, match.index) });
    result.push({ type: 'image', value: src, name: match[1] });
    cursor = match.index + match[0].length;
  }
  if (cursor < props.content.length || !result.length) result.push({ type: 'text', value: props.content.slice(cursor) });
  return result;
});
function retryImage(event) {
  const image = event.currentTarget;
  if (image.dataset.retry === '1') {
    image.style.display = 'none';
    return;
  }
  image.dataset.retry = '1';
  image.src = `${image.src}${image.src.includes('?') ? '&' : '?'}retry=1`;
}
</script>

<template>
  <span v-if="effect === 'rainbow'" class="rainbow-text"><template v-for="(part, partIndex) in parts" :key="partIndex"><span v-if="part.type === 'image'" class="custom-emoji-wrap"><img class="custom-emoji" :src="part.value" :alt="`自定义表情 ${part.name}`" @error="retryImage"></span><template v-else><span v-for="(character, index) in (segmenter ? Array.from(segmenter.segment(part.value), item => item.segment) : Array.from(part.value))" :key="index" :style="{ color: colors[index % colors.length] }">{{ character }}</span></template></template></span>
  <template v-else><template v-for="(part, partIndex) in parts" :key="partIndex"><img v-if="part.type === 'image'" class="custom-emoji" :src="part.value" :alt="`自定义表情 ${part.name}`" @error="retryImage"><span v-else>{{ part.value }}</span></template></template>
</template>
