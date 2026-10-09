<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import RoomActivities from './RoomActivities.vue';
import ChatText from './ChatText.vue';
import InventoryPanel from './InventoryPanel.vue';

const token = ref(localStorage.getItem('lan_chat_token') || '');
const user = ref(null);
const socket = ref(null);
const connected = ref(false);
const screen = ref('entry');
const tab = ref('login');
const entryError = ref('');
const messages = ref([]);
const messagesEl = ref(null);
const autoScrollEnabled = ref(true);
const pendingMessageCount = ref(0);
const messageScrollLabel = computed(() => pendingMessageCount.value
  ? `有 ${pendingMessageCount.value > 99 ? '99+' : pendingMessageCount.value} 条新消息 · 回到底部`
  : '回到底部');
// 聊天区里哪些「活动折叠带」被点开了（按组的 id 记）。切房间或重拉历史时清空。
const expandedStreams = ref([]);
const users = ref([]);
// 全站在线的人的 id（跨房间）。私聊是**跨房间**的，只看本房间名单会把换了房间的对方误判成「已离线」。
const onlineIds = ref([]);
const rooms = ref([]);
const currentRoomId = ref('lobby');
const privateWith = ref(null);
// 私聊对象按 id 实时从在线列表里取，而不是一直用点开那一刻的快照 ——
// 否则对方中途改昵称、升级、被踢，私聊条上显示的还是旧信息。
// 掉线时列表里就找不到了，回退到快照，至少把名字留住。
const privatePeer = computed(() => {
  const target = privateWith.value;
  if (!target) return null;
  return users.value.find(item => item.id === target.id) || target;
});
// 私聊时对方不在线 = 已离线。只提示、不自动踢回房间：翻旧记录还是能看的。
// ★ 判据要用**全站在线名单**（online_ids），不能用本房间的 users ——
//   私聊是跨房间的（后端按 user_id 找人），对方换个房间就会被误判成「已离线、发不出消息」。
//   没拿到 online_ids 时退回旧口径，至少不比原来差。
const privatePeerOnline = computed(() => {
  if (!privateWith.value) return false;
  const ids = onlineIds.value;
  return ids.length ? ids.includes(privateWith.value.id) : users.value.some(item => item.id === privateWith.value.id);
});
const input = ref('');
const search = ref('');
const showAnnouncement = ref(false);
// 「编辑本房间公告」：房主和管理员才能用。草稿和提示都放这里，关掉弹窗就丢弃。
const editingAnnouncement = ref(false);
const announcementDraft = ref('');
const announcementNotice = ref('');
const showProfile = ref(false);
const showShop = ref(false);
const showActivities = ref(false);
const activityTab = ref('chance');
const activityPacket = ref(null);
const activityOutcome = ref(null);
const EFFECT_DURATION_MS = 30 * 1000;
// 文字格式采用单选模式，避免颜色、彩虹和闪亮叠加后难以阅读。
const textFormat = ref('');
const textFormatExpiresAt = ref(0);
const textStyle = computed(() => ['bold', 'blue', 'purple', 'red'].includes(textFormat.value) ? textFormat.value : '');
const textEffect = computed(() => ['rainbow', 'glow'].includes(textFormat.value) ? textFormat.value : '');
const shopItems = ref([]);
const shopNotice = ref('');
const shopView = ref('buy');
const recycleBusy = ref(false);
const petNow = ref(Date.now());
const showPetPicker = ref(false);
const petPickerNotice = ref('');
const petFeedback = ref('');
const petBusy = ref(false);
const petActionAnimation = ref('');
const petReaction = ref('');
const petBubbleText = ref('');
let petActionTimer = null;
let petClickTimer = null;
let petBubbleTimer = null;
let petSpeechTimer = null;
const petChoice = ref('');
const PET_CHOICES = [
  { type: 'chicken', name: '小鸡', icon: '🐥', description: '活泼又爱撒娇的小伙伴' },
  { type: 'cat', name: '小猫', icon: '🐱', description: '安静陪着你聊天的小猫' },
  { type: 'dog', name: '小狗', icon: '🐶', description: '热情迎接你的忠实伙伴' },
];
const PET_CHANGE_COST = 20;
const PET_DECAY_PER_MINUTE = { hunger: 1.2, thirst: 1.5, cleanliness: 0.8 };
const PET_ACTIONS = [
  { action: 'feed', label: '喂食', icon: '🍖', itemId: 'pet_food', stat: 'hunger', animation: 'pet-feed' },
  { action: 'water', label: '喂水', icon: '🫗', itemId: 'pet_water', stat: 'thirst', animation: 'pet-water' },
  { action: 'clean', label: '清洁', icon: '🧼', itemId: 'pet_clean', stat: 'cleanliness', animation: 'pet-clean' },
];
const avatarOptions = ref([]);
const customEmojis = ref([]);
const customEmojiMap = computed(() => Object.fromEntries(customEmojis.value.map(item => [item.name, item.src])));
const DEFAULT_EMOJIS = ['😀', '😄', '😂', '😎', '👍', '❤️', '🤣', '🥳', '🤔', '👏', '🎉', '🔥'];
const emojiChoices = computed(() => customEmojis.value.length ? [] : DEFAULT_EMOJIS);
const profileAvatar = ref('🙂');
// 班级不是自由文本，而是「年级 + 班级号」两个下拉，各自最后一项是「取消」（也就是不填）。
// 取消放在末尾：打开下拉先看到的是一堆可选项，而不是「取消」。
// 选项要和 app.py 里的 PROFILE_GRADE_OPTIONS / PROFILE_CLASS_OPTIONS 保持一致。
const GRADE_OPTIONS = ['七年级', '八年级', '九年级'];
const CLASS_OPTIONS = ['1班', '2班', '3班', '4班', '5班', '6班', '7班', '8班', '9班', '10班'];
const profileForm = ref({ nickname: '', real_name: '', grade: '', classNo: '', bio: '', oldPassword: '', newPassword: '', confirmPassword: '' });
// 学生有没有动过班级下拉框。没动过就沿用原值 ——
// 这样「库里存的是旧格式自由文本、两个下拉都解析不出来」时，不会因为看着是空的就把班级清掉。
const classTouched = ref(false);
const profileNotice = ref('');
// 对方的资料卡（「查看资料」1 金币）：真实姓名/班级/签名只走这条收费通道下发，
// 不会混在在线列表（那是免费广播给全房间的）。
const profileCard = ref(null);
const profileCardNotice = ref('');
const avatarPreview = ref(null);
const avatarPreviewDialog = ref(null);
const avatarPreviewLoaded = ref(false);
const avatarPreviewError = ref(false);
const giftTarget = ref(null);
const showGifts = ref(false);
const showEmoji = ref(false);
const giftMenuMode = ref('send');
const coinGiftAmount = ref(10);
const context = ref(null);
const soundOn = ref(true);
// 公告按房间分开，正文在项目根目录的「公告/<房间号>.txt」，由后端按房间下发，改完存盘即生效。
// 房主也能在弹窗里直接改自己房间的公告。这里只是一份兜底：接口暂时拿不到时不让侧栏出现空白块。
const announcement = ref('欢迎来到局域网互动聊天室！请大家文明聊天，友好交流。');
const lastClock = ref('');
const joinBanner = ref(null);
let joinBannerSeq = 0;
let joinBannerTimer = null;
const BANNER_DURATION_MS = 10000;
const itemAnimation = ref(null);
let itemAnimationTimer = null;

const loginForm = ref({ username: '', password: '' });
const registerForm = ref({ username: '', password: '', nickname: '' });
const guestName = ref('');

const onlineUsers = computed(() => users.value.filter(u => u.nickname.toLowerCase().includes(search.value.trim().toLowerCase())));
const sortedUsers = computed(() => [...onlineUsers.value].sort((a, b) => a.role === 'admin' ? -1 : b.role === 'admin' ? 1 : a.nickname.localeCompare(b.nickname, 'zh')));
const characterCount = computed(() => input.value.length);
const isGuest = computed(() => user.value?.role === 'guest');
const isAdmin = computed(() => user.value?.role === 'admin');
const isUnlimitedCoins = computed(() => Boolean(user.value?.coins_unlimited || isAdmin.value));
// 我是不是「当前这个房间」的房主 —— 服务端按房间算好了下发（见 Session.public() 的 owner）。
// 所以换房间后这个值会跟着变：在 A 房间是房主，切到 B 房间就不是了。
const isOwner = computed(() => user.value?.owner === true);
// 能不能对在线列表里的人点管理菜单：管理员管所有房间，房主只在本房间管人。
const canModerate = computed(() => isAdmin.value || isOwner.value);
// 把库里存的班级拆回两个下拉：「八年级3班」→ 八年级 / 3班。
// 老写法「八（3）班」「8年级3班」也认；实在认不出就返回空，由 classTouched 兜住、不会被清掉。
function splitClassName(raw) {
  const text = String(raw || '').replace(/[\s\u3000]+/g, '');
  let grade = '';
  const gradeMatched = text.match(/([0-9一二三四五六七八九]{1,3})年级/);
  if (gradeMatched) {
    grade = { '七': '七年级', '7': '七年级', '八': '八年级', '8': '八年级', '九': '九年级', '9': '九年级' }[gradeMatched[1]] || '';
  } else {
    // 「八（3）班」这种：年级后面直接跟括号，没有「年级」两个字。
    grade = GRADE_OPTIONS.find(item => text.startsWith(item[0] + '（') || text.startsWith(item[0] + '(')) || '';
  }
  const numberMatched = text.match(/(?<!\d)(\d{1,2})班/) || text.match(/[（(](\d{1,2})[）)]/);
  const classNo = numberMatched ? (CLASS_OPTIONS.find(item => item === `${numberMatched[1]}班`) || '') : '';
  return { grade, classNo };
}
const formClassName = computed(() => (profileForm.value.grade || '') + (profileForm.value.classNo || ''));
// 保存时会真正写进去的班级：动过下拉就用下拉拼出来的，没动过就沿用原值。
const effectiveClassName = computed(() => classTouched.value ? formClassName.value : String(user.value?.class_name || ''));
// 设置面板里真实发生的改动。和服务端同一套口径：填了但和现在一模一样的字段不算改动、不收费。
const profileChanges = computed(() => {
  const me = user.value || {};
  const form = profileForm.value;
  const list = [];
  if (profileAvatar.value !== (me.avatar || '🙂')) list.push('头像');
  const nickname = form.nickname.trim();
  if (nickname && nickname !== me.nickname) list.push('昵称');
  if (form.real_name.trim() !== (me.real_name || '')) list.push('真实姓名');
  if (effectiveClassName.value !== (me.class_name || '')) list.push('班级');
  if (form.bio.trim() !== (me.bio || '')) list.push('个性签名');
  if (form.newPassword) list.push('密码');
  return list;
});
const profileCost = computed(() => isAdmin.value ? 0 : profileChanges.value.length * 5);
const profileCostLabel = computed(() => {
  if (!profileChanges.value.length) return '还没有改动';
  const count = '本次修改 ' + profileChanges.value.length + ' 处';
  // 管理员不显示任何费用字样，只报「改了几处」。
  return profileCost.value ? count + ' · 需 ' + profileCost.value + ' 金币' : count;
});
const currentRoom = computed(() => rooms.value.find(room => room.id === currentRoomId.value) || { id: 'lobby', name: '综合大厅', icon: '🌟', description: '大家随意聊聊吧' });
const petMinLevel = computed(() => Number(user.value?.pet_min_level || 5));
const petUnlocked = computed(() => !isGuest.value && Number(user.value?.level || 1) >= petMinLevel.value);
const petState = computed(() => {
  void petNow.value;
  if (!petUnlocked.value) return null;
  const source = user.value?.pet;
  if (!source?.type) return null;
  const updatedAtMs = Number(source.updated_at || 0) * 1000;
  const elapsed = updatedAtMs ? Math.max(0, petNow.value - updatedAtMs) / 60000 : 0;
  return {
    ...source,
    hunger: Math.max(0, Math.min(100, Number(source.hunger ?? 100) - elapsed * PET_DECAY_PER_MINUTE.hunger)),
    thirst: Math.max(0, Math.min(100, Number(source.thirst ?? 100) - elapsed * PET_DECAY_PER_MINUTE.thirst)),
    cleanliness: Math.max(0, Math.min(100, Number(source.cleanliness ?? 100) - elapsed * PET_DECAY_PER_MINUTE.cleanliness)),
  };
});
const hasPet = computed(() => Boolean(petState.value));
const petWillChange = computed(() => Boolean(hasPet.value && petChoice.value && petChoice.value !== petState.value?.type));
const petPickerHint = computed(() => {
  if (!hasPet.value) return '首次领养免费，确认后会保存到当前账号。';
  if (petWillChange.value) return `更换宠物需要 ${PET_CHANGE_COST} 金币，原来的饥饿、口渴和清洁状态会保留。`;
  return '当前选择和正在养的宠物相同，不会扣除金币。';
});
const petChoiceName = computed(() => petState.value?.name || '');
const petMood = computed(() => {
  const pet = petState.value;
  if (!pet) return 'waiting';
  const lowest = Math.min(pet.hunger, pet.thirst, pet.cleanliness);
  return lowest <= 0 ? 'sad' : lowest < 25 ? 'hungry' : 'happy';
});
const PET_SPEECHES = {
  happy: ['你好呀！', '你今天开心吗？', '今天天气不错！', '陪我聊聊天吧。', '我在这里陪你。'],
  hungry: ['我好饿……', '肚子咕咕叫了。', '可以给我一点吃的吗？'],
  sad: ['我累了……', '我有点低落。', '可以照顾我一下吗？'],
  waiting: ['你好呀！'],
};
// 活动消息的类型 → 中文标签，只用来拼折叠带上那行摘要（「红包 3 · 接龙 4」）。
// 认不出的显示「其他」：将来服务端加了新玩法、这里忘了补标签，也只是标签笼统，不会出错。
const ACTIVITY_LABELS = { lottery: '抽奖', wheel: '转盘', redpack_claim: '红包', redpack_create: '红包', prediction_start: '竞猜', prediction_vote: '竞猜', idiom: '接龙', game_start: '开局', game_win: '答对', truth: '真心话', dare: '大冒险', prompt_answer: '回答', horn: '大喇叭' };
// 哪些活动消息要显示「去参与」按钮、点了跳到互动中心的哪个页签。
// 只列**等人动手**的活动：开局（去答题）、真心话/大冒险（去回答这道题）、
// 红包发出（去抢）、竞猜开始（去下注）。
// 「结果类」不给按钮（lottery / wheel / game_win / redpack_claim / prediction_vote /
// prompt_answer / horn / idiom）—— 事已经办完了，再放个按钮只会让人白点一趟。
const ACTIVITY_JOIN = { game_start: 'games', truth: 'games', dare: 'games', redpack_create: 'social', prediction_start: 'social' };
// 按钮上的文字按活动类型说话，别一律写「去参与」——
// 「去参与」不知道参与什么，「去答题」「去回答」「去抢红包」一眼就懂。
const ACTIVITY_JOIN_LABEL = { game_start: '去答题', truth: '去回答', dare: '去回答', redpack_create: '去抢红包', prediction_start: '去下注' };
// 折叠带：连续相邻的「流水档」活动消息收成一条，遇到真人的话就断开成新的一组 ——
// 这样删掉的是刷屏，留下的是完整对话上下文。
// 判据只有 activity_tier === 'stream' 一处，哪些活动算流水由服务端决定
// （见 app.ACTIVITY_STREAM_KINDS），前端不靠文案前缀去猜 —— 那样改一个 emoji 就全乱。
// 哪些活动类型值得显示「去参与」按钮。判据只有这一处 ACTIVITY_JOIN，
// 模板里只认它返回的 tab，不再另写一份名单（改一处漏一处是最常见的毛病）。
function joinTabFor(message) {
  if (message?.kind !== 'activity') return '';
  return ACTIVITY_JOIN[message.activity_kind] || '';
}
function joinLabelFor(message) {
  return ACTIVITY_JOIN_LABEL[message.activity_kind] || '去参与';
}
// 点「去参与」= 打开互动中心并直接落到对应页签，省得进去再自己找。
// 私聊界面里不给参与：互动中心只作用于公共房间（见 interaction() 的拦截）。
function joinActivity(message) {
  const tab = joinTabFor(message);
  if (!tab) return;
  if (privateWith.value) { activityOutcome.value = { type: 'interaction_error', message: '房间互动请先返回公共房间。' }; return; }
  openActivities(tab);
}
const feed = computed(() => {
  const groups = [];
  for (const message of messages.value) {
    const isStream = message.kind === 'activity' && message.activity_tier === 'stream';
    const last = groups[groups.length - 1];
    if (isStream && last && last.stream) last.items.push(message);
    else if (isStream) groups.push({ id: `stream-${message.id}`, stream: true, items: [message] });
    else groups.push({ id: message.id, stream: false, message });
  }
  return groups;
});
function streamOpen(id) { return expandedStreams.value.includes(id); }
function toggleStream(id) { expandedStreams.value = streamOpen(id) ? expandedStreams.value.filter(item => item !== id) : expandedStreams.value.concat(id); }
function streamSummary(items) {
  const counts = new Map();
  for (const item of items) {
    const label = ACTIVITY_LABELS[item.activity_kind] || '其他';
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  return [...counts].map(([label, count]) => `${label} ${count}`).join(' · ');
}
// 商城分组顺序：礼物 → 徽章 → 宠物用品。头像在资料设置中直接选择，不属于商城。
const shopGroups = computed(() => [
  { key: 'gift', label: '🎁 礼物', items: shopItems.value.filter(item => item.item_type === 'gift') },
  { key: 'badge', label: '🏅 徽章', items: shopItems.value.filter(item => item.item_type === 'badge') },
  { key: 'pet', label: '🐾 宠物用品', items: shopItems.value.filter(item => item.item_type.startsWith('pet_')) }
].filter(group => group.items.length));
const formatSecondsLeft = computed(() => {
  void lastClock.value;
  return textFormat.value && textFormatExpiresAt.value ? Math.max(0, Math.ceil((textFormatExpiresAt.value - Date.now()) / 1000)) : 0;
});

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (token.value) headers['X-Session-Token'] = token.value;
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.message || '操作失败');
  return body;
}

