/**
 * 对话消息按「桶」存放：一个桶就是一段对话的消息数组，键是 conversation_id。
 *
 * 新建对话还没有服务端 id，先挂在一个临时桶（`draft:`）上，等 `conversation_started`
 * 带回真实 id 再改挂过去。这几条规则单独放在这里，是因为它们曾经写错过一次：
 * 改挂时把新消息插到了已有历史前面，用户看到自己刚发的话跑到了旧消息上面。
 */

/** 新建但服务端还没返回 conversation_id 时的临时桶名。 */
export const DRAFT_THREAD_PREFIX = "draft:";

export function isDraftThreadKey(key) {
  return typeof key === "string" && key.startsWith(DRAFT_THREAD_PREFIX);
}

export function nextDraftThreadKey() {
  return `${DRAFT_THREAD_PREFIX}${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** 桶名对应的服务端会话 id：临时桶还没有 id，返回 null。 */
export function conversationIdOfThreadKey(key) {
  if (!key || isDraftThreadKey(key)) return null;
  return key;
}

/**
 * 这一轮该写进哪个桶：已经在某段对话里就接着写那份对话，只有新对话才开临时桶。
 *
 * 早先无论有没有会话都先开临时桶、等事件回来再改挂，结果是「继续对话」这条路上
 * 新消息要等一次往返才落到正确位置，期间显示在最上面。
 */
export function threadKeyForSubmit(activeThreadKey) {
  const conversationId = conversationIdOfThreadKey(activeThreadKey);
  if (conversationId) return conversationId;
  // 已经点了「新建对话」有一个还没用上的临时桶，接着用它，别再开一个空的
  if (isDraftThreadKey(activeThreadKey)) return activeThreadKey;
  return nextDraftThreadKey();
}

/** 新的一轮永远接在末尾：消息是有时序的，桶里的先后就是对话的先后。 */
export function appendTurn(messages, ...incoming) {
  return [...(Array.isArray(messages) ? messages : []), ...incoming];
}

/**
 * 把临时桶改挂到真实会话 id 下。
 *
 * 临时桶里是刚发出的那一轮，比目标桶里已有的消息都新，所以是**追加**在后面。
 * 这里写成追加而不是拼接在前面：拼接会让新消息顶到历史上面。
 */
export function moveThreadBucket(threads, fromKey, toKey) {
  if (fromKey === toKey || !threads[fromKey]) return threads;
  const next = { ...threads };
  const moved = next[fromKey];
  delete next[fromKey];
  next[toKey] = appendTurn(next[toKey], ...moved);
  return next;
}
