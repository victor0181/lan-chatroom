<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';

const props = defineProps({ user: Object, room: Object, token: String, packet: Object, outcome: Object, initialTab: String });
const emit = defineEmits(['action', 'close']);
// 弹窗用 v-if 挂载，每次打开都会重建组件，所以初始页签取一次即可。
const tab = ref(props.initialTab || 'chance');
const state = ref({ activities: [], prizes: [], wheel_prizes: [], horn: {}, prompt: {} });
const hornText = ref('');
const notice = ref('');
const waiting = ref(false);
const result = ref(null);
const rotation = ref(0);
const spinning = ref(false);
const now = ref(Date.now() / 1000);
const total = ref(20);
const count = ref(5);
const answer = ref('');
const rankKind = ref('room');
const rows = ref([]);
const loadingRanks = ref(false);
const openGames = computed(() => state.value.activities.filter(a => ['number','song','idiom'].includes(a.kind) && a.status === 'open'));
// 真心话 / 大冒险：同样是「房间里的一道题」，全房间都能回答，所以它有自己的答题板，
// 不是抽题者私有的东西。已提交的回答按 answers 的插入顺序展示（谁先答排前面）。
const openPrompt = computed(() => state.value.activities.find(a => ['truth','dare'].includes(a.kind) && a.status === 'open'));
// 没有进行中的题时，让最近两道已结束的题还留在界面上 ——
// 否则题一结束就什么都不剩，玩家会以为「玩坏了」。
const recentPrompts = computed(() => state.value.activities.filter(a => ['truth','dare'].includes(a.kind) && a.status !== 'open').slice(0, 3));
const replyText = ref('');
// 每道题只让答一次：答过就把输入框锁掉并提示，避免白等 0.6 秒节流才报错。
const repliedPrompt = id => Boolean(openPrompt.value?.replies?.some(item => item.id === props.user.id));
// 奖励值由服务端下发（snapshot.prompt），改后端常量即可，界面不会写错价。
const promptReward = computed(() => state.value.prompt?.reward ?? 1);
const promptAnswerMax = computed(() => state.value.prompt?.answer_max ?? 60);
function sendReply() {
  if (!replyText.value.trim()) { notice.value = '请先写下你的回答。'; return; }
  act('prompt_answer', { id: openPrompt.value.id, text: replyText.value });
  replyText.value = '';
}
const packets = computed(() => state.value.activities.filter(a => a.kind === 'redpack').slice(0, 8));
const prediction = computed(() => state.value.activities.find(a => a.kind === 'prediction'));
const names = { number: '猜数字', song: '猜歌名', idiom: '成语接龙' };
// 价格与冷却由服务端下发（snapshot.horn），改后端常量即可，界面不会写错价。
const hornCost = computed(() => state.value.horn?.cost ?? 10);
const hornCooldown = computed(() => state.value.horn?.cooldown ?? 60);
const hornMax = computed(() => state.value.horn?.max_length ?? 60);
function sendHorn() {
  if (!hornText.value) { notice.value = '请先输入要喊话的内容。'; return; }
  act('horn', { content: hornText.value });
}
const odds = list => list.map(prize => `${prize.label} ${prize.weight}%`).join('、');
const lotteryOdds = computed(() => odds(state.value.prizes));
const wheelOdds = computed(() => odds(state.value.wheel_prizes));
const rankings = [['room','房间榜'],['wealth','财富榜'],['charm','魅力榜'],['active','活跃榜']];
const descriptions = { room: '本房间小游戏得分，答对猜谜 +10，接龙成功 +2。', wealth: '当前金币余额。注册用户长期保存，在线游客临时上榜。', charm: '累计收礼魅力：基础礼物 +1，商城礼物按价格 / 5 计分，至少 +1。', active: '累计公开发言与私聊消息数量，活动公告不计入。' };
let timer, pendingTimer, spinTimer;