function receive(data) {
  if (data.type === 'activities') activityPacket.value = data;
  if (data.type === 'interaction_result' || data.type === 'interaction_error') {
    activityOutcome.value = data;
    // 合并而不是整体替换：服务端若只下发公开字段，不能把本地已有的道具栏一并抹掉。
    if (data.user) user.value = { ...user.value, ...data.user };
    // 互动错误留在互动中心提示；尤其是大喇叭冷却提示，不要写进公共聊天区打扰其他人。
    if (data.type === 'interaction_error' && data.action !== 'horn') pushLive({ kind: 'system', id: `activity-error-${Date.now()}`, content: data.message });
  }
  if (data.type === 'ready') {
    user.value = data.user;
    if (data.room_id) {
      // ★ 进页面时是按默认的大厅拉历史，服务端真正所在的房间要等 ready 才知道。
      //   换了房间就必须重新拉一次，否则屏幕上会是「游戏房标题 + 大厅消息」。
      const roomChanged = data.room_id !== currentRoomId.value;
      currentRoomId.value = data.room_id;
      if (roomChanged) loadHistory();
    }
    if (data.announcement) announcement.value = data.announcement;
  }
  if (data.type === 'presence_notice') { if (!privateWith.value) showJoinBanner(data); }
  if (data.type === 'horn') showHornBanner(data);
  // users = 本房间名单；presence = 跨房间的在线变化（两种都带 online_ids + room_counts）。
  if (data.type === 'users') { users.value = data.users || []; applyPresence(data); }
  if (data.type === 'presence') applyPresence(data);
  if (data.type === 'message') {
    // 切房那一瞬间从旧房间漏过来的广播：连礼物动画都不该放（它不属于当前画面）。
    if (belongsHere(data.message)) {
      if (['gift', 'use_item', 'coin_gift'].includes(data.message.kind)) showItemAnimation(data.message);
      // ★ 私聊界面里不能混进房间的公共消息：这里原来是无条件追加，
      //   于是「只有你们两人能看到」的窗口里会冒出别人对全房间说的话。
      //   退出私聊时 loadHistory() 会把房间历史整块重拉，丢掉这几条不会缺内容。
      if (!privateWith.value) {
        pushLive({ kind: 'message', ...data.message });
        if (data.message.level_up && data.message.sender_id === user.value?.id) {
          pushLive({ kind: 'system', id: `level-${Date.now()}`, content: `🎉 恭喜你升到 Lv.${data.message.sender_level}「${data.message.level_title || levelTitle(data.message.sender_level)}」！奖励 ${data.message.level_reward || 10} 金币` });
        }
      }
    }
  }
  if (data.type === 'private_message') {
    const message = { kind: 'private', ...data.message };
    if (privateWith.value && (message.sender_id === privateWith.value.id || message.recipient_id === privateWith.value.id)) pushLive(message);
    else pushLive({ kind: 'system', id: `s-${Date.now()}`, content: `${message.sender_name} 发来了一条私聊消息` });
  }
  if (data.type === 'system') pushLive({ kind: 'system', id: `s-${Date.now()}-${Math.random()}`, content: data.message });
  if (data.type === 'error') { if (showShop.value) shopNotice.value = data.message; else pushLive({ kind: 'system', id: `s-${Date.now()}`, content: `提示：${data.message}` }); if (showActivities.value) activityOutcome.value = { type: 'interaction_error', message: data.message }; }
  // 消息被撤回：画面里那份、待补回的底稿、**以及随后落地的历史响应**，三处都要挡住。
  //   前两处当场摘掉；第三处靠 rememberDeleted 留一个墓碑（否则删完历史一回来它又出现）。
  if (data.type === 'message_deleted') { rememberDeleted(data.message_id); messages.value = messages.value.filter(m => m.id !== data.message_id); liveBuffer = liveBuffer.filter(item => item.entry.id !== data.message_id); }
  // 换房间时公告整块跟着换（每个房间一份）；正在编辑的草稿一并作废，
  // 否则会把 A 房间写了一半的内容存到 B 房间去。
  if (data.type === 'room_joined') { currentRoomId.value = data.room_id; privateWith.value = null; editingAnnouncement.value = false; announcementNotice.value = ''; if (data.announcement) announcement.value = data.announcement; loadHistory(); }
  // 别人（或另一个管理员）刚改了本房间的公告 —— 立刻刷新，不用等重连。
  if (data.type === 'announcement_updated') { if (data.room_id === currentRoomId.value) announcement.value = data.announcement; }
  if (data.type === 'profile_updated') {
    users.value = users.value.map(item => item.id === data.user.id ? { ...item, ...data.user } : item);
    if (data.user.id === user.value?.id) user.value = { ...user.value, ...data.user };
    updateMessagesForProfile(data.user);
    // 正在查看资料卡时也立即刷新公开资料，避免弹窗停留旧头像或昵称。
    if (profileCard.value?.id === data.user.id) profileCard.value = { ...profileCard.value, ...data.user };
  }
  if (data.type === 'shop_result') { user.value = { ...user.value, ...data.user }; shopNotice.value = data.message; }
  if (data.type === 'item_received') {
    // 服务端会把收礼后的本人信息一并带上（含 inventory）—— 不合并的话新礼物要刷新才出现。
    if (data.user) user.value = { ...user.value, ...data.user };
    pushLive({ kind: 'system', id: `item-received-${Date.now()}`, content: `🎁 你收到了 ${data.sender_name} 送来的 ${data.icon} ${data.item_name}，已存入物品栏` });
  }
  if (data.type === 'coins_received') {
    if (data.user) user.value = { ...user.value, ...data.user };
    pushLive({ kind: 'system', id: `coins-received-${Date.now()}`, content: `💰 你收到了 ${data.sender_name} 送来的 ${data.amount} 金币` });
  }
  if (data.type === 'kicked') { window.alert(data.message); logout(false); }
  nextTick(scrollMessages);
}

let reconnectTimer = null;
let pingTimer = null;
let leaving = false;
let resyncOnReconnect = false;

function startHeartbeat() {
  window.clearInterval(pingTimer);
  pingTimer = window.setInterval(() => send({ type: 'ping' }), 25000);
}
function stopHeartbeat() { window.clearInterval(pingTimer); pingTimer = null; }
function stopReconnect() { if (reconnectTimer) { window.clearTimeout(reconnectTimer); reconnectTimer = null; } }
function scheduleReconnect() {
  stopReconnect();
  reconnectTimer = window.setTimeout(() => { if (!leaving && token.value) connect(); }, 2000);
}
function handleSessionLost(code) {
  const tips = code === 4004 ? '账号已在另一个窗口进入，本窗口已退出。' : '登录状态已失效，请重新进入聊天室。';
  logout(false);
  entryError.value = tips;
}
function connect() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${scheme}://${window.location.host}/ws?token=${encodeURIComponent(token.value)}`);
  socket.value = ws; connected.value = false;
  ws.onopen = () => {
    connected.value = true; stopReconnect(); startHeartbeat();
    // 重连成功后补拉一次历史：断线期间的广播服务端不会重推，
    // 不补的话这个学生的屏幕上会永久缺一段。
    if (resyncOnReconnect) { resyncOnReconnect = false; loadHistory(); }
  };
  ws.onclose = event => {
    connected.value = false; stopHeartbeat();
    if (event && (event.code === 4003 || event.code === 4004)) { handleSessionLost(event.code); return; }
    // 网络抖动、服务重启、或服务端因积压主动断开（4008）时自动重连，学生不需要手动重新进入。
    if (!leaving && token.value) { resyncOnReconnect = true; scheduleReconnect(); }
  };
  ws.onmessage = event => receive(JSON.parse(event.data));
}

