<script setup>
import { computed, ref, watch } from 'vue';

const props = defineProps({ user: Object, items: Array, busy: Boolean });
const emit = defineEmits(['recycle']);
const filter = ref('all');
const sort = ref('type');
const selected = ref({});
const confirming = ref(false);
const owned = computed(() => (props.items || [])
  .filter(item => ['badge', 'gift'].includes(item.item_type) && Number(props.user?.inventory?.[item.id]) > 0)
  .map(item => ({ ...item, count: Number(props.user.inventory[item.id]), price: Number(item.recycle_price || 0),
    equipped: item.item_type === 'badge' && (props.user?.badge_id ? props.user.badge_id === item.id : props.user?.badge === item.icon) })));
const visible = computed(() => owned.value.filter(item => filter.value === 'all' || item.item_type === filter.value)
  .sort((a, b) => sort.value === 'count' ? b.count - a.count : sort.value === 'value' ? b.price - a.price
    : a.item_type.localeCompare(b.item_type) || a.name.localeCompare(b.name, 'zh')));
const chosen = computed(() => owned.value.filter(item => selected.value[item.id] !== undefined)
  .map(item => ({ ...item, quantity: Number(selected.value[item.id]) })));
const quantityValid = computed(() => chosen.value.length > 0 && chosen.value.every(item =>
  Number.isInteger(item.quantity) && item.quantity >= 1 && item.quantity <= Math.min(item.count, 1000000)));
const total = computed(() => chosen.value.reduce((sum, item) => sum + item.quantity * item.price, 0));
const totalCount = computed(() => chosen.value.reduce((sum, item) => sum + item.quantity, 0));
const allSelected = computed(() => visible.value.length > 0 && visible.value.every(item => selected.value[item.id] !== undefined));
const losesEquipped = computed(() => chosen.value.some(item => item.equipped && item.quantity === item.count));
watch(() => props.user?.inventory, () => {
  const updated = {};
  for (const item of chosen.value) updated[item.id] = Math.min(item.quantity, item.count);
  selected.value = updated;
});
watch(selected, () => { confirming.value = false; }, { deep: true });
watch(() => props.busy, (busy, wasBusy) => {
  if (wasBusy && !busy) { confirming.value = false; selected.value = {}; }
});
function toggle(item, checked) {
  if (checked) selected.value[item.id] = 1;
  else delete selected.value[item.id];
}
function toggleAll(checked) {
  for (const item of visible.value) {
    if (!checked || selected.value[item.id] === undefined) toggle(item, checked);
  }
}
function selectDuplicates() {
  const next = { ...selected.value };
  for (const item of visible.value.filter(item => item.count > 1)) {
    next[item.id] = Math.min(next[item.id] || item.count - 1, item.count - 1, 1000000);
  }
  selected.value = next;
}
function submit() {
  if (!quantityValid.value || props.busy) return;
  emit('recycle', chosen.value.map(item => ({ item_id: item.id, quantity: item.quantity })));
}
</script>

<template>
  <section class="inventory-panel" aria-label="我的物品">
    <div class="inventory-tools">
      <div class="inventory-filters" role="group" aria-label="物品类型">
        <button v-for="option in [{ value: 'all', label: '全部' }, { value: 'badge', label: '徽章' }, { value: 'gift', label: '礼物' }]" :key="option.value" :aria-pressed="filter === option.value" :disabled="busy" @click="filter = option.value">{{ option.label }}</button>
      </div>
      <select v-model="sort" aria-label="库存排序" :disabled="busy"><option value="type">按类型</option><option value="count">按数量</option><option value="value">按回收价</option></select>
      <button class="plain-button" :disabled="busy || !visible.some(item => item.count > 1)" @click="selectDuplicates">选择重复物品</button>
    </div>
    <div class="inventory-table" role="table" aria-label="徽章和礼物库存">
      <div class="inventory-row inventory-head" role="row">
        <input type="checkbox" aria-label="全选当前列表" :checked="allSelected" :disabled="busy || !visible.length" @change="toggleAll($event.target.checked)">
        <span>物品</span><span>库存</span><span>回收价</span><span>数量</span>
      </div>
      <div v-for="item in visible" :key="item.id" class="inventory-row" role="row">
        <input type="checkbox" :aria-label="`选择${item.name}`" :checked="selected[item.id] !== undefined" :disabled="busy" @change="toggle(item, $event.target.checked)">
        <div class="inventory-name"><span class="inventory-icon">{{ item.icon }}</span><div><b>{{ item.name }}</b><small>{{ item.item_type === 'badge' ? '徽章' : '礼物' }}<em v-if="item.equipped"> · 佩戴中</em></small></div></div>
        <span class="inventory-count">{{ item.count }}</span><span class="inventory-price">{{ item.price }} 金币</span>
        <input v-if="selected[item.id] !== undefined" v-model.number="selected[item.id]" type="number" min="1" :max="Math.min(item.count, 1000000)" step="1" :aria-label="`${item.name}回收数量`" :disabled="busy">
        <span v-else class="inventory-unselected">—</span>
      </div>
      <div v-if="!visible.length" class="inventory-empty">{{ filter === 'badge' ? '暂无徽章' : filter === 'gift' ? '暂无礼物' : '暂无可整理的物品' }}</div>
    </div>
    <div class="inventory-checkout" aria-live="polite">
      <span v-if="!chosen.length">未选择物品</span>
      <span v-else-if="!quantityValid" class="inventory-warning">请输入有效数量，不可超过库存</span>
      <template v-else>
        <strong>{{ confirming ? '确认回收' : '已选择' }} {{ totalCount }} 件 · {{ total }} 金币</strong>
        <small v-if="chosen.some(item => item.price === 0)">含 {{ chosen.filter(item => item.price === 0).reduce((sum, item) => sum + item.quantity, 0) }} 件免费礼物，仅清理库存</small>
        <small v-if="losesEquipped" class="inventory-warning">将回收正在佩戴的最后一枚徽章</small>
      </template>
      <div class="inventory-actions">
        <button v-if="confirming" class="plain-button" :disabled="busy" @click="confirming = false">取消</button>
        <button class="primary-button" :disabled="busy || !quantityValid || user?.role === 'guest'" @click="confirming ? submit() : confirming = true">{{ busy ? '处理中…' : confirming ? '确认回收' : '回收所选' }}</button>
      </div>
    </div>
  </section>
</template>