async function fetchData(path) {
  const response = await fetch(path, { headers: { 'X-Session-Token': props.token } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || '加载失败');
  return body;
}
async function load() {
  try { state.value = await fetchData(`/api/activities?room_id=${encodeURIComponent(props.room.id)}`); }
  catch (error) { notice.value = error.message; }
}
async function loadRanks() {
  loadingRanks.value = true;
  try { rows.value = (await fetchData(`/api/rankings?kind=${rankKind.value}&room_id=${encodeURIComponent(props.room.id)}`)).rows; }
  catch (error) { notice.value = error.message; }
  finally { loadingRanks.value = false; }
}
function act(action, data = {}) {
  if (waiting.value || spinning.value) return;
  waiting.value = true; notice.value = ''; result.value = null;
  emit('action', { action, ...data });
  clearTimeout(pendingTimer);
  pendingTimer = setTimeout(() => { waiting.value = false; notice.value = '未收到结果，请检查连接并刷新活动。'; }, 8000);
}
function seconds(activity) { return Math.max(0, Math.ceil(activity.expires_at - now.value)); }
function remaining(activity) { return activity.status === 'open' ? `剩余 ${seconds(activity)} 秒` : '已结束'; }
function wasClaimed(activity) { return activity.claims?.some(item => item.id === props.user.id); }
watch(() => props.packet, packet => { if (packet?.room_id === props.room.id) state.value = packet; });
watch(() => props.outcome, packet => {
  if (!packet) return;
  clearTimeout(pendingTimer); waiting.value = false;
  if (packet.type === 'interaction_error') { notice.value = packet.message; return; }
  result.value = packet;
  const details = packet.result || {};
  if (packet.action === 'wheel') {
    spinning.value = true;
    const count = state.value.wheel_prizes.length || 6;
    const target = (360 - (details.index + 0.5) * (360 / count)) % 360;
    rotation.value = (Math.floor(rotation.value / 360) + 4) * 360 + target;
    spinTimer = setTimeout(() => { spinning.value = false; notice.value = `转盘结果：${details.prize}（消耗 ${details.cost} 金币）`; }, 2200);
  } else if (packet.action === 'horn') { hornText.value = ''; notice.value = `📣 大喇叭已喊出（消耗 ${details.cost} 金币），所有房间的在线用户都会看到。`; }
  else if (details.prize) notice.value = `抽奖结果：${details.prize}（消耗 ${details.cost} 金币）`;
  else notice.value = details.hint || details.prompt || (details.amount ? `领取了 ${details.amount} 金币！` : '操作成功，房间进度已更新。');
});
watch(rankKind, loadRanks);
watch(tab, value => { if (value === 'ranks') loadRanks(); });
watch(() => props.room.id, () => { answer.value = ''; load(); if (tab.value === 'ranks') loadRanks(); });
onMounted(() => { load(); timer = setInterval(() => { now.value = Date.now() / 1000; }, 1000); });
onBeforeUnmount(() => { clearInterval(timer); clearTimeout(pendingTimer); clearTimeout(spinTimer); });
</script>

<template>
  <div class="modal" role="dialog" aria-modal="true" aria-label="房间互动中心"><div class="modal-box activity-box">
    <div class="modal-title">🎮 房间互动 · {{ room.name }} <span class="shop-coins">金币 {{ user.coins }} · 魅力 {{ user.charm || 0 }}</span></div>
    <div class="activity-tabs"><button v-for="[key,label] in [['chance','抽奖 / 转盘'],['horn','📣 大喇叭'],['social','红包 / 竞猜'],['games','房间小游戏'],['ranks','排行榜']]" :key="key" :class="{selected:tab===key}" @click="tab=key">{{ label }}</button></div>
    <p v-if="user && user.role === 'guest'" class="activity-help">游客不能使用金币功能（抽奖、转盘、大喇叭、红包、竞猜、商城），注册账号后可用。</p>
    <div class="activity-body">
      <template v-if="tab==='chance'">
        <div class="activity-columns">
          <section class="activity-panel panel-cta"><h3>🎡 幸运转盘</h3><div class="wheel-frame"><div class="wheel-pointer">▼</div><div class="prize-wheel" :style="{transform:`rotate(${rotation}deg)`}"><span v-for="(prize,index) in state.wheel_prizes" :key="prize.label" :style="{transform:`rotate(${index*60+30}deg) translateY(-70px) rotate(-${index*60+30}deg)`}">{{ prize.label }}</span><b>★</b></div></div><button class="primary-button" :disabled="waiting || spinning || user.coins<10" @click="act('wheel')">{{ spinning ? '转动中……' : '转一次 · 10 金币' }}</button></section>
          <section class="activity-panel panel-cta"><h3>🎰 随机抽奖</h3><p>点击抽取金币或道具，奖励自动到账。5 金币一次，小额稳妥。</p><ul class="prize-list"><li v-for="prize in state.prizes" :key="prize.label">{{ prize.label }}</li></ul><button class="primary-button" :disabled="waiting || spinning || user.coins<5" @click="act('lottery')">抽一次 · 5 金币</button></section>
        </div>
        <p class="activity-help">抽奖奖池（5 金币一次）：{{ lotteryOdds }}</p>
        <p class="activity-help">转盘奖池（10 金币一次）：{{ wheelOdds }}。转盘奖品金额翻倍，适合搏大奖。仅使用聊天室虚拟金币。</p>
      </template>
      <template v-if="tab==='horn'">
        <section class="activity-panel">
          <h3>📣 大喇叭</h3>
          <p>花 {{ hornCost }} 金币，向所有房间的在线用户喊话。横幅显示 10 秒，内容同时保存在当前房间的聊天记录中。</p>
          <form class="activity-form horn-form" @submit.prevent="sendHorn"><input v-model="hornText" :maxlength="hornMax" :placeholder="`想对所有房间说的话（最多 ${hornMax} 字）`"><button class="primary-button" :disabled="waiting || user.coins<hornCost">喊一次 · {{ hornCost }} 金币</button></form>
          <p class="activity-help">当前金币 {{ user.coins }}。同一人每 {{ hornCooldown }} 秒只能喊一次；内容含违规词会被退回，且不扣金币。</p>
        </section>
      </template>
      <template v-if="tab==='social'">
        <section class="activity-panel"><h3>🧧 发拼手气红包</h3><div class="activity-form"><label>总金币 <input v-model.number="total" type="number" min="1" max="500"></label><label>份数 <input v-model.number="count" type="number" min="1" max="50"></label><button class="primary-button" :disabled="waiting || user.role==='guest'" @click="act('redpack_create',{total,count})">发红包</button></div><p>每人限领一次；2 分钟后未领取金币自动退还。发出即扣总额，每份至少 1 金币。</p></section>
        <section class="activity-panel"><h3>房间红包</h3><div v-if="!packets.length" class="activity-help">暂时没有红包，发一个热闹一下吧。</div><article v-for="packet in packets" :key="packet.id" class="redpacket"><b>🧧 {{ packet.host }} · {{ packet.total }} 金币</b><span>{{ packet.left_count }}/{{ packet.count }} 份可领 · {{ remaining(packet) }}</span><button class="plain-button" :disabled="waiting || user.role==='guest' || packet.status!=='open' || seconds(packet)===0 || wasClaimed(packet)" @click="act('redpack_claim',{id:packet.id})">{{ wasClaimed(packet) ? '已领取' : '抢红包' }}</button><small>{{ packet.claims?.map(item=>`${item.name} ${item.amount}金币`).join('，') || '还没有人领取' }}</small><small v-if="packet.refund">过期退还 {{ packet.refund }} 金币</small></article></section>
          <section class="activity-panel prediction-panel"><h3>🔮 房间竞猜</h3><p>猜开奖骰子的单数或双数。每轮花费 5 金币且只能选一次；60 秒后自动开奖，猜中奖励 10 金币。</p><button class="primary-button" :disabled="waiting || user.role==='guest' || prediction?.status==='open'" @click="act('prediction_start')">发起一轮竞猜</button><div v-if="prediction" class="prediction-card"><b>{{ prediction.question }}</b><p>{{ remaining(prediction) }}</p><div class="activity-form"><button v-for="choice in prediction.options" :key="choice" class="plain-button" :disabled="waiting || user.role==='guest' || prediction.status!=='open' || seconds(prediction)===0 || user.coins<5" @click="act('prediction_vote',{id:prediction.id,choice})">{{ choice }} · {{ prediction.votes[choice] }} 人</button></div><p v-if="prediction.status!=='open'">开奖结果：{{ prediction.answer }}<br>获奖：{{ prediction.winners?.join('、') || '无人猜中' }}</p></div></section>
      </template>
      <template v-if="tab==='games'">
        <section class="activity-panel"><h3>全房间一起玩</h3><div class="activity-form"><button v-for="[key,label] in Object.entries(names)" :key="key" class="plain-button" :disabled="waiting || openGames.length>0" @click="act('game_start',{game:key})">{{ label }}</button></div><p>每轮 2 分钟；猜数字、猜歌名率先答对获得 10 金币与 10 房间金币。成语接龙每接一句 +2 金币与 +2 房间金币，轮流接，不能重复。</p></section>
        <section v-for="game in openGames" :key="game.id" class="activity-panel game-board"><h3>{{ names[game.kind] }} · {{ remaining(game) }}</h3><p>{{ game.hint }}</p><strong v-if="game.kind==='idiom'" class="idiom-word">{{ game.word }}</strong><form class="activity-form" @submit.prevent="act('game_answer',{id:game.id,answer})"><input v-model="answer" :placeholder="game.kind==='number' ? '输入 1～100' : game.kind==='song' ? '输入歌名' : '输入下一个成语'" required maxlength="40"><button class="primary-button" :disabled="waiting || seconds(game)===0">提交答案</button></form><small v-if="game.kind==='idiom'">词库示例：意气风发 → 发扬光大 → 大显身手 → 手到擒来</small></section>
        <section class="activity-panel"><h3>💬 真心话 / ⚡ 大冒险</h3><p>抽一道题，全房间都能回答，回答 {{ promptReward }} 金币（每人每题只能答一次）。也可以抽到新题后不回答，直接跳过。</p><div class="activity-form"><button class="plain-button" :disabled="waiting || openPrompt" @click="act('truth')">抽真心话</button><button class="plain-button" :disabled="waiting || openPrompt" @click="act('dare')">抽大冒险</button></div><p v-if="openPrompt" class="activity-help">本房间已有一道题在进行，先回答它或等它结束（剩余 {{ seconds(openPrompt) }} 秒）。</p></section>
        <section v-if="openPrompt" class="activity-panel prompt-board"><h3>{{ openPrompt.kind==='truth' ? '💬 真心话' : '⚡ 大冒险' }} · {{ remaining(openPrompt) }}</h3><p class="prompt-question">{{ openPrompt.host }} 抽到：「{{ openPrompt.prompt }}」</p><ul class="prompt-replies"><li v-for="item in openPrompt.replies" :key="item.id"><b>{{ item.name }}</b><span>{{ item.text }}</span></li><li v-if="!openPrompt.replies?.length" class="prompt-empty">还没有人回答，你来说第一句</li></ul><form v-if="!repliedPrompt(openPrompt.id)" class="activity-form prompt-reply-form" @submit.prevent="sendReply"><input v-model="replyText" :maxlength="promptAnswerMax" :placeholder="`写下你的回答（最多 ${promptAnswerMax} 字）`" required><button class="primary-button" :disabled="waiting || seconds(openPrompt)===0">提交回答 · +{{ promptReward }} 金币</button></form><p v-else class="activity-help">你已经回答过这道题了，等别人答完或等它结束。</p></section>
        <section v-else-if="recentPrompts.length" class="activity-panel prompt-board"><h3>最近抽到的题</h3><p v-for="item in recentPrompts" :key="item.id" class="activity-help">{{ item.kind==='truth' ? '💬' : '⚡' }} 「{{ item.prompt }}」· {{ item.reply_count||0 }} 人回答</p></section>
        <section class="activity-panel"><h3>最近战绩</h3><p v-for="game in state.activities.filter(a=>['number','song','idiom'].includes(a.kind)&&a.status!=='open').slice(0,5)" :key="game.id">{{ names[game.kind] }} · {{ game.winner ? `胜者：${game.winner}` : '本轮已结束' }} {{ game.answer ? `· 答案 ${game.answer}` : '' }}</p></section>
      </template>
      <template v-if="tab==='ranks'">
        <div class="activity-form"><button v-for="[key,label] in rankings" :key="key" class="plain-button" :class="{selected:rankKind===key}" @click="rankKind=key">{{ label }}</button><button class="plain-button" :disabled="loadingRanks" @click="loadRanks">刷新</button></div><p class="activity-help">{{ descriptions[rankKind] }}</p><table class="rank-table"><thead><tr><th>名次</th><th>用户</th><th>等级</th><th>{{ rankKind==='active' ? '消息数' : rankKind==='charm' ? '魅力' : '金币' }}</th></tr></thead><tbody><tr v-for="(row,index) in rows" :key="row.id"><td>{{ ['🥇','🥈','🥉'][index] || index+1 }}</td><td><img v-if="row.avatar?.startsWith('/avatars/')" :src="row.avatar" :alt="`${row.nickname} 的头像`"><span v-else>{{ row.avatar }}</span> {{ row.nickname }}</td><td>Lv.{{ row.level }}</td><td>{{ row.score }}</td></tr><tr v-if="!rows.length"><td colspan="4">暂无记录</td></tr></tbody></table>
      </template>
    </div>
    <div class="activity-notice" role="status">{{ waiting ? '正在处理……' : spinning ? '转盘正在转动……' : notice || '活动结果会同步到本房间聊天记录。' }}</div>
    <div class="activity-footer"><button class="plain-button" @click="load">刷新活动</button><button class="plain-button" @click="emit('close')">关闭</button></div>
  </div></div>
</template>