function send(data) { if (socket.value && socket.value.readyState === WebSocket.OPEN) socket.value.send(JSON.stringify(data)); }
function interaction(data) {
  if (privateWith.value) { activityOutcome.value = { type: 'interaction_error', message: '房间互动请先返回公共房间。' }; return; }
  if (!connected.value) { activityOutcome.value = { type: 'interaction_error', message: '连接已断开，请重新进入聊天室。' }; return; }
  send({ type: 'interaction', ...data });
}
const MESSAGE_BOTTOM_THRESHOLD = 24;
function isMessageNearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight <= MESSAGE_BOTTOM_THRESHOLD; }
function handleMessagesScroll(event) {
  const el = event.target;
  if (!el?.classList?.contains('messages')) return;
  if (isMessageNearBottom(el)) {
    autoScrollEnabled.value = true;
    pendingMessageCount.value = 0;
  } else {
    autoScrollEnabled.value = false;
  }
}
function scrollMessages(force = false) {
  const el = messagesEl.value || document.querySelector('.messages');
  if (!el) return;
  if (force) {
    autoScrollEnabled.value = true;
    pendingMessageCount.value = 0;
  }
  if (!autoScrollEnabled.value) return;
  el.scrollTop = el.scrollHeight;
}
function resumeAutoScroll() {
  autoScrollEnabled.value = true;
  pendingMessageCount.value = 0;
  nextTick(() => scrollMessages(true));
}
function resetMessageScroll() {
  autoScrollEnabled.value = true;
  pendingMessageCount.value = 0;
}

// ★★ 「会话代次」：每进入一次会话（登录 / 注册 / 游客 / 页面带 token 自动登录）或退出一次
//    会话，都 +1。**只在本次会话里有效**的异步响应，落地前都要核对自己出发时的代次 ——
//    对不上就整段丢弃。要挡的是这几类（都已在浏览器里复现过）：
//      ① 上一个账号「查看资料 / 保存资料」的响应回来，把新账号的昵称、身份、金币整组覆盖掉；
//      ② 上一个账号的初始化流程跑完后还给新账号建了一次连接 —— 服务端踢掉旧连接，新账号
//         被误判成「账号已在另一个窗口进入」而自动退出；
//      ③ 上一个账号的 /api/me 失败时去清 token，把新账号刚存好的 token 也一并清掉。
//    ⚠️ `historySeq` 是同一思路的**画面代次**，专管历史请求。两个维度不能互相替代：
//       换账号要动 sessionEpoch；换房间 / 切私聊要动 historySeq。
let sessionEpoch = 0;
function epochAlive(started) { return started === sessionEpoch; }

// 每次请求带一个自增序号：晚到的旧响应不能覆盖新画面。
// 场景：快速进私聊再退出，私聊那次请求的响应可能最后才回来 —— 没有这道闸门它会把
// 房间画面整块盖掉（表现是「公共房间里显示着私聊记录」）。用序号比「当前对象还是不是它」
// 更可靠：在 A↔B 之间来回切时，后者会误判成同一处而放过旧响应。
let historySeq = 0;
// 正在飞的历史请求序号集合：非空时收到的实时消息要留一份底（见 pushLive）。
// ★ 用集合而不是计数器：logout 会把它清空，于是**旧账号那次请求**回来时删的是一个
//   「已经不存在的序号」，不会误减新账号刚加上的计数。计数器版本会 —— 表现是：换账号后
//   新账号的历史还没落地就不再缓冲，刚冒出来的实时消息被历史整块覆盖（先显示、再消失）。
let historyInFlight = new Set();
// ★ 历史是**整体覆盖** messages 的：请求在飞的时候收到的实时消息会被它整块抹掉
//   （表现：新消息先冒出来、历史一回来就没了，得自己再拉一次才看得到）。
//   所以请求期间收到的实时消息先攒在这里，历史落地后按 id 去重补回去。
let liveBuffer = [];
// 历史请求返回的是快照，资料更新可能先于它到达；落地历史时再应用这些最新公开字段。
let profileOverrides = new Map();
// ★ 「墓碑」：被管理员删掉的消息 id。删除通知只把画面上那份和待补回的底稿摘掉 ——
//   要是此刻正好有个 /api/messages 在飞（它取的是删除**之前**的快照），落地时那句
//   整块赋值会把它又摆回画面上（实测：删掉之后历史一回来它又出现了）。
//   所以删除时记一笔，历史落地时再按它过滤一次。
let deletedIds = new Set();
// 墓碑只用来挡住「正在飞的那一次响应」，不是永久账本：只留最近若干条。
const DELETED_IDS_KEEP = 500;
function rememberDeleted(id) {
  if (id === undefined || id === null) return;
  deletedIds.add(id);
  while (deletedIds.size > DELETED_IDS_KEEP) deletedIds.delete(deletedIds.values().next().value);
}
// 当前画面是「哪个房间」还是「和谁的私聊」——补回实时消息时只认同一个视图，
// 否则会把上一个房间的消息串到新画面里。
function viewKey() { return privateWith.value ? `private:${privateWith.value.id}` : `room:${currentRoomId.value}`; }
// ★ 这条实时消息属不属于「当前这个画面」。**规则只在这里写一遍** ——
//   ① 房间消息自带 room_id（服务端 save_message 的返回里就有）。切房间是「本地先切、
//      服务端后切」：点下去到服务端真正把你移进新房间之间，旧房间的广播仍会推到这条连接上。
//      照单全收的话，这些消息会被当成新房间的消息、在历史落地时补进新房间
//      （实测：切到游戏房之后，大厅里刚说的话出现在游戏房的画面里）。
//   ② 已经删掉的消息，不许再从任何实时通道回来。
function belongsHere(entry) {
  if (entry.room_id && entry.room_id !== 'private' && entry.room_id !== currentRoomId.value) return false;
  if (entry.id !== undefined && deletedIds.has(entry.id)) return false;
  return true;
}
// ★ 服务端实时推送的消息统一从这里进画面：既 push，也在历史请求期间留一份底。
//   别再往 messages.value 直接 push —— 那样只要历史请求正在飞就会丢（踩过）。
//   一律先过 belongsHere：不属于当前画面的（旧房间漏过来的 / 已删除的）在这里就被挡住。
function pushLive(entry) {
  if (!belongsHere(entry)) return;
  messages.value.push(entry);
  if (!autoScrollEnabled.value) pendingMessageCount.value += 1;
  if (historyInFlight.size > 0) liveBuffer.push({ key: viewKey(), entry });
}

// 资料广播到达后，已显示的历史消息也要跟着使用最新的公开资料。
// 服务端保存消息时会写入当时的资料，因此不能只更新在线名单。
function updateMessagesForProfile(profile) {
  if (!profile?.id) return;
  const fields = {};
  const mapping = {
    nickname: 'sender_name',
    avatar: 'sender_avatar',
    badge: 'sender_badge',
    role: 'sender_role',
    level: 'sender_level',
    level_title: 'level_title',
  };
  for (const [source, target] of Object.entries(mapping)) {
    if (profile[source] !== undefined) fields[target] = profile[source];
  }
  if (!Object.keys(fields).length) return;
  const profileKey = String(profile.id);
  profileOverrides.set(profileKey, { ...(profileOverrides.get(profileKey) || {}), ...fields });
  const sameUser = message => message?.sender_id === profile.id || String(message?.sender_id) === String(profile.id);
  messages.value = messages.value.map(message => sameUser(message) ? { ...message, ...fields } : message);
  liveBuffer = liveBuffer.map(item => item?.entry && sameUser(item.entry)
    ? { ...item, entry: { ...item.entry, ...fields } }
    : item);
}

async function loadHistory() {
  const seq = ++historySeq;
  historyInFlight.add(seq);
  const key = viewKey();
  const privateTarget = privateWith.value ? privateWith.value.id : null;
  const params = new URLSearchParams();
  if (privateTarget) params.set('private_with', privateTarget);
  else params.set('room_id', currentRoomId.value);
  let result = null;
  let failure = '';
  try {
    result = await api(`/api/messages?${params.toString()}`);
  } catch (error) {
    // 断网、服务重启、连接被掐 —— 这一枪都可能打空。以前这里直接往外抛，而调用点大多是
    // 「发了不管」（onopen / 切房 / 收到 room_joined），于是变成未处理的 Promise 拒绝：
    // 控制台一条红字，界面上却什么提示都没有。
    failure = (error && error.message) || '网络异常';
  } finally {
    // 只删自己那一个序号：logout 已把集合清空，这里删的是不存在的项，不会动新账号的计数。
    historyInFlight.delete(seq);
  }
  // 返回值是「本次用的序号」：调用方拿它跟当前的 historySeq 比对，就知道这次是不是
  // 已经被更新的一次请求取代了 —— 用来决定「等历史落地后再补的系统提示」还要不要插。
  if (seq !== historySeq) return seq;
  if (!result) {
    // ★ 拉失败时**绝不动**画面上已有的消息（宁可留着旧记录，也不能清成一片空白），
    //   只补一条看得见的提示。走 pushLive 而不是直接 push，跟其它下行保持一致。
    pushLive({ kind: 'system', id: `hist-fail-${seq}`, content: `⚠️ 聊天记录加载失败：${failure}（可以切个房间或稍后再试）` });
    await nextTick(scrollMessages);
    return seq;
  }
  // 请求期间收到的实时消息：只取属于**当前这个视图**的那些。
  // 其余 key 的条目属于已被取代的请求（它们属于旧视图），留着只会串台，直接丢掉。
  const live = liveBuffer.filter(item => item.key === key).map(item => item.entry);
  liveBuffer = [];
  // 墓碑过滤：这次响应的数据是在删除**之前**取的，里面可能还带着刚被删掉的那条。
  messages.value = result.messages
    .filter(message => !deletedIds.has(message.id))
    .map(message => {
      const override = profileOverrides.get(String(message.sender_id));
      return { kind: privateTarget ? 'private' : 'message', ...message, ...(override || {}) };
    });
  // 历史里没有的实时消息补回去（按 id 去重）：历史响应是快照，可能比这些消息更旧。
  const seen = new Set(messages.value.map(item => item.id));
  for (const entry of live) {
    if (entry.id !== undefined && (seen.has(entry.id) || deletedIds.has(entry.id))) continue;
    messages.value.push(entry);
    if (entry.id !== undefined) seen.add(entry.id);
  }
  // 整体换了消息（切房间 / 重拉历史），之前点开的折叠带就作废了：组的 id 是按消息 id 生成的，
  // 不清掉只会越积越多，而且下次恰好撞上同一个 id 时会莫名其妙地自动展开。
  expandedStreams.value = [];
  await nextTick(scrollMessages);
  return seq;
}

async function loadRooms() { const result = await api('/api/rooms'); rooms.value = result.rooms; }
// 房间列表上的人数不会自己变（loadRooms 只在进页面 / 切房时调）。
// ★ 服务端每次有人加入 / 离开 / 换房间都会广播一份「全站 presence」，里面带 room_counts
//   （每个房间各几个人）。**不能只改当前房间那一个数字** —— 别的房间有人走了，
//   左侧那个数字会一直留着旧值（实测：B 房的人离开，A 房看到的还是旧人数）。
function syncRoomOnline(counts) {
  if (!counts) return;
  // ★ 缺键 = 这个房间**一个人都没有**（服务端只统计有人的房间），必须按 0 处理。
  //   早先写成「缺键就保留旧值」，表现就是「人走了，人数还停在 1」（浏览器探针抓到的）。
  rooms.value = rooms.value.map(room => ({ ...room, online: counts[room.id] || 0 }));
}
// users 与 presence 两种下行都带 online_ids / room_counts，统一在这里落到前端状态上。
function applyPresence(data) {
  if (Array.isArray(data.online_ids)) onlineIds.value = data.online_ids;
  syncRoomOnline(data.room_counts);
}
async function loadShop() { const result = await api('/api/shop'); shopItems.value = result.items; }
async function loadAvatars() { const result = await api('/api/avatars'); avatarOptions.value = result.avatars; }
async function loadCustomEmojis() { const result = await api('/api/emojis'); customEmojis.value = Array.isArray(result.emojis) ? result.emojis : []; }
// 公告取不到时保留当前内容即可，不能因为这一项失败就挡住进入聊天室。
async function loadAnnouncement(roomId = currentRoomId.value) {
  try {
    const result = await api(`/api/announcement?room_id=${encodeURIComponent(roomId)}`);
    // ★ 回来时房间可能已经换了（打开公告到响应返回之间用户切了房）：这一次请求属于旧房间。
    //   旧房间的正文不能写进画面；currentRoomId 更不能被改回去 —— 改回去后标题与实际
    //   连接（还在新房间）就对不上：新房间的消息会被房间过滤挡掉，发出去的消息也和标题不是一间。
    if (roomId !== currentRoomId.value) return;
    if (result.announcement) announcement.value = result.announcement;
    if (result.room_id) currentRoomId.value = result.room_id;
  } catch (_) {}
}

