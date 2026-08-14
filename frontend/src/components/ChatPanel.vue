<template>
  <el-card class="chat-panel" body-style="padding:0;height:100%;display:flex;flex-direction:column;overflow:hidden;">
    <template #header>
      <div class="panel-header">
        <span>🤖 AI 编辑助手</span>
        <el-button size="small" @click="store.clear()">清空对话</el-button>
      </div>
    </template>

    <!-- Messages -->
    <div class="messages scrollbar-thin" ref="msgContainer">
      <div v-for="(msg, i) in store.messages" :key="i" :class="['msg', msg.role]">
        <div class="msg-avatar">{{ msg.role === 'user' ? '👤' : '🤖' }}</div>
        <div class="msg-body">
          <div v-if="msg.html" class="msg-html" v-html="msg.html"></div>
          <div v-else class="msg-text">{{ msg.content }}</div>
          <div v-for="(action, j) in msg.actions" :key="j" class="msg-action">
            <el-tag size="small" :type="action.success ? 'success' : 'danger'">{{ action.text }}</el-tag>
          </div>
        </div>
      </div>
      <div v-if="store.sending" class="msg assistant">
        <div class="msg-avatar">🤖</div>
        <div class="msg-body"><div class="typing">思考中...</div></div>
      </div>
    </div>

    <!-- Input -->
    <div class="input-area">
      <el-input
        v-model="store.inputText"
        type="textarea"
        :rows="2"
        placeholder="描述你想做的视频，如：用导入的素材做一个15秒的产品展示"
        @keydown.enter.exact.prevent="store.send()"
        resize="none"
      />
      <el-button type="primary" @click="store.send()" :loading="store.sending" :disabled="!online">
        发送
      </el-button>
    </div>
  </el-card>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'
import { useChatStore } from '../stores/chat'
import { storeToRefs } from 'pinia'

const store = useChatStore()
const msgContainer = ref(null)
const online = ref(true)

watch(() => store.messages.length, async () => {
  await nextTick()
  if (msgContainer.value) msgContainer.value.scrollTop = msgContainer.value.scrollHeight
})

watch(() => store.messages.at(-1)?.content, async () => {
  await nextTick()
  if (msgContainer.value) msgContainer.value.scrollTop = msgContainer.value.scrollHeight
})
</script>

<style scoped>
.chat-panel { background: var(--bg-card); border: 1px solid #333; }
.panel-header { display: flex; align-items: center; justify-content: space-between; font-weight: 600; }
.header-actions { display: flex; gap: 6px; }
.messages { flex: 1; overflow-y: auto; padding: 16px; }
.msg { display: flex; gap: 10px; margin-bottom: 16px; }
.msg-avatar { font-size: 24px; flex-shrink: 0; }
.msg-body { flex: 1; min-width: 0; }
.msg.user .msg-text, .msg.user .msg-html {
  background: var(--bg-input); padding: 10px 14px; border-radius: 10px; display: inline-block;
}
.msg.assistant .msg-text, .msg.assistant .msg-html {
  color: var(--text-primary); line-height: 1.6;
}
.msg-html :deep(p) { margin-bottom: 6px; }
.msg-html :deep(code) { background: #333; padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.msg-html :deep(pre) { background: #1a1a2e; padding: 10px; border-radius: 6px; overflow-x: auto; margin: 8px 0; }
.msg-action { margin-top: 4px; }
.typing { color: var(--text-secondary); font-style: italic; }
.input-area { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #333; }
.input-area :deep(.el-textarea__inner) { background: var(--bg-input); border-color: #444; color: var(--text-primary); resize: none; }
</style>