// 打开公告弹窗（只读）。每次打开都重取一次，免得显示的是很久以前缓存的旧内容。
function openAnnouncement() {
  editingAnnouncement.value = false;
  announcementNotice.value = '';
  showAnnouncement.value = true;
  loadAnnouncement();
}
function startEditAnnouncement() {
  announcementDraft.value = announcement.value;
  announcementNotice.value = '';
  editingAnnouncement.value = true;
}
function cancelEditAnnouncement() {
  editingAnnouncement.value = false;
  announcementNotice.value = '';
}
async function saveAnnouncement() {
  announcementNotice.value = '';
  // ★ 这一枪属于**发起时**那个房间。读取那条路（loadAnnouncement）已经加了同样的校验，
  //   保存这条路漏了：保存大厅公告后、响应回来前关掉弹窗并切到游戏房，落地时会把大厅的
  //   公告写进游戏房的侧栏（实测复现）。发起时先锁定房间，回来对不上就整段丢弃。
  const roomId = currentRoomId.value;
  try {
    const result = await api('/api/announcement', {
      method: 'POST',
      body: JSON.stringify({ room_id: roomId, content: announcementDraft.value })
    });
    if (roomId !== currentRoomId.value) return;
    announcement.value = result.announcement;
    editingAnnouncement.value = false;
    announcementNotice.value = result.message || '公告已更新';
  } catch (error) {
    if (roomId !== currentRoomId.value) return;
    // 字数超限、有违禁词、保存太频繁都由服务端给出具体原因，原样显示，别吞掉。
    announcementNotice.value = error.message;
  }
}

async function enterSession(data) {
  // ★ 新一代从这里开始：上一个账号还在飞的任何响应，此后落地都会被 epochAlive 挡掉。
  sessionEpoch += 1;
  const epoch = sessionEpoch;
  leaving = false; stopReconnect();
  token.value = data.token; user.value = data.user; profileAvatar.value = data.user.avatar || '🙂'; localStorage.setItem('lan_chat_token', token.value); screen.value = 'chat';
  try {
    await Promise.all([loadRooms(), loadShop(), loadAvatars(), loadCustomEmojis(), loadAnnouncement()]);
    // ★ 这中间用户可能已经退出、或又换了账号：那就绝不能再用**旧 token**去 connect()，
    //   否则会给新账号多建一条连接（服务端踢掉旧的 → 新账号被误判成「另一个窗口进入」）。
    if (!epochAlive(epoch)) return;
    connect();
    await loadHistory();
  } catch (error) {
    if (!epochAlive(epoch)) return;   // 上一次会话的失败，不许往新会话的画面里插提示
    pushLive({ kind: 'system', id: 'load-error', content: error.message });
  }
}

async function login() { entryError.value = ''; try { await enterSession(await api('/api/login', { method: 'POST', body: JSON.stringify(loginForm.value) })); } catch (error) { entryError.value = error.message; } }
async function register() { entryError.value = ''; try { await enterSession(await api('/api/register', { method: 'POST', body: JSON.stringify(registerForm.value) })); } catch (error) { entryError.value = error.message; } }
async function guest() { entryError.value = ''; try { await enterSession(await api('/api/guest', { method: 'POST', body: JSON.stringify({ nickname: guestName.value }) })); } catch (error) { entryError.value = error.message; } }

async function logout(callServer = true) {
  leaving = true; stopReconnect(); stopHeartbeat();
  if (callServer) await api('/api/logout', { method: 'POST' }).catch(() => {});
  socket.value?.close(); socket.value = null; token.value = ''; user.value = null; screen.value = 'entry';
  // ★ 会话级状态必须一并清干净。只清 token 的话，换个账号登进来会**继承上一个账号**的
  //   私聊对象、消息列表、在线名单 —— 表现是「刚登录就显示着上一个人的私聊栏」，
  //   一不小心就把消息发给了不该发的人。
  // ★ 在飞的历史请求必须当场作废。只清界面状态是不够的：那个请求的序号仍是「最新的」，
  //   它带着**上一个账号**的私聊记录回来时会直接写进新账号的页面（实测：刚登进来的新游客
  //   屏幕上出现了上一个账号的私聊）。把序号推高一格，旧响应回来就被闸门丢掉了。
  // ★ 推高一格「会话代次」：上一个账号的在飞响应（查看资料 / 保存资料 / 初始化）此后全部作废。
  sessionEpoch += 1;
  historySeq += 1; historyInFlight.clear(); liveBuffer = []; deletedIds = new Set(); profileOverrides = new Map();
  resetMessageScroll(); privateWith.value = null; messages.value = []; users.value = []; onlineIds.value = [];
  expandedStreams.value = []; rooms.value = []; currentRoomId.value = 'lobby';
  search.value = ''; input.value = ''; customEmojis.value = []; resetTextFormat();
  showActivities.value = false; showShop.value = false; showProfile.value = false;
  recycleBusy.value = false; shopView.value = 'buy';
  showPetPicker.value = false; petPickerNotice.value = ''; petFeedback.value = ''; petBusy.value = false; petActionAnimation.value = ''; petReaction.value = ''; petBubbleText.value = '';
  if (petActionTimer) window.clearTimeout(petActionTimer);
  petActionTimer = null;
  if (petClickTimer) window.clearTimeout(petClickTimer);
  petClickTimer = null;
  if (petBubbleTimer) window.clearTimeout(petBubbleTimer);
  petBubbleTimer = null;
  if (petSpeechTimer) window.clearTimeout(petSpeechTimer);
  petSpeechTimer = null;
  showAnnouncement.value = false; showGifts.value = false; context.value = null;
  closeAvatarPreview();
  profileCard.value = null; activityPacket.value = null; activityOutcome.value = null;
  shopNotice.value = ''; profileNotice.value = ''; entryError.value = '';
  editingAnnouncement.value = false; announcementDraft.value = ''; announcementNotice.value = '';
  joinBanner.value = null; itemAnimation.value = null;
  window.clearTimeout(joinBannerTimer);
  joinBannerTimer = null;
  if (itemAnimationTimer) window.clearTimeout(itemAnimationTimer);
  itemAnimationTimer = null;
  localStorage.removeItem('lan_chat_token');
  leaving = false;
}

function submit() { const content = input.value.trim(); if (!content) return; const format = { text_style: textStyle.value, text_effect: textEffect.value }; send(privateWith.value ? { type: 'private_message', target_id: privateWith.value.id, content, ...format } : { type: 'message', content, ...format }); input.value = ''; }
function chooseEmoji(emoji) { input.value += emoji; showEmoji.value = false; }
function chooseCustomEmoji(emoji) { input.value += emoji.token; showEmoji.value = false; }
function sendGiftItem(item) {
  if (!giftTarget.value) {
    pushLive({ kind: 'system', id: `s-${Date.now()}`, content: '请先在右侧在线用户中选择一位好友，再操作礼物或道具' });
    return;
  }
  send({ type: 'gift', item_id: item.id, target_id: giftTarget.value.id });
  showGifts.value = false;
}
function sendCoinGift() {
  const amount = Number(coinGiftAmount.value);
  if (!Number.isInteger(amount) || amount < 1 || amount > 10000) {
    pushLive({ kind: 'system', id: `s-${Date.now()}`, content: '金币数量必须是 1～10000 的整数' });
    return;
  }
  if (!giftTarget.value) {
    pushLive({ kind: 'system', id: `s-${Date.now()}`, content: '请先在右侧在线用户中选择一位好友，再赠送金币' });
    return;
  }
  if (!isUnlimitedCoins.value && Number(user.value?.coins || 0) < amount) {
    pushLive({ kind: 'system', id: `s-${Date.now()}`, content: `金币不足，你只有 ${user.value?.coins || 0} 金币` });
    return;
  }
  send({ type: 'coin_gift', target_id: giftTarget.value.id, amount });
  showGifts.value = false;
}
function useItem(item) { if (inventoryCount(item.id) < 1) return; send({ type: 'use_item', item_id: item.id }); showGifts.value = false; }
function openGiftMenu(mode) { context.value = null; giftMenuMode.value = mode; if (mode === 'send') coinGiftAmount.value = 10; showGifts.value = true; }
// loadRooms() 只是「顺手刷一下左侧人数」：它不是切换房间的必要条件，失败了留着旧列表就行，
// 下一次成功时自然补上。但**必须接住**它的失败 —— 不接就是未处理的 Promise 拒绝，
// 跟 loadHistory 里那处是同一个坑（切房 / 重连时网络一断就会冒出来）。
function selectRoom(room) { if (room.id === currentRoomId.value && !privateWith.value) return; resetMessageScroll(); privateWith.value = null; currentRoomId.value = room.id; messages.value = []; send({ type: 'join_room', room_id: room.id }); loadHistory(); loadRooms().catch(() => {}); }
function goLobby() { const lobby = rooms.value.find(room => room.id === 'lobby'); if (lobby) selectRoom(lobby); else leavePrivate(); }
// 进出私聊的两条系统提示：必须等 loadHistory() 落地之后再插。
// loadHistory 内部有 `messages.value = result.messages.map(...)` 这一步，
// 它是异步的（要等 HTTP 回来），提前 push 会被它整块覆盖掉 —— 表现是「刚进私聊提示闪一下就没了」。
async function openPrivate(target) {
  if (!target || target.id === user.value?.id) return;
  resetMessageScroll(); privateWith.value = target; messages.value = [];
  const seq = await loadHistory();
  // ★ 等历史落地的这段时间里，用户可能已经退出私聊（或又换了别人）。这时**不能**再插这条提示：
  //   否则会出现「已经回到公共房间、却写着只有两人能看到」，而紧接着发出去的那句话其实是
  //   **公开**的（实测过的坑）。seq 对不上 = 这次请求已被更新的一次取代；对象对不上 = 早离开了。
  if (seq !== historySeq || privateWith.value?.id !== target.id) return;
  pushLive({ kind: 'system', id: `enter-private-${Date.now()}`, content: `你正在与 ${target.nickname} 私聊，只有你们两人能看到这些消息` });
}
// 退出私聊：回到原来的公共房间。
// 用 currentRoomId 兜底而不是 roomNameOf —— currentRoomId 在私聊期间一直保留着「刚才那个房间」，
// 直接回退到它，聊天记录和公告都不用重新拉，视觉上也跟「从没离开过这个房间」一致。
async function leavePrivate() {
  if (!privateWith.value) return;
  const name = privateWith.value.nickname;
  resetMessageScroll();
  privateWith.value = null;
  messages.value = [];
  const seq = await loadHistory();
  // 同样的道理：等待期间如果又进了别人的私聊，这条「已退出」就不该再插。
  if (seq !== historySeq) return;
  pushLive({ kind: 'system', id: `leave-private-${Date.now()}`, content: `已退出与 ${name} 的私聊，回到房间聊天` });
}
// 打开设置面板时把当前资料回填进去，让学生看到「现在是什么」再改。
// me 上的 real_name / class_name / bio / username 来自 self_public()（只发给本人），
// 别人的这些字段在在线列表里拿不到 —— 那是要花 1 金币才看得见的。
function openProfile() {
  const me = user.value || {};
  profileAvatar.value = me.avatar || '🙂';
  const parsed = splitClassName(me.class_name);
  profileForm.value = {
    nickname: me.nickname || '', real_name: me.real_name || '',
    grade: parsed.grade, classNo: parsed.classNo,
    bio: me.bio || '', oldPassword: '', newPassword: '', confirmPassword: ''
  };
  classTouched.value = false;
  profileNotice.value = '';
  showProfile.value = true;
}
async function saveProfile() {
  profileNotice.value = '';
  const form = profileForm.value;
  if (form.newPassword && form.newPassword !== form.confirmPassword) { profileNotice.value = '两次输入的新密码不一致，请重新输入。'; return; }
  const me = user.value || {};
  // 只把真正变了的字段发上去：服务端按「变化处数」计费，发多了没变也不会收钱，
  // 但少发能避免把「昵称被清空」这类误操作带上去。
  const body = {};
  if (profileAvatar.value !== (me.avatar || '🙂')) body.avatar = profileAvatar.value;
  const nickname = form.nickname.trim();
  if (nickname && nickname !== me.nickname) body.nickname = nickname;
  if (form.real_name.trim() !== (me.real_name || '')) body.real_name = form.real_name.trim();
  if (effectiveClassName.value !== (me.class_name || '')) body.class_name = effectiveClassName.value;
  if (form.bio.trim() !== (me.bio || '')) body.bio = form.bio.trim();
  if (form.newPassword) { body.new_password = form.newPassword; body.old_password = form.oldPassword; }
  if (!Object.keys(body).length) { profileNotice.value = '还没有改动任何内容。'; return; }
  const epoch = sessionEpoch;   // ★ 与 viewProfile 同构：保存资料的响应同样不能跨会话落地
  try {
    const result = await api('/api/profile', { method: 'POST', body: JSON.stringify(body) });
    if (!epochAlive(epoch)) return;
    user.value = { ...user.value, ...result.user };
    profileNotice.value = result.message || '资料已更新。';
    profileForm.value = { ...profileForm.value, oldPassword: '', newPassword: '', confirmPassword: '' };
  } catch (error) { if (epochAlive(epoch)) profileNotice.value = error.message; }
}
// 查看他人资料：1 金币一次（同一次登录里对同一个人只扣一次）。看自己、管理员查看都不收费。
async function viewProfile(target) {
  context.value = null;
  if (!target?.id) return;
  const epoch = sessionEpoch;
  try {
    const result = await api('/api/profile/view', { method: 'POST', body: JSON.stringify({ user_id: target.id }) });
    // ★ 回来先看是不是同一次会话。这中间退出 / 换账号的话，这张名片属于上一个账号 ——
    //   尤其 result.user 带的是**上一个账号**的余额等，合并进 user 会把新账号的昵称、
    //   身份、金币整组覆盖掉（实测：C 登录后屏幕上显示着 A 的昵称与金币）。
    if (!epochAlive(epoch)) return;
    profileCard.value = result.card;
    profileCardNotice.value = result.message || (result.charged ? `已扣除 ${result.charged} 金币` : '本次未扣金币');
    if (result.user) user.value = { ...user.value, ...result.user };
  } catch (error) {
    if (!epochAlive(epoch)) return;
    pushLive({ kind: 'system', id: `profile-view-${Date.now()}`, content: `查看资料失败：${error.message}` });
  }
}
function roomNameOf(roomId) { return rooms.value.find(room => room.id === roomId)?.name || (roomId === 'private' ? '私聊' : '未知'); }
function isBadgeEquipped(item) { return item.item_type === 'badge' && user.value?.badge_id === item.id; }
function shopActionLabel(item) {
  const owned = inventoryCount(item.id) > 0;
  if (item.price === 0) return '领取';
  if (item.item_type === 'badge') return owned ? (isBadgeEquipped(item) ? '佩戴中' : '佩戴') : '兑换';
  if (['avatar', 'badge'].includes(item.item_type) && owned) return '已拥有';
  return '兑换';
}
function shopActionDisabled(item) {
  if (item.price === 0) return false;
  if (item.item_type === 'badge' && inventoryCount(item.id) > 0) return isBadgeEquipped(item);
  if (['avatar', 'badge'].includes(item.item_type) && inventoryCount(item.id) > 0) return true;
  return !isUnlimitedCoins.value && Number(user.value?.coins || 0) < item.price;
}
function buyItem(item) {
  // 已拥有的徽章点按钮＝换着戴，不用再花金币。
  if (item.item_type === 'badge' && inventoryCount(item.id) > 0) { if (!isBadgeEquipped(item)) useItem(item); return; }
  if (!isUnlimitedCoins.value && item.price > 0 && Number(user.value?.coins || 0) < item.price) { shopNotice.value = `金币不足，还需要 ${item.price - Number(user.value?.coins || 0)} 金币`; return; }
  shopNotice.value = ''; send({ type: 'shop_buy', item_id: item.id });
}
async function recycleItems(items) {
  if (recycleBusy.value) return;
  const epoch = sessionEpoch;
  recycleBusy.value = true; shopNotice.value = '';
  try {
    const result = await api('/api/shop/recycle', { method: 'POST', body: JSON.stringify({ items }) });
    if (!epochAlive(epoch)) return;
    user.value = { ...user.value, ...result.user };
    shopNotice.value = result.message;
  } catch (error) {
    if (epochAlive(epoch)) shopNotice.value = error.message;
  } finally { if (epochAlive(epoch)) recycleBusy.value = false; }
}
function petAsset(type) { return '/static/pets/' + type + '.svg'; }
function petStatPercent(value) { return String(Math.round(Math.max(0, Math.min(100, Number(value) || 0)))) + '%'; }
function petStatClass(value) {
  const score = Number(value) || 0;
  return score <= 0 ? 'empty' : score < 25 ? 'low' : '';
}
function petActionItem(action) { return PET_ACTIONS.find(item => item.action === action) || PET_ACTIONS[0]; }
function openPetPicker() {
  if (isGuest.value) {
    petFeedback.value = '游客不能养宠物，请注册账号后再来。';
    return;
  }
  if (!petUnlocked.value) {
    petFeedback.value = `等级达到 Lv.${petMinLevel.value} 后才能养宠物。`;
    return;
  }
  petChoice.value = petState.value?.type || 'chicken';
  petPickerNotice.value = '';
  showPetPicker.value = true;
}
async function confirmPet() {
  if (!petChoice.value || petBusy.value) return;
  if (!petUnlocked.value) {
    petPickerNotice.value = `等级达到 Lv.${petMinLevel.value} 后才能养宠物。`;
    return;
  }
  if (petWillChange.value && !isUnlimitedCoins.value && Number(user.value?.coins || 0) < PET_CHANGE_COST) {
    petPickerNotice.value = `更换宠物需要 ${PET_CHANGE_COST} 金币，你只有 ${Number(user.value?.coins || 0)} 金币。`;
    return;
  }
  petBusy.value = true;
  petPickerNotice.value = '';
  const epoch = sessionEpoch;
  try {
    const result = await api('/api/pet/adopt', { method: 'POST', body: JSON.stringify({ pet_type: petChoice.value }) });
    if (!epochAlive(epoch)) return;
    user.value = { ...user.value, ...result.user };
    showPetPicker.value = false;
    petFeedback.value = result.message || '宠物已经准备好啦！';
    showPetSpeech('你好呀！');
    queuePetSpeech();
  } catch (error) {
    if (epochAlive(epoch)) petPickerNotice.value = error.message;
  } finally {
    petBusy.value = false;
  }
}
async function usePetAction(action) {
  if (!petState.value || petBusy.value) return;
  const item = petActionItem(action);
  if (inventoryCount(item.itemId) < 1) {
    petFeedback.value = '请先到等级商城购买' + item.label + '用品。';
    return;
  }
  petBusy.value = true;
  petFeedback.value = '';
  petActionAnimation.value = item.animation;
  if (petActionTimer) window.clearTimeout(petActionTimer);
  petActionTimer = window.setTimeout(() => { petActionAnimation.value = ''; }, 1100);
  const epoch = sessionEpoch;
  try {
    const result = await api('/api/pet/action', { method: 'POST', body: JSON.stringify({ action }) });
    if (!epochAlive(epoch)) return;
    user.value = { ...user.value, ...result.user };
    petFeedback.value = result.message || (item.label + '成功。');
  } catch (error) {
    if (epochAlive(epoch)) petFeedback.value = error.message;
  } finally {
    petBusy.value = false;
  }
}
function reactToPet() {
  if (!hasPet.value) return;
  petReaction.value = 'love';
  showPetSpeech('你好呀！');
  if (petClickTimer) window.clearTimeout(petClickTimer);
  petClickTimer = window.setTimeout(() => { petReaction.value = ''; }, 850);
}
function showPetSpeech(text = '') {
  const choices = PET_SPEECHES[petMood.value] || PET_SPEECHES.happy;
  petBubbleText.value = text || choices[Math.floor(Math.random() * choices.length)];
  if (petBubbleTimer) window.clearTimeout(petBubbleTimer);
  petBubbleTimer = window.setTimeout(() => { petBubbleText.value = ''; }, 4800);
}
function queuePetSpeech() {
  if (petSpeechTimer) window.clearTimeout(petSpeechTimer);
  petSpeechTimer = null;
  if (!hasPet.value) return;
  petSpeechTimer = window.setTimeout(() => {
    petSpeechTimer = null;
    if (hasPet.value) {
      showPetSpeech();
      queuePetSpeech();
    }
  }, 18000 + Math.random() * 12000);
}
// 同理：打不开商城也要说一句，不能让它在控制台里无声地失败（弹窗里就有现成的提示位）。
function openShop() { shopNotice.value = ''; shopView.value = 'buy'; showShop.value = true; loadShop().catch(error => { shopNotice.value = `商品加载失败：${error.message}（可以关掉重开一次）`; }); }
function openActivities(tab = 'chance') { activityTab.value = tab; activityOutcome.value = null; showActivities.value = true; }
function toggleContext(target, event) { giftTarget.value = target; context.value = { target, x: Math.min(event.clientX, window.innerWidth - 180), y: Math.min(event.clientY, window.innerHeight - 180) }; }
function closeMenus(event) { if (!event?.target.closest('.floating-menu') && !event?.target.closest('.user-item') && !event?.target.closest('.gift-button')) { context.value = null; showGifts.value = false; } if (!event?.target.closest('.compose-tools')) showEmoji.value = false; }
function userAction(action) {
  if (!context.value?.target) return;
  const target = context.value.target;
  // 任命/撤销房主要看「他现在是不是房主」，所以在收起菜单之前先读出来。
  const wasOwner = Boolean(target.owner);
  context.value = null;
  if (action === 'mention') input.value += `@${target.nickname} `;
  else if (action === 'private') openPrivate(target);
  else if (action === 'gift') { giftTarget.value = target; openGiftMenu('send'); }
  else if (action === 'profile') viewProfile(target);
  // 房主是「某个房间的房主」，所以必须带上当前房间号，不能只传一个人。
  else if (action === 'owner') send({ type: 'admin_action', action: wasOwner ? 'unset_owner' : 'set_owner', target_id: target.id, room_id: currentRoomId.value });
  // 全站禁言给久一点（30 分钟）；本房间禁言维持原来的 2 分钟。
  else send({ type: 'admin_action', action, target_id: target.id, minutes: action === 'mute_all' ? 30 : 2 });
}
function deleteMessage(message) { send({ type: 'admin_action', action: 'delete', message_id: message.id }); }
function roleName(role) {
  // ★ 用查表 + 兜底，不写连级三目。
  //   以前是 `role === 'admin' ? '管理员' : role === 'guest' ? '游客' : '普通用户'`，
  //   多出任何一种身份时它会**静默显示成「普通用户」**——不报错，你还以为是对的。
  //   查表兜底会显示原样字符串，漏改一眼就能看见。
  const names = { admin: '管理员', user: '普通用户', guest: '游客' };
  return names[role] || String(role || '') || '未知身份';
}
function itemTypeName(type) { return ({ avatar: '头像', badge: '徽章', gift: '礼物', pet_food: '宠物用品', pet_water: '宠物用品', pet_clean: '宠物用品' })[type] || '物品'; }
function levelTitle(level) { const titles = { 1: '初来乍到', 2: '活跃成员', 3: '聊天达人', 4: '房间红人', 5: '资深玩家', 6: '传奇人物', 7: '社区明星', 8: '终极大佬' }; return titles[Math.max(1, Number(level) || 1)] || `Lv.${level}高手`; }
function messageEffectActive(message) {
  if (!message?.text_style && !message?.text_effect) return false;
  const created = Date.parse(String(message.created_at || '').replace(' ', 'T'));
  return !Number.isFinite(created) || Date.now() - created < EFFECT_DURATION_MS;
}

function joinBannerTier(level) {
  const value = Number(level) || 1;
  if (value >= 7) return 'legendary';
  if (value >= 5) return 'gold';
  if (value >= 3) return 'silver';
  return 'normal';
}
function showJoinBanner(data) {
  const banner = { ...data, bannerKind: 'presence', bannerId: ++joinBannerSeq, tier: joinBannerTier(data.level) };
  setNoticeBanner(banner);
}
function setNoticeBanner(banner) {
  window.clearTimeout(joinBannerTimer);
  joinBanner.value = banner;
  joinBannerTimer = window.setTimeout(() => {
    if (joinBanner.value?.bannerId === banner.bannerId) joinBanner.value = null;
    joinBannerTimer = null;
  }, BANNER_DURATION_MS);
}
async function showHornBanner(data) {
  // 长消息在内容清空前滚动完一轮。
  const duration = BANNER_DURATION_MS / 1000 - 1;
  const banner = { ...data, bannerKind: 'horn', bannerId: ++joinBannerSeq, own: data.sender_id === user.value?.id, duration, scroll: false };
  setNoticeBanner(banner);
  await nextTick();
  // 量一次实际宽度：只有装不下的句子才滚动，短句居中静止更好读。
  if (joinBanner.value?.bannerId !== banner.bannerId) return;
  const track = document.querySelector('.join-horn .horn-track');
  const inner = track?.firstElementChild;
  const needScroll = Boolean(track && inner && inner.scrollWidth > track.clientWidth + 2);
  if (joinBanner.value?.bannerId === banner.bannerId) {
    joinBanner.value = { ...joinBanner.value, scroll: needScroll };
  }
}
function itemAnimationKind(message) {
  const itemId = String(message?.attachment?.item_id || '').toLowerCase();
  if (itemId.includes('flower') || itemId.includes('rose')) return 'flower';
  if (itemId.includes('kiss')) return 'kiss';
  if (itemId.includes('applause')) return 'applause';
  if (itemId.includes('firework')) return 'fireworks';
  if (itemId.includes('diamond')) return 'diamond';
  if (itemId.includes('cake')) return 'cake';
  if (itemId.includes('trophy')) return 'trophy';
  return 'gift';
}
function showItemAnimation(message) {
  itemAnimation.value = {
    kind: itemAnimationKind(message),
    icon: message?.attachment?.icon || '🎁',
    name: message?.attachment?.item_name || '物品',
    sender: message?.sender_name || '',
    target: message?.attachment?.target_name || ''
  };
  if (itemAnimationTimer) window.clearTimeout(itemAnimationTimer);
  itemAnimationTimer = window.setTimeout(() => { itemAnimation.value = null; }, 3200);
}
function activeMessageEffect(message) { return messageEffectActive(message) && ['glow', 'rainbow'].includes(message?.text_effect) ? message.text_effect : ''; }
function messageClasses(message) {
  if (!messageEffectActive(message)) return ['effect-none'];
  return [...(message.text_style || '').split(' ').filter(style => ['bold', 'blue', 'purple', 'red'].includes(style)).map(style => `font-${style}`), `effect-${['glow', 'rainbow'].includes(message.text_effect) ? message.text_effect : 'none'}`];
}
function resetTextFormat() { textFormat.value = ''; textFormatExpiresAt.value = 0; }
function toggleFormat(format) {
  if (textFormat.value === format) return resetTextFormat();
  textFormat.value = format;
  textFormatExpiresAt.value = Date.now() + EFFECT_DURATION_MS;
}
function avatar(name) { return (name || '?').slice(0, 1).toUpperCase(); }
function isMuted(onlineUser) { return onlineUser.muted_until > Date.now() / 1000; }
function inventoryCount(itemId) { return Number(user.value?.inventory?.[itemId] || 0); }
function coinsLabel(account) { return account?.coins_unlimited || account?.role === 'admin' ? '∞' : (account?.coins ?? 0); }
function isImageAvatar(value) { return typeof value === 'string' && value.startsWith('/avatars/'); }
function imageAvatar(value) { return isImageAvatar(value) ? value : ''; }
async function openAvatarPreview(card) {
  if (!isImageAvatar(card?.avatar)) return;
  avatarPreviewLoaded.value = false;
  avatarPreviewError.value = false;
  avatarPreview.value = { src: imageAvatar(card.avatar), nickname: card.nickname };
  await nextTick();
  avatarPreviewDialog.value?.showModal();
}
function closeAvatarPreview() {
  avatarPreviewDialog.value?.close();
  avatarPreview.value = null;
}
// Esc 关掉最上层的弹窗；没有弹窗时收回表情面板。
function handleKeydown(event) {
  if (event.key !== 'Escape') return;
  if (avatarPreview.value) { event.preventDefault(); closeAvatarPreview(); return; }
  // 正在编辑公告时，Esc 先退回「只读」，而不是连着弹窗一起关掉（草稿不会丢）。
  if (showAnnouncement.value && editingAnnouncement.value) { cancelEditAnnouncement(); return; }
  if (showShop.value || showProfile.value || showAnnouncement.value || showActivities.value || profileCard.value || showPetPicker.value) {
    showShop.value = false; showProfile.value = false; showAnnouncement.value = false; showActivities.value = false; profileCard.value = null; showPetPicker.value = false;
    editingAnnouncement.value = false; announcementNotice.value = '';
    return;
  }
  showEmoji.value = false;
}
// 消息时间只显示必要的部分：今天只给时分，同年给月日时分，跨年才带年份。
// 原来的完整时间戳（2026-10-02 16:28:41）每条占约 130px，同一天的日期还要重复几十遍。
function formatTime(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const stamp = Date.parse(raw.replace(' ', 'T'));
  if (!Number.isFinite(stamp)) return raw;
  const date = new Date(stamp);
  const pad = number => String(number).padStart(2, '0');
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return clock;
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return date.getFullYear() === now.getFullYear() ? `${day} ${clock}` : `${date.getFullYear()}-${day} ${clock}`;
}
function tick() {
  petNow.value = Date.now();
  if (hasPet.value && !petSpeechTimer) queuePetSpeech();
  if (!hasPet.value && petSpeechTimer) { window.clearTimeout(petSpeechTimer); petSpeechTimer = null; petBubbleText.value = ''; }
  lastClock.value = new Date().toLocaleString('zh-CN');
  if (textFormatExpiresAt.value && Date.now() >= textFormatExpiresAt.value) resetTextFormat();
}

// ★ 页面带 token 自动登录，也是一次「会话」——和 enterSession 一样必须认代次：
//   上一代任务跑完时若已经换了账号，绝不能再去 connect()；也不能在失败时清 token
//   （那会把新账号刚存好的 token 一起清掉，表现是「刚登进来又被弹回登录页」）。
async function restoreSession() {
  sessionEpoch += 1;
  const epoch = sessionEpoch;
  try {
    const me = await api('/api/me');
    if (!epochAlive(epoch)) return;
    user.value = me.user; profileAvatar.value = me.user.avatar || '🙂'; screen.value = 'chat';
    await Promise.all([loadRooms(), loadShop(), loadAvatars(), loadCustomEmojis(), loadAnnouncement()]);
    if (!epochAlive(epoch)) return;
    leaving = false; connect();
    await loadHistory();
  } catch (_) {
    if (!epochAlive(epoch)) return;
    token.value = ''; localStorage.removeItem('lan_chat_token');
  }
}
onMounted(async () => {
  document.addEventListener('click', closeMenus); document.addEventListener('keydown', handleKeydown); document.addEventListener('scroll', handleMessagesScroll, true); tick(); window.__clock = window.setInterval(tick, 1000);
  if (token.value) restoreSession();
});
onBeforeUnmount(() => { document.removeEventListener('click', closeMenus); document.removeEventListener('keydown', handleKeydown); document.removeEventListener('scroll', handleMessagesScroll, true); window.clearInterval(window.__clock); leaving = true; stopReconnect(); stopHeartbeat(); window.clearTimeout(joinBannerTimer); if (itemAnimationTimer) window.clearTimeout(itemAnimationTimer); if (petActionTimer) window.clearTimeout(petActionTimer); if (petClickTimer) window.clearTimeout(petClickTimer); if (petBubbleTimer) window.clearTimeout(petBubbleTimer); if (petSpeechTimer) window.clearTimeout(petSpeechTimer); socket.value?.close(); });
</script>

<template>
  <div v-if="screen === 'entry'" class="entry-page">
    <div class="entry-window">
      <div class="entry-titlebar"><span>✦ 局域网互动聊天室</span><span>— □ ×</span></div>
      <div class="entry-content">
        <div class="entry-logo">聊</div>
        <div class="entry-copy">
          <h1>欢迎来到局域网互动聊天室</h1><p>同一个局域网，和大家聊聊天吧！</p>
          <div class="tab-bar"><button v-for="item in [['login','登录账号'],['register','注册账号'],['guest','游客进入']]" :key="item[0]" class="tab" :class="{ active: tab === item[0] }" @click="tab = item[0]; entryError = ''">{{ item[1] }}</button></div>
          <form v-if="tab === 'login'" class="entry-form" @submit.prevent="login"><label>账号 <input v-model.trim="loginForm.username" required maxlength="24" autocomplete="username"></label><label>密码 <input v-model="loginForm.password" required type="password" maxlength="64" autocomplete="current-password"></label><button class="primary-button">进入聊天室</button></form>
          <form v-else-if="tab === 'register'" class="entry-form" @submit.prevent="register"><label>账号 <input v-model.trim="registerForm.username" required maxlength="24"></label><label>密码 <input v-model="registerForm.password" required type="password" maxlength="64"></label><label>昵称 <input v-model.trim="registerForm.nickname" required maxlength="20"></label><button class="primary-button">注册并进入</button></form>
          <form v-else class="entry-form" @submit.prevent="guest"><label>昵称 <input v-model.trim="guestName" required maxlength="20" placeholder="例如：小明"></label><button class="primary-button">游客进入大厅</button></form>
          <div class="form-error">{{ entryError }}</div>
        </div>
      </div>
      <div class="entry-status">● 服务器状态：等待进入　|　建议使用 Edge 浏览器</div>
    </div>
  </div>

  <div v-else class="chat-app">
    <header class="topbar"><div class="brand"><span class="brand-mark">聊</span><span>局域网互动聊天室</span><small>LAN INTERACTIVE CHAT</small></div><div class="top-tools"><button @click="openAnnouncement()">📢 房间公告</button><button @click="logout()">退出</button></div></header>
    <div class="toolbar"><button @click="goLobby">🏠 大厅</button><button title="只清空本机当前画面，不删除聊天记录" @click="messages = []">🧹 清空屏幕</button><button @click="soundOn = !soundOn">{{ soundOn ? '🔔 声音：开' : '🔕 声音：关' }}</button><button @click="openProfile()">⚙ 我的资料</button><button class="shop-toolbar-button" @click="openShop()">🛍 等级商城</button><button class="horn-toolbar-button" @click="openActivities('horn')">📣 大喇叭</button><button class="activity-toolbar-button" @click="openActivities('chance')">🎮 互动中心</button><span class="toolbar-sep"></span><span class="toolbar-note">欢迎来到大家的聊天室，文明聊天哦！</span><span class="connection" :class="connected ? 'online' : 'offline'">{{ connected ? '● 已连接' : '○ 连接中' }}</span></div>
    <main class="layout">
      <button v-if="!autoScrollEnabled || pendingMessageCount" class="message-scroll-button" type="button" @click="resumeAutoScroll">↓ {{ messageScrollLabel }}</button>
      <aside class="left-panel panel"><div class="panel-title">📁 房间列表</div><div class="room-list"><button v-for="room in rooms" :key="room.id" class="room" :class="{ selected: room.id === currentRoomId && !privateWith }" @click="selectRoom(room)"><span>{{ room.icon }}</span><span>{{ room.name }}</span><em>{{ room.online }}</em></button></div><div class="left-card"><b>📢 本房间公告</b><p class="announce-text">{{ announcement }}</p></div><div class="left-card online-card"><b>服务器信息</b><p>运行状态：<span class="green">{{ connected ? '正常' : '连接中' }}</span></p><p>当前房间：{{ privateWith ? `与 ${privateWith.nickname} 私聊` : currentRoom.name }}</p><p>在线人数：<strong>{{ users.length }}</strong></p></div><div class="pet-card" :class="{ 'pet-empty': !hasPet, 'pet-sad': petMood === 'sad' }">
          <div class="pet-card-title"><b>🐾 我的虚拟宠物</b><button v-if="hasPet" class="pet-change-button" @click="openPetPicker">更换</button></div>
          <template v-if="!hasPet">
            <button class="pet-house-button" @click="openPetPicker" :disabled="!petUnlocked">
              <span class="pet-house-art"><img :src="petAsset('house')" alt="小房子"></span>
              <strong>{{ petUnlocked ? '点击小房子领养' : `Lv.${petMinLevel} 解锁宠物` }}</strong>
              <small>{{ isGuest ? `注册账号并达到 Lv.${petMinLevel} 后可领养` : petUnlocked ? '选择一位像素小伙伴吧' : `当前 Lv.${user?.level || 1}，继续升级吧` }}</small>
            </button>
          </template>
          <template v-else>
            <div class="pet-scene" :class="[`pet-scene-${petMood}`, petActionAnimation]">
              <img class="pet-background" :src="'/static/pets/scene.svg'" alt="">
              <span class="pet-walker pet-click-target" role="button" tabindex="0" title="点击宠物和它互动" @click.stop="reactToPet" @keydown.enter.prevent.stop="reactToPet">
                <span v-if="petBubbleText" class="pet-bubble" aria-live="polite">{{ petBubbleText }}</span>
                <img class="pet-sprite" :class="`pet-${petState.type}`" :src="petAsset(petState.type)" :alt="petChoiceName">
                <span v-if="petReaction" class="pet-reaction" aria-hidden="true">♥</span>
              </span>
              <span class="pet-sparkle">✦</span>
            </div>
            <div class="pet-name-row"><b>{{ petChoiceName }}</b><span>{{ petMood === 'sad' ? '有点低落' : petMood === 'hungry' ? '需要照顾' : '状态不错' }}</span></div>
            <div class="pet-stats">
              <div class="pet-stat"><span>饥饿</span><div class="pet-meter"><i :class="petStatClass(petState.hunger)" :style="{ width: petStatPercent(petState.hunger) }"></i></div><em>{{ Math.round(petState.hunger) }}</em></div>
              <div class="pet-stat"><span>口渴</span><div class="pet-meter"><i :class="petStatClass(petState.thirst)" :style="{ width: petStatPercent(petState.thirst) }"></i></div><em>{{ Math.round(petState.thirst) }}</em></div>
              <div class="pet-stat"><span>清洁</span><div class="pet-meter"><i :class="petStatClass(petState.cleanliness)" :style="{ width: petStatPercent(petState.cleanliness) }"></i></div><em>{{ Math.round(petState.cleanliness) }}</em></div>
            </div>
            <div class="pet-actions"><button v-for="item in PET_ACTIONS" :key="item.action" :class="item.animation" :disabled="petBusy" @click="usePetAction(item.action)">{{ item.icon }} {{ item.label }}<small>×{{ inventoryCount(item.itemId) }}</small></button></div>
            <small class="pet-feedback" :class="{ error: petFeedback.includes('请先') || petFeedback.includes('不能') || petFeedback.includes('用完') }">{{ petFeedback || '状态会随时间慢慢下降，记得照顾它。' }}</small>
          </template>
        </div></aside>
      <section class="center-panel panel"><div class="panel-title chat-title"><span>{{ privateWith ? `💬 与 ${privatePeer.nickname} 私聊` : `${currentRoom.icon} ${currentRoom.name}` }}</span><template v-if="privateWith"><span class="title-right">只有你们两人能看到</span><button class="private-exit" title="退出私聊，返回房间聊天" @click="leavePrivate()">✕ 取消私聊</button></template><span v-else class="title-right">{{ currentRoom.description }}</span></div><div v-if="privateWith" class="private-bar" :class="{ offline: !privatePeerOnline }"><span class="private-bar-icon">{{ privatePeerOnline ? '🔒' : '⚠️' }}</span><strong>{{ privatePeer.nickname }}</strong><span class="private-bar-state">{{ privatePeerOnline ? '在线 · 私聊中' : '已离线 · 可以翻看记录，但发不出消息' }}</span><span class="private-bar-level">Lv.{{ privatePeer.level || 1 }} · {{ privatePeer.level_title || levelTitle(privatePeer.level) }}</span><button class="private-exit" @click="leavePrivate()">✕ 返回房间聊天</button></div><div class="join-banner" :class="joinBanner?.bannerKind === 'horn' ? ['join-horn', { 'horn-scroll': joinBanner.scroll }] : 'join-normal'" :style="joinBanner?.bannerKind === 'horn' ? { '--horn-speed': `${joinBanner.duration}s` } : undefined" aria-live="polite"><template v-if="joinBanner?.bannerKind === 'horn'"><span class="join-icon horn-icon">📣</span><strong>{{ joinBanner.nickname }}</strong><div class="horn-track"><span>{{ joinBanner.content }}</span></div></template><template v-else-if="joinBanner"><span class="join-icon">{{ joinBanner.action === 'leave' ? '👋' : joinBanner.tier === 'legendary' ? '🌈' : joinBanner.tier === 'gold' ? '✨' : joinBanner.tier === 'silver' ? '⭐' : '👋' }}</span><strong>{{ joinBanner.nickname }}</strong><span>{{ joinBanner.action === 'leave' ? '离开了聊天室' : '进入了聊天室' }}</span><em>Lv.{{ joinBanner.level }} · {{ joinBanner.level_title }}</em></template></div><div class="messages"><div v-if="!feed.length" class="chat-empty"><span class="chat-empty-icon">🌤️</span><b>{{ currentRoom.name }} 还没有人说话</b><small>发第一条消息，打个招呼吧。</small></div><template v-for="group in feed" :key="group.id"><div v-if="group.stream" class="activity-bundle"><button class="bundle-head" @click="toggleStream(group.id)"><span class="bundle-count">活动消息 {{ group.items.length }} 条</span><span class="bundle-summary">{{ streamSummary(group.items) }}</span><span class="bundle-caret">{{ streamOpen(group.id) ? '收起 ▴' : '展开 ▾' }}</span></button><div v-if="streamOpen(group.id)" class="bundle-body"><div v-for="item in group.items" :key="item.id" class="bundle-item"><span class="bundle-time">{{ formatTime(item.created_at) }}</span><span class="bundle-text">{{ item.content }}</span><button v-if="user?.role === 'admin'" class="message-delete" @click="deleteMessage(item)">删除</button></div></div></div><div v-else-if="group.message.kind === 'system'" class="system-message">— {{ group.message.content }} —</div><div v-else class="message-row" :class="{ gift: ['gift', 'coin_gift'].includes(group.message.kind), private: group.message.kind === 'private', file: group.message.kind === 'file', 'used-item': group.message.kind === 'use_item' }"><div class="message-avatar"><img v-if="isImageAvatar(group.message.sender_avatar)" :src="imageAvatar(group.message.sender_avatar)" :alt="`${group.message.sender_name} 的头像`"><span v-else>{{ group.message.sender_avatar || avatar(group.message.sender_name) }}</span></div><div class="message-body"><div class="message-head"><span class="message-name" :class="{ admin: group.message.sender_role === 'admin' }">{{ group.message.sender_role === 'admin' ? '👑 ' : '' }}<span v-if="group.message.sender_badge" class="badge-mark">{{ group.message.sender_badge }}</span>{{ group.message.sender_name }} <i>Lv.{{ group.message.sender_level || '' }} · {{ group.message.level_title || levelTitle(group.message.sender_level) }}</i></span><span class="message-time">{{ formatTime(group.message.created_at) }}</span></div><div v-if="group.message.kind === 'file'" class="message-text" :class="messageClasses(group.message)"><a :href="group.message.attachment?.url" target="_blank">📎 {{ group.message.attachment?.name || group.message.content }}</a><small>（{{ Math.ceil((group.message.attachment?.size || 0) / 1024) }} KB）</small></div><div v-else-if="['gift', 'coin_gift', 'use_item'].includes(group.message.kind)" class="message-text item-message" :class="messageClasses(group.message)"><ChatText :content="group.message.content" :effect="activeMessageEffect(group.message)" :custom-emojis="customEmojiMap" /></div><div v-else class="message-text" :class="messageClasses(group.message)"><ChatText :content="group.message.content" :effect="activeMessageEffect(group.message)" :custom-emojis="customEmojiMap" /><button v-if="joinTabFor(group.message)" class="activity-join-button" @click="joinActivity(group.message)">{{ joinLabelFor(group.message) }}</button></div></div><button v-if="user?.role === 'admin' && group.message.kind !== 'private'" class="message-delete" @click="deleteMessage(group.message)">删除</button></div></template></div><div class="composer"><div class="compose-tools"><button class="emoji-button" title="插入表情" aria-haspopup="true" :aria-expanded="showEmoji ? 'true' : 'false'" @click="showEmoji = !showEmoji">😀</button><div v-if="showEmoji" class="emoji-pop" role="group" aria-label="表情选择"><button v-for="emoji in emojiChoices" :key="emoji" @click="chooseEmoji(emoji)">{{ emoji }}</button><button v-for="emoji in customEmojis" :key="`custom-${emoji.name}`" class="custom-emoji-button" :title="emoji.label" @click="chooseCustomEmoji(emoji)"><img :src="emoji.src" :alt="emoji.label"></button><span v-if="!customEmojis.length" class="emoji-empty">可将 PNG/GIF/WebP 放入 emojis 文件夹</span></div><span class="tool-divider"></span><button class="effect-button" :class="{active:textFormat==='bold'}" @click="toggleFormat('bold')">粗体</button><button class="effect-button" :class="{active:textFormat==='blue'}" @click="toggleFormat('blue')">蓝字</button><button class="effect-button" :class="{active:textFormat==='purple'}" @click="toggleFormat('purple')">紫字</button><button class="effect-button" :class="{active:textFormat==='red'}" @click="toggleFormat('red')">红字</button><button class="effect-button" :class="{active:textFormat==='rainbow'}" @click="toggleFormat('rainbow')">彩虹</button><button class="effect-button" :class="{active:textFormat==='glow'}" @click="toggleFormat('glow')">闪亮</button><button class="effect-button" @click="resetTextFormat">清除效果</button><button class="gift-button" @click.stop="openGiftMenu('use')">🧰 用道具</button><button class="gift-button" @click.stop="openGiftMenu('send')">🎁 送礼物</button><span class="compose-hint">Enter 发送<span class="hint-extra"> · Shift+Enter 换行</span></span></div><textarea v-model="input" maxlength="500" :placeholder="privateWith ? `对 ${privateWith.nickname} 说点什么……` : '在这里输入聊天内容……'" @keydown.enter.exact.prevent="submit"></textarea><div class="compose-bottom"><span>{{ characterCount }} / 500 <small v-if="textStyle || textEffect">· 效果剩余 {{ formatSecondsLeft }} 秒</small></span><button class="send-button" @click="submit">发送</button></div></div></section>
      <aside class="right-panel panel"><div class="panel-title">👥 在线用户 <span>{{ users.length }}</span></div><div class="user-search"><input v-model="search" placeholder="搜索用户"><span>🔍</span></div><div class="user-list"><div v-for="onlineUser in sortedUsers" :key="onlineUser.id" class="user-item" @click.stop="toggleContext(onlineUser, $event)"><div class="avatar"><img v-if="isImageAvatar(onlineUser.avatar)" :src="imageAvatar(onlineUser.avatar)" :alt="`${onlineUser.nickname} 的头像`"><span v-else>{{ onlineUser.avatar || avatar(onlineUser.nickname) }}</span></div><div class="user-info"><b><span class="user-name">{{ onlineUser.role === 'admin' ? '👑 ' : '' }}<span v-if="onlineUser.badge" class="badge-mark">{{ onlineUser.badge }}</span>{{ onlineUser.nickname }}</span><em class="user-role" :class="{ muted: isMuted(onlineUser) }">{{ isMuted(onlineUser) ? '禁言中' : roleName(onlineUser.role) }}</em><em v-if="onlineUser.owner" class="user-owner" title="本房间房主">房主</em></b><i class="user-level">Lv.{{ onlineUser.level || 1 }} · {{ onlineUser.level_title || levelTitle(onlineUser.level) }}</i><small v-if="onlineUser.bio" class="user-bio" :title="onlineUser.bio">{{ onlineUser.bio }}</small></div><span class="online-dot"></span></div></div><div class="my-card"><div class="avatar big"><img v-if="isImageAvatar(user?.avatar)" :src="imageAvatar(user?.avatar)" :alt="`${user?.nickname || '我'} 的头像`"><span v-else>{{ user?.avatar || avatar(user?.nickname) }}</span></div><div><b><span v-if="user?.badge" class="badge-mark">{{ user.badge }}</span>{{ user?.nickname }}</b><span class="role-tag">Lv.{{ user?.level || 1 }} · {{ user?.level_title || levelTitle(user?.level) }} · {{ roleName(user?.role) }}</span><div class="level-progress"><span :style="{ width: `${Math.min(100, ((user?.exp || 0) % 50) * 2)}%` }"></span></div><small>金币 {{ coinsLabel(user) }}</small></div><button title="我的资料" @click="openProfile()">⚙</button></div></aside>
    </main>
    <footer class="statusbar"><span>© 局域网互动聊天室 2026</span><span>服务器：教师机　|　仅限本地网络使用</span><span>{{ lastClock }}</span></footer>
    <div v-if="itemAnimation" class="item-animation-overlay" aria-live="polite"><div class="item-animation-card" :class="`item-effect-${itemAnimation.kind}`"><div class="item-burst"><i v-for="n in 8" :key="n"></i></div><div class="item-animation-icon">{{ itemAnimation.icon }}</div><strong>{{ itemAnimation.name }}</strong><span v-if="itemAnimation.target">{{ itemAnimation.sender }} 送给 {{ itemAnimation.target }}</span><span v-else>{{ itemAnimation.sender }} 使用了物品</span></div></div>
    <div v-if="context" class="floating-menu context-menu" :style="{ left: `${context.x}px`, top: `${context.y}px` }"><template v-if="context.target.id !== user?.id"><button @click="userAction('private')">💬 私聊</button></template><button @click="userAction('mention')">@ 提醒他</button><template v-if="context.target.id !== user?.id"><button @click="userAction('gift')">🎁 送礼物</button></template><button @click="userAction('profile')">🔍 查看资料（{{ context.target.id === user?.id ? '免费' : '1 金币' }}）</button><template v-if="isOwner && !isAdmin && context.target.role !== 'admin' && context.target.id !== user?.id"><hr><button @click="userAction('mute')">🔇 本房间禁言 2 分钟</button><button @click="userAction('unmute')">🔊 解除本房间禁言</button></template><template v-if="isAdmin && context.target.role !== 'admin'"><hr><button @click="userAction('mute')">🔇 本房间禁言 2 分钟</button><button @click="userAction('unmute')">🔊 解除本房间禁言</button><button @click="userAction('mute_all')">🚫 全站禁言 30 分钟</button><button @click="userAction('unmute_all')">✅ 解除全站禁言</button><button @click="userAction('kick')">🚪 踢出聊天室</button><button v-if="context.target.role === 'user'" @click="userAction('owner')">{{ context.target.owner ? '🏠 撤下本房间房主' : '🏠 设为本房间房主' }}</button></template></div>
    <div v-if="showGifts" class="floating-menu gift-menu" @click.stop><div class="gift-title">{{ giftMenuMode === 'send' ? '送礼物' : '用道具' }}　{{ giftMenuMode === 'send' ? (giftTarget ? `送给 ${giftTarget.nickname}` : '先选择好友') : (giftTarget ? `自己使用或送给 ${giftTarget.nickname}` : '自己使用，也可先选好友再送人') }}</div><div v-if="giftMenuMode === 'send'" class="coin-gift-item"><span>💰 金币</span><input v-model.number="coinGiftAmount" type="number" min="1" max="10000" step="1" aria-label="赠送金币数量"><button :disabled="!giftTarget" @click="sendCoinGift">送出</button></div><div v-for="item in shopItems.filter(item => giftMenuMode === 'send' ? item.item_type === 'gift' && inventoryCount(item.id) > 0 : ['avatar','badge'].includes(item.item_type) && inventoryCount(item.id) > 0)" :key="item.id" class="gift-item"><span>{{ item.icon }} {{ item.name }} ×{{ inventoryCount(item.id) }}</span><template v-if="giftMenuMode === 'send'"><button :disabled="inventoryCount(item.id) < 1 || !giftTarget" @click="sendGiftItem(item)">送出</button></template><template v-else><button :disabled="inventoryCount(item.id) < 1 || isBadgeEquipped(item)" @click="useItem(item)">{{ isBadgeEquipped(item) ? '佩戴中' : item.item_type === 'badge' ? '佩戴' : '使用' }}</button><button class="secondary-action" :disabled="inventoryCount(item.id) < 1 || !giftTarget" @click="sendGiftItem(item)">送人</button></template></div><div v-if="!shopItems.some(item => (giftMenuMode === 'send' ? item.item_type === 'gift' : ['avatar','badge'].includes(item.item_type)) && inventoryCount(item.id) > 0)" class="gift-empty">暂无可用物品</div></div>
    <div v-if="showPetPicker" class="modal" role="dialog" aria-modal="true" aria-label="选择虚拟宠物">
      <div class="modal-box pet-picker-box">
        <div class="modal-title">🏠 选择你的像素小伙伴</div>
        <p class="pet-picker-copy">{{ petPickerHint }}</p>
        <div class="pet-choice-grid">
          <button v-for="choice in PET_CHOICES" :key="choice.type" class="pet-choice" :class="{ picked: petChoice === choice.type }" @click="petChoice = choice.type">
            <img :src="petAsset(choice.type)" :alt="choice.name">
            <b>{{ choice.name }}</b><small>{{ choice.description }}</small>
          </button>
        </div>
        <div v-if="petPickerNotice" class="pet-picker-notice">{{ petPickerNotice }}</div>
        <div class="modal-actions"><button class="primary-button" :disabled="petBusy" @click="confirmPet">{{ hasPet ? (petWillChange ? `确认更换（${PET_CHANGE_COST}金币）` : '保持当前宠物') : '确认领养' }}</button><button class="plain-button" :disabled="petBusy" @click="showPetPicker = false">取消</button></div>
      </div>
    </div>
    <div v-if="showAnnouncement" class="modal" role="dialog" aria-modal="true" aria-label="房间公告"><div class="modal-box"><div class="modal-title">📢 {{ currentRoom.name }} · 房间公告</div><p v-if="!editingAnnouncement" class="announce-text">{{ announcement }}</p><template v-else><textarea v-model="announcementDraft" class="announce-editor" maxlength="400" rows="8" placeholder="写点什么给这个房间的人看……"></textarea><small class="announce-hint">最多 200 字，保存时过一遍违禁词过滤。内容存在「公告/{{ currentRoomId }}.txt」里，老师用记事本改的是同一个文件。</small></template><div v-if="announcementNotice" class="announce-notice">{{ announcementNotice }}</div><div class="modal-actions"><template v-if="!editingAnnouncement"><button v-if="canModerate" class="primary-button" @click="startEditAnnouncement">✏️ 编辑本房间公告</button><button class="plain-button" @click="showAnnouncement = false">知道了</button></template><template v-else><button class="primary-button" @click="saveAnnouncement">保存公告</button><button class="plain-button" @click="cancelEditAnnouncement">取消</button></template></div></div></div>
    <div v-if="showProfile" class="modal" role="dialog" aria-modal="true" aria-label="我的资料">
      <div class="modal-box profile-box">
        <div class="modal-title">⚙ 我的资料<span class="profile-cost">{{ profileCostLabel }}</span></div>
        <div v-if="isGuest" class="profile-notice">游客不能修改资料，请注册账号后再来。注册后可以设置昵称、密码、真实姓名、班级和个性签名。</div>
        <template v-else>
          <div class="profile-body">
          <div class="profile-section">
            <b>🖼 头像<em>改一处 5 金币</em></b>
            <small>默认头像和教师机 avatars 文件夹里的图片都可以选；头像本身仍在「等级商城」里用金币兑换。</small>
            <div class="avatar-picker"><button v-for="item in avatarOptions" :key="item.value" :class="{ picked: profileAvatar === item.value }" @click="profileAvatar = item.value"><img v-if="isImageAvatar(item.value)" :src="item.value" :alt="`头像 ${item.value.split('/').pop()}`"><span v-else>{{ item.value }}</span></button></div>
          </div>
          <div class="profile-section">
            <b>📛 基本信息<em>每改一处 5 金币</em></b>
            <small>个性签名会显示在在线列表里（所有人都看得到）；真实姓名和班级要花 1 金币才能查看。不填也可以，会显示「未填写」。</small>
            <div class="profile-grid">
              <label>昵称<input v-model="profileForm.nickname" maxlength="20" placeholder="1~20 个字"></label>
              <label>真实姓名<input v-model="profileForm.real_name" maxlength="20" placeholder="选填，例如：张小明"></label>
              <label class="class-picker">班级<span class="class-selects"><select v-model="profileForm.grade" @change="classTouched = true"><option v-for="item in GRADE_OPTIONS" :key="item" :value="item">{{ item }}</option><option value="">取消</option></select><select v-model="profileForm.classNo" @change="classTouched = true"><option v-for="item in CLASS_OPTIONS" :key="item" :value="item">{{ item }}</option><option value="">取消</option></select></span><small>保存后显示：{{ effectiveClassName || '未填写' }}</small></label>
              <label>个性签名<input v-model="profileForm.bio" maxlength="40" placeholder="选填，最多 40 个字"></label>
            </div>
          </div>
          <div class="profile-section">
            <b>🔑 修改密码<em>改一处 5 金币</em></b>
            <small>必须先填写原密码；改完用新密码登录即可，这次登录不会掉线。</small>
            <div class="profile-grid">
              <label>原密码<input v-model="profileForm.oldPassword" type="password" maxlength="64" autocomplete="current-password"></label>
              <label>新密码<input v-model="profileForm.newPassword" type="password" maxlength="64" autocomplete="new-password" placeholder="至少 3 位"></label>
              <label>确认新密码<input v-model="profileForm.confirmPassword" type="password" maxlength="64" autocomplete="new-password"></label>
            </div>
          </div>
          <div class="profile-summary">
            <span v-if="!profileChanges.length">还没有改动任何内容</span>
            <span v-else>将修改：{{ profileChanges.join('、') }}<template v-if="profileCost">　→　扣除 {{ profileCost }} 金币</template></span>
            <span class="profile-balance">当前金币：{{ coinsLabel(user) }}</span>
          </div>
          <div v-if="profileNotice" class="profile-notice">{{ profileNotice }}</div>
          </div>
          <div class="modal-actions"><button class="primary-button" :disabled="!profileChanges.length" @click="saveProfile">保存修改</button><button class="plain-button" @click="showProfile = false">关闭</button></div>
        </template>
      </div>
    </div>
    <div v-if="profileCard" class="modal" role="dialog" aria-modal="true" :aria-label="profileCard.id === user?.id ? '我的资料（只读）' : '对方资料'">
      <div class="modal-box card-box">
        <div class="modal-title">🔍 {{ profileCard.nickname }} 的资料<span class="profile-cost">{{ profileCardNotice }}</span></div>
        <div class="card-head">
          <button v-if="isImageAvatar(profileCard.avatar)" class="avatar big avatar-preview-trigger" type="button" title="查看头像原图" :aria-label="`查看 ${profileCard.nickname} 的头像原图`" @click="openAvatarPreview(profileCard)"><img :src="imageAvatar(profileCard.avatar)" :alt="`${profileCard.nickname} 的头像`"></button>
          <div v-else class="avatar big"><span>{{ profileCard.avatar || avatar(profileCard.nickname) }}</span></div>
          <div><b><span v-if="profileCard.badge" class="badge-mark">{{ profileCard.badge }}</span>{{ profileCard.nickname }}</b><span class="role-tag">{{ roleName(profileCard.role) }} · Lv.{{ profileCard.level || 1 }} · {{ profileCard.level_title || levelTitle(profileCard.level) }}</span><small v-if="profileCard.bio" class="card-bio">{{ profileCard.bio }}</small></div>
        </div>
        <div class="card-rows">
          <div><span>真实姓名</span><b>{{ profileCard.real_name || '未填写' }}</b></div>
          <div><span>班级</span><b>{{ profileCard.class_name || '未填写' }}</b></div>
          <div><span>金币 / 魅力</span><b>{{ coinsLabel(profileCard) }} / {{ profileCard.charm || 0 }}</b></div>
          <div><span>经验</span><b>{{ profileCard.exp || 0 }}（Lv.{{ profileCard.level || 1 }}）</b></div>
          <div><span>加入时间</span><b>{{ formatTime(profileCard.created_at) || '未知' }}</b></div>
          <div><span>当前房间</span><b>{{ roomNameOf(profileCard.room_id) }}</b></div>
          <div><span>在线状态</span><b>{{ profileCard.online ? '在线' : '已离线' }}</b></div>
          <div v-if="profileCard.username"><span>账号</span><b>{{ profileCard.username }}（仅管理员可见）</b></div>
        </div>
        <button class="plain-button" @click="profileCard = null">关闭</button>
      </div>
    </div>
    <dialog v-if="avatarPreview" ref="avatarPreviewDialog" class="avatar-preview-dialog" aria-label="头像原图" @cancel.prevent="closeAvatarPreview" @click.self="closeAvatarPreview">
      <div class="avatar-preview-toolbar">
        <strong>{{ avatarPreview.nickname }} 的头像</strong>
        <a :href="avatarPreview.src" target="_blank" rel="noopener noreferrer">打开原图</a>
        <button type="button" title="关闭" aria-label="关闭头像原图" autofocus @click="closeAvatarPreview">✕</button>
      </div>
      <div class="avatar-preview-stage" aria-live="polite">
        <span v-if="avatarPreviewError" role="alert">头像加载失败</span>
        <span v-else-if="!avatarPreviewLoaded">加载中…</span>
        <img v-show="!avatarPreviewError" :src="avatarPreview.src" :alt="`${avatarPreview.nickname} 的头像原图`" @load="avatarPreviewLoaded = true" @error="avatarPreviewError = true">
      </div>
    </dialog>
    <RoomActivities v-if="showActivities" :user="user" :room="currentRoom" :token="token" :packet="activityPacket" :outcome="activityOutcome" :initial-tab="activityTab" @action="interaction" @close="showActivities=false" />
    <div v-if="showShop" class="modal" role="dialog" aria-modal="true" aria-label="等级商城">
      <div class="modal-box shop-box">
        <div class="modal-title">🛍 等级商城 <span class="shop-coins">我的金币：{{ coinsLabel(user) }}</span></div>
        <div class="shop-tabs" role="tablist" aria-label="商城页面">
          <button id="shop-buy-tab" role="tab" :aria-selected="shopView === 'buy'" aria-controls="shop-buy-panel" :disabled="recycleBusy" @click="shopView = 'buy'">商城</button>
          <button id="shop-inventory-tab" role="tab" :aria-selected="shopView === 'inventory'" aria-controls="shop-inventory-panel" :disabled="recycleBusy" @click="shopView = 'inventory'">我的物品</button>
        </div>
        <p v-if="isGuest" class="shop-notice">游客不能使用金币功能，请注册账号后再来。</p>
        <div v-if="shopView === 'buy'" id="shop-buy-panel" class="shop-table" role="tabpanel" aria-labelledby="shop-buy-tab">
          <div class="shop-row shop-head" role="row"><span>图标</span><span>物品名称</span><span>类型</span><span>说明</span><span>价格</span><span>拥有</span><span>操作</span></div>
          <template v-for="group in shopGroups" :key="group.key">
            <div class="shop-group-row">{{ group.label }}</div>
            <div v-for="item in group.items" :key="item.id" class="shop-row" role="row"><span class="shop-icon">{{ item.icon }}</span><b>{{ item.name }}</b><span class="shop-type">{{ itemTypeName(item.item_type) }}</span><small>{{ item.description }}</small><span class="shop-price">{{ item.price === 0 ? '免费' : `${item.price} 金币` }}</span><span class="shop-stock">{{ inventoryCount(item.id) }}</span><button class="primary-button shop-action" :disabled="shopActionDisabled(item)" @click="buyItem(item)">{{ shopActionLabel(item) }}</button></div>
          </template>
        </div>
        <InventoryPanel v-else id="shop-inventory-panel" role="tabpanel" aria-labelledby="shop-inventory-tab" :user="user" :items="shopItems" :busy="recycleBusy" @recycle="recycleItems" />
        <div class="shop-footer"><div v-if="shopNotice" class="shop-notice" role="status">{{ shopNotice }}</div><button class="plain-button" :disabled="recycleBusy" @click="showShop = false">关闭</button></div>
      </div>
    </div>
  </div>
</template>





