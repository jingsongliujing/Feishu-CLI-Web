<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { marked } from 'marked'
import WelcomeView from './WelcomeView.vue'
import { buildAuthHeaders, clearAuthSession, getAuthAccount } from '../lib/auth'

interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string
  loading?: boolean
  metadata?: Record<string, any>
}

interface HistoryItem {
  id: string
  title: string
  time: string
}

interface Skill {
  id: string
  name: string
  icon: string
  description: string
}

interface SetupStep {
  key: string
  title: string
  command?: string
  display_command?: string
  description?: string
  status?: 'pending' | 'running' | 'success' | 'failed'
}

interface ScenarioField {
  key: string
  label: string
  placeholder: string
}

interface ScenarioTemplate {
  id: string
  title: string
  category: string
  description: string
  fields: ScenarioField[]
}

interface PlanCommand {
  command: string
  reason: string
  expected: string
  write: boolean
}

interface PlanPreview {
  query: string
  summary: string
  relevant_skills: string[]
  references: string[]
  reason_for_confirmation: string
  need_confirmation: boolean
  commands: PlanCommand[]
  cli_state?: Record<string, any>
}

const router = useRouter()
const messages = ref<Message[]>([])
const inputText = ref('')
const isLoading = ref(false)
const isInitializing = ref(true)
const messagesContainer = ref<HTMLElement | null>(null)
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const shouldStickToBottom = ref(true)
let scrollFrame = 0
const sessionId = ref('')
const historyList = ref<HistoryItem[]>([])
const showSidebar = ref(false)
const currentSkill = ref('lark_cli')
const showWriteConfirm = ref(false)
const pendingWriteMessage = ref('')
const pendingWriteSkill = ref('lark_cli')
const currentAccount = ref(getAuthAccount())
const planPreview = ref<PlanPreview | null>(null)
const planLoading = ref(false)
const planError = ref('')
const pendingPlanMessage = ref('')
const pendingPlanSkill = ref('lark_cli')
const scenarioTemplates = ref<ScenarioTemplate[]>([])
const showScenarioPanel = ref(false)
const selectedScenario = ref<ScenarioTemplate | null>(null)
const scenarioValues = ref<Record<string, string>>({})

const showLarkSetup = ref(false)
const larkSetupRunning = ref(false)
const larkSetupMessage = ref('')
const larkSetupAuthUrl = ref('')
const larkSetupUserCode = ref('')
const larkSetupExpiresIn = ref('')
const larkSetupTerminal = ref('')
const larkSetupShowLog = ref(false)
const larkSetupSteps = ref<SetupStep[]>([])
const larkSetupScopes = ref<string[]>([])
const larkSetupForceAuth = ref(false)
const larkSetupTitle = computed(() => {
  if (larkSetupForceAuth.value) return '重新授权飞书账号'
  return larkSetupScopes.value.length ? '补充飞书权限' : '连接飞书账号'
})
const larkSetupActionText = computed(() => {
  if (larkSetupRunning.value) return '处理中...'
  if (larkSetupForceAuth.value) return '开始重新授权'
  return larkSetupScopes.value.length ? '补充授权' : '开始连接'
})
const larkSetupHint = computed(() => {
  if (larkSetupForceAuth.value) return '会先退出当前飞书授权，再按首次连接流程生成新的授权链接。'
  if (larkSetupScopes.value.length) return '当前操作需要额外权限，完成补充授权后可以回到刚才的请求继续执行。'
  return '首次连接会先准备当前账号的独立 CLI 环境，再生成飞书授权链接。'
})

const showModelPanel = ref(false)
const modelPresets = ref<any[]>([])
const modelPreset = ref('qwen')
const modelApiKey = ref('')
const modelBaseUrl = ref('')
const modelName = ref('')
const modelStatus = ref('')
const modelSaving = ref(false)
const currentModel = ref<Record<string, string>>({})

const skills: Skill[] = [
  {
    id: 'auto',
    name: '自动',
    icon: 'auto',
    description: '自动识别飞书需求并交给飞书 CLI 处理'
  },
  {
    id: 'lark_cli',
    name: '飞书CLI',
    icon: 'ops',
    description: '执行飞书 CLI 命令，支持消息、日历、文档、云空间、表格、多维表格等'
  }
]

const authHeaders = () => buildAuthHeaders()

const currentUserId = computed(() => currentAccount.value?.account || 'local')

const handleMessagesScroll = () => {
  const el = messagesContainer.value
  if (!el) return
  shouldStickToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 96
}

const scrollToBottom = async (force = false) => {
  if (!force && !shouldStickToBottom.value) return
  await nextTick()
  if (scrollFrame) cancelAnimationFrame(scrollFrame)
  scrollFrame = requestAnimationFrame(() => {
    const el = messagesContainer.value
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: force ? 'auto' : 'smooth' })
  })
}

const syncSessionUrl = (id: string, replace = true) => {
  const url = id ? `?session=${encodeURIComponent(id)}` : window.location.pathname
  if (replace) window.history.replaceState({}, '', url)
  else window.history.pushState({}, '', url)
}

const formatTime = (timestamp: number) => {
  if (!timestamp) return ''
  return new Date(timestamp * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const normalizeDisplayText = (value: string) => (value || '').replace(/\\n/g, '\n')

const formatContent = (content: string) => marked.parse(normalizeDisplayText(content || '')) as string

const setupStepCopy: Record<string, { title: string; description: string }> = {
  install_cli: {
    title: '安装飞书 CLI',
    description: '当前服务器还没有检测到 lark-cli，需要先安装官方 CLI。'
  },
  install_skills: {
    title: '安装飞书能力包',
    description: '安装官方飞书 CLI skills，让系统能识别消息、日程、文档、多维表格等命令。'
  },
  config_init: {
    title: '初始化飞书应用配置',
    description: '为当前 Web 账号准备独立的飞书 CLI 配置，后续授权会写入这个隔离环境。'
  },
  clear_auth: {
    title: '退出旧授权',
    description: '先清除当前账号已有的飞书登录态，避免新旧授权混用。'
  },
  auth_login: {
    title: '打开飞书授权链接',
    description: '系统会生成一个授权链接，请在浏览器里完成登录和授权。'
  }
}

const normalizeSetupStep = (step: any): SetupStep => {
  const copy = setupStepCopy[step.key] || {}
  return {
    key: step.key,
    title: copy.title || step.title,
    description: copy.description || step.description || '',
    command: step.display_command || step.command || '',
    display_command: step.display_command || step.command || '',
    status: step.status || 'pending'
  }
}

const loadScenarios = async () => {
  try {
    const response = await fetch('/api/v1/scenarios', { headers: authHeaders() })
    const payload = await response.json()
    scenarioTemplates.value = payload.data || []
  } catch (error) {
    console.error('load scenarios failed:', error)
  }
}

const selectScenarioTemplate = (template: ScenarioTemplate) => {
  selectedScenario.value = template
  scenarioValues.value = {}
  for (const field of template.fields || []) {
    scenarioValues.value[field.key] = ''
  }
}

const applyScenarioTemplate = async () => {
  if (!selectedScenario.value) return
  const response = await fetch('/api/v1/scenarios/render', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      template_id: selectedScenario.value.id,
      values: scenarioValues.value
    })
  })
  const payload = await response.json()
  inputText.value = payload.data?.message || ''
  showScenarioPanel.value = false
  await nextTick()
  adjustTextareaHeight()
}

const clearPlanPreview = () => {
  planPreview.value = null
  planError.value = ''
  pendingPlanMessage.value = ''
}

const previewPlan = async (message: string, skill = currentSkill.value) => {
  planLoading.value = true
  planError.value = ''
  planPreview.value = null
  try {
    const response = await fetch('/api/v1/chat/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: currentUserId.value,
        message,
        session_id: sessionId.value,
        skill: skill === 'auto' ? undefined : skill
      })
    })
    if (response.status === 401) {
      clearAuthSession()
      await router.replace('/login')
      return
    }
    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`)
    const payload = await response.json()
    if (payload.data?.session_id) {
      sessionId.value = payload.data.session_id
      syncSessionUrl(sessionId.value)
    }
    planPreview.value = payload.data?.plan || null
  } catch (error: any) {
    planError.value = error.message || '计划生成失败'
  } finally {
    planLoading.value = false
  }
}

const getLarkTrace = (msg: Message) => {
  const trace = msg.metadata?.execution_trace
  if (Array.isArray(trace) && trace.length) return trace
  return Array.isArray(msg.metadata?.lark_progress) ? msg.metadata?.lark_progress : []
}

const getLarkProgressSummary = (msg: Message) => {
  const trace = getLarkTrace(msg)
  if (!trace.length) return ''
  if (msg.content) return '执行过程'
  const last = trace[trace.length - 1]
  if (last.includes('规划')) return '处理中：正在规划'
  if (last.includes('修复')) return '处理中：正在修复命令'
  return '处理中'
}

const applyLarkSetupMetadata = (metadata?: Record<string, any>) => {
  if (!metadata?.setup_required) return
  showLarkSetup.value = true
  larkSetupForceAuth.value = false
  larkSetupScopes.value = Array.isArray(metadata.setup_scopes) ? metadata.setup_scopes : []
  larkSetupMessage.value = larkSetupScopes.value.length
    ? `当前账号需要补充飞书权限：${larkSetupScopes.value.join('、')}。授权成功后再重试刚才的操作。`
    : '当前账号需要连接飞书。授权和后续命令都会绑定当前登录账号。'
  if (Array.isArray(metadata.setup_steps)) {
    larkSetupSteps.value = metadata.setup_steps.map((step: any) => normalizeSetupStep({ ...step, status: 'pending' }))
  }
}

const appendLarkProgress = (msgIndex: number, content: string) => {
  const text = normalizeDisplayText(content || '').trim()
  if (!text) return
  const current = messages.value[msgIndex].metadata || {}
  const progress = Array.isArray(current.lark_progress) ? [...current.lark_progress] : []
  progress.push(text)
  messages.value[msgIndex].metadata = { ...current, lark_progress: progress.slice(-80) }
}

const applyStreamMetadata = (msgIndex: number, payload: any) => {
  const data = payload.metadata || payload.data || {}
  const current = messages.value[msgIndex].metadata || {}
  messages.value[msgIndex].metadata = { ...current, ...data }
  applyLarkSetupMetadata(messages.value[msgIndex].metadata)
}

const loadHistory = async () => {
  try {
    const response = await fetch('/api/v1/sessions?limit=50', { headers: authHeaders() })
    if (response.status === 401) {
      await router.replace('/login')
      return
    }
    const data = await response.json()
    historyList.value = (data.data || []).map((item: any) => ({
      id: item.session_id,
      title: item.title || '新会话',
      time: formatTime(item.updated_at)
    }))
  } catch (error) {
    console.error('加载会话列表失败:', error)
  }
}

const loadChat = async (id: string, silent = false) => {
  sessionId.value = id
  if (!silent) messages.value = []
  try {
    const response = await fetch(`/api/v1/sessions/${encodeURIComponent(id)}/messages`, { headers: authHeaders() })
    if (!response.ok) return
    const data = await response.json()
    const list = data.data?.messages || data.data || []
    messages.value = list.map((m: any) => ({
      id: Date.now() + Math.random(),
      role: m.role === 'assistant' ? 'assistant' : 'user',
      content: m.content || '',
      metadata: m.metadata || {}
    }))
    messages.value.forEach((msg) => applyLarkSetupMetadata(msg.metadata))
    syncSessionUrl(id, silent)
    shouldStickToBottom.value = true
    await scrollToBottom(true)
  } catch (error) {
    console.error('加载历史消息失败:', error)
  }
  if (!silent) showSidebar.value = false
}

const deleteHistory = async (event: Event, id: string) => {
  event.stopPropagation()
  await fetch(`/api/v1/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders()
  })
  if (sessionId.value === id) newChat()
  await loadHistory()
}

const newChat = () => {
  sessionId.value = ''
  messages.value = []
  shouldStickToBottom.value = true
  syncSessionUrl('', false)
  showSidebar.value = false
}

const runChatRequest = async (message: string, assistantMsgIndex: number, confirmWrite = false, skill = currentSkill.value) => {
  const response = await fetch('/api/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      user_id: currentUserId.value,
      message,
      session_id: sessionId.value,
      stream: true,
      confirm_write: confirmWrite,
      skill: skill === 'auto' ? undefined : skill
    })
  })
  if (response.status === 401) {
    clearAuthSession()
    await router.replace('/login')
    return
  }
  if (!response.ok || !response.body) throw new Error(`HTTP error! status: ${response.status}`)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let firstContentReceived = false

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() || ''

    for (const block of blocks) {
      const line = block.split('\n').find((item) => item.startsWith('data:'))
      if (!line) continue
      const data = JSON.parse(line.slice(5).trim())
      if (data.type === 'session') {
        sessionId.value = data.session_id
        syncSessionUrl(sessionId.value)
      } else if (data.type === 'progress') {
        messages.value[assistantMsgIndex].loading = false
        appendLarkProgress(assistantMsgIndex, data.content || '')
      } else if (data.type === 'content') {
        if (!firstContentReceived) {
          messages.value[assistantMsgIndex].loading = false
          firstContentReceived = true
        }
        messages.value[assistantMsgIndex].content += data.content || ''
      } else if (data.type === 'metadata') {
        applyStreamMetadata(assistantMsgIndex, data)
      } else if (data.type === 'error') {
        messages.value[assistantMsgIndex].content += data.content || '请求失败'
      } else if (data.type === 'done') {
        if (data.session_id) sessionId.value = data.session_id
      }
    }
    await scrollToBottom()
  }

  if (!messages.value[assistantMsgIndex].content) {
    messages.value[assistantMsgIndex].content = '抱歉，没有收到回复。'
  }
}

const isWriteConfirmationMessage = (content: string) => {
  return content.includes('confirm_write=true') || content.includes('确认执行')
}

const sendMessage = async () => {
  const text = inputText.value.trim()
  if (!text || isLoading.value || planLoading.value) return
  showWriteConfirm.value = false
  pendingWriteMessage.value = ''
  clearPlanPreview()
  shouldStickToBottom.value = true
  inputText.value = ''
  if (textareaRef.value) textareaRef.value.style.height = 'auto'
  pendingPlanMessage.value = text
  pendingPlanSkill.value = currentSkill.value
  await previewPlan(text, currentSkill.value)
  await scrollToBottom()
}

const openLarkReauth = () => {
  showLarkSetup.value = true
  larkSetupForceAuth.value = true
  larkSetupScopes.value = []
  larkSetupAuthUrl.value = ''
  larkSetupUserCode.value = ''
  larkSetupExpiresIn.value = ''
  larkSetupTerminal.value = ''
  larkSetupShowLog.value = false
  larkSetupMessage.value = '将为当前登录账号重新生成飞书授权链接，完成后后续命令会使用新的授权状态。'
  larkSetupSteps.value = [
    {
      key: 'clear_auth',
      title: setupStepCopy.clear_auth.title,
      description: setupStepCopy.clear_auth.description,
      command: 'lark-cli auth logout',
      status: 'pending'
    },
    {
      key: 'auth_login',
      title: setupStepCopy.auth_login.title,
      description: setupStepCopy.auth_login.description,
      command: 'lark-cli auth login --recommend --no-wait --json',
      status: 'pending'
    }
  ]
}

const executePlannedMessage = async () => {
  const text = pendingPlanMessage.value.trim()
  if (!text || isLoading.value) return
  const skill = pendingPlanSkill.value
  clearPlanPreview()

  messages.value.push({ id: Date.now(), role: 'user', content: text })
  const assistantMsgIndex = messages.value.length
  messages.value.push({ id: Date.now() + 1, role: 'assistant', content: '', loading: true, metadata: {} })
  isLoading.value = true
  await scrollToBottom()

  try {
    await runChatRequest(text, assistantMsgIndex, false, skill)
    if (isWriteConfirmationMessage(messages.value[assistantMsgIndex].content)) {
      pendingWriteMessage.value = text
      pendingWriteSkill.value = skill
      showWriteConfirm.value = true
    }
    await loadHistory()
  } catch (error: any) {
    messages.value[assistantMsgIndex].content = error.message || '网络错误，请重试'
  } finally {
    messages.value[assistantMsgIndex].loading = false
    isLoading.value = false
    await scrollToBottom()
  }
}

const confirmPendingWrite = async () => {
  if (!pendingWriteMessage.value || isLoading.value) return
  showWriteConfirm.value = false
  const assistantMsgIndex = messages.value.length
  messages.value.push({ id: Date.now(), role: 'assistant', content: '', loading: true, metadata: {} })
  isLoading.value = true
  try {
    await runChatRequest(pendingWriteMessage.value, assistantMsgIndex, true, pendingWriteSkill.value)
    await loadHistory()
  } finally {
    messages.value[assistantMsgIndex].loading = false
    isLoading.value = false
  }
}

const quickQuestion = (question: string) => {
  inputText.value = question
  sendMessage()
}

const adjustTextareaHeight = () => {
  if (!textareaRef.value) return
  textareaRef.value.style.height = 'auto'
  textareaRef.value.style.height = Math.min(textareaRef.value.scrollHeight, 120) + 'px'
}

const handleKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendMessage()
  }
}

const toggleSidebar = () => {
  showSidebar.value = !showSidebar.value
}

const selectSkill = (skillId: string) => {
  currentSkill.value = skillId
  showModelPanel.value = false
  showScenarioPanel.value = false
}

const appendLarkSetupTerminal = (chunk: string) => {
  larkSetupTerminal.value += chunk
  larkSetupTerminal.value = larkSetupTerminal.value.slice(-20000)
}

const updateLarkSetupStep = (stepKey: string, status: SetupStep['status']) => {
  larkSetupSteps.value = larkSetupSteps.value.map((step) => (step.key === stepKey ? { ...step, status } : step))
}

const startLarkSetup = async () => {
  if (larkSetupRunning.value) return
  larkSetupRunning.value = true
  larkSetupTerminal.value = ''
  larkSetupShowLog.value = false
  larkSetupAuthUrl.value = ''
  larkSetupUserCode.value = ''
  larkSetupExpiresIn.value = ''
  larkSetupMessage.value = '正在为当前账号准备飞书 CLI 初始化与授权。'
  larkSetupSteps.value = larkSetupSteps.value.map((step) => ({ ...step, status: 'pending' }))

  try {
    const response = await fetch('/api/v1/lark/setup/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: currentUserId.value, scopes: larkSetupScopes.value, force_auth: larkSetupForceAuth.value })
    })
    if (!response.ok || !response.body) throw new Error(`HTTP error! status: ${response.status}`)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        const line = block.split('\n').find((item) => item.startsWith('data:'))
        if (!line) continue
        const data = JSON.parse(line.slice(5).trim())
        if (data.type === 'status') {
          larkSetupSteps.value = (data.steps || []).map((step: any) => normalizeSetupStep({ ...step, status: 'pending' }))
        } else if (data.type === 'step_start') {
          updateLarkSetupStep(data.step?.key, 'running')
          appendLarkSetupTerminal(`\n> ${data.step?.display_command || ''}\n`)
        } else if (data.type === 'terminal') {
          appendLarkSetupTerminal(data.chunk || '')
        } else if (data.type === 'auth') {
          larkSetupAuthUrl.value = data.auth_url || larkSetupAuthUrl.value
          larkSetupUserCode.value = data.user_code ? String(data.user_code) : larkSetupUserCode.value
          larkSetupExpiresIn.value = data.expires_in ? String(data.expires_in) : larkSetupExpiresIn.value
        } else if (data.type === 'auth_wait') {
          larkSetupMessage.value = data.message || '正在等待当前账号完成飞书授权。'
        } else if (data.type === 'step_done') {
          updateLarkSetupStep(data.step_key, data.success ? 'success' : 'failed')
          if (!data.success) larkSetupShowLog.value = true
        } else if (data.type === 'done') {
          larkSetupMessage.value = data.message || '飞书授权流程已结束。'
          if (data.success) {
            showLarkSetup.value = false
            larkSetupForceAuth.value = false
          }
        }
      }
    }
  } catch (error: any) {
    larkSetupShowLog.value = true
    larkSetupMessage.value = error.message || '飞书授权流程失败，请稍后重试。'
  } finally {
    larkSetupRunning.value = false
  }
}

const refreshLarkSetup = async () => {
  const response = await fetch('/api/v1/lark/setup/status', { headers: authHeaders() })
  const payload = await response.json()
  const data = payload.data || {}
  larkSetupForceAuth.value = false
  showLarkSetup.value = !data.ready
  larkSetupMessage.value = data.ready ? '当前账号的飞书 CLI 已就绪。' : '当前账号还需要连接飞书。'
  larkSetupSteps.value = (data.steps || []).map((step: any) => normalizeSetupStep({ ...step, status: 'pending' }))
}

const loadModelConfig = async () => {
  const response = await fetch('/api/v1/models/config')
  const payload = await response.json()
  modelPresets.value = payload.data?.presets || []
  currentModel.value = payload.data?.current || {}
  modelBaseUrl.value = currentModel.value.base_url || ''
  modelName.value = currentModel.value.model || ''
}

const applyPresetDefaults = () => {
  const preset = modelPresets.value.find((item) => item.id === modelPreset.value)
  if (!preset) return
  modelBaseUrl.value = preset.base_url
  modelName.value = preset.model
}

const saveModelConfig = async (useDefaultQwenKey = false) => {
  modelSaving.value = true
  modelStatus.value = ''
  try {
    const response = await fetch('/api/v1/models/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        preset: modelPreset.value,
        api_key: modelApiKey.value,
        base_url: modelBaseUrl.value,
        model: modelName.value,
        provider: 'openai',
        use_default_qwen_key: useDefaultQwenKey
      })
    })
    const payload = await response.json()
    currentModel.value = payload.data?.current || {}
    modelStatus.value = '模型配置已保存'
    modelApiKey.value = ''
  } finally {
    modelSaving.value = false
  }
}

const handleLogout = async () => {
  await fetch('/api/v1/auth/logout', { method: 'POST', headers: authHeaders() })
  clearAuthSession()
  currentAccount.value = null
  await router.replace('/login')
}

const closeFloatingPanels = (event: MouseEvent) => {
  const target = event.target as HTMLElement
  if (!target.closest('.skill-selector')) {
    showModelPanel.value = false
    showScenarioPanel.value = false
  }
}

onMounted(async () => {
  document.addEventListener('click', closeFloatingPanels)
  await Promise.all([loadHistory(), refreshLarkSetup(), loadModelConfig(), loadScenarios()])
  const urlSessionId = new URLSearchParams(window.location.search).get('session')
  if (urlSessionId) await loadChat(urlSessionId, true)
  isInitializing.value = false
})

onUnmounted(() => {
  document.removeEventListener('click', closeFloatingPanels)
  if (scrollFrame) cancelAnimationFrame(scrollFrame)
})
</script>

<template>
  <div class="app-container">
    <div class="overlay" :class="{ show: showSidebar }" @click="toggleSidebar"></div>

    <aside class="sidebar" :class="{ show: showSidebar }">
      <div class="sidebar-top">
        <div class="sidebar-logo">
          <div class="logo-icon">飞</div>
          <span class="logo-text">飞书 CLI</span>
        </div>
        <button type="button" class="new-chat-btn" @click="newChat">
          <span>+</span>
          新建对话
        </button>
      </div>

      <div class="history-section">
        <div class="history-header">
          <span>聊天记录</span>
          <span v-if="historyList.length" class="history-count">{{ historyList.length }}</span>
        </div>
        <div class="history-list">
          <div v-if="historyList.length === 0" class="no-history">
            <p>暂无历史记录</p>
            <p class="no-history-tip">开始你的第一次飞书对话吧</p>
          </div>
          <div
            v-for="item in historyList"
            :key="item.id"
            class="history-item"
            :class="{ active: item.id === sessionId }"
            @click="loadChat(item.id)"
          >
            <div>
              <div class="history-title">{{ item.title }}</div>
              <div class="history-time">{{ item.time }}</div>
            </div>
            <button type="button" class="history-delete" @click="deleteHistory($event, item.id)" title="删除">×</button>
          </div>
        </div>
      </div>

      <div class="user-profile">
        <div class="user-avatar">{{ currentAccount?.name?.slice(0, 1) || '账' }}</div>
        <div class="user-info">
          <div class="user-name">{{ currentAccount?.name || '账号' }}</div>
          <div class="user-id">{{ currentAccount?.account || '' }}</div>
          <button type="button" class="logout-mini" @click="openLarkReauth">重新授权飞书</button>
          <button type="button" class="logout-mini" @click="handleLogout">退出</button>
        </div>
      </div>
    </aside>

    <main class="main-content">
      <button type="button" class="mobile-menu-btn" @click="toggleSidebar">☰</button>

      <div class="chat-container">
        <div ref="messagesContainer" class="chat-messages" @scroll.passive="handleMessagesScroll">
          <WelcomeView
            v-if="messages.length === 0 && !isInitializing"
            :current-skill="currentSkill"
            @quick-question="quickQuestion"
          />

          <div v-if="isInitializing" class="init-loading">
            <div class="typing-indicator"><span></span><span></span><span></span></div>
          </div>

          <template v-else>
            <div v-for="msg in messages" :key="msg.id" class="message" :class="msg.role">
              <div class="message-content">
                <template v-if="msg.loading">
                  <div class="typing-indicator"><span></span><span></span><span></span></div>
                </template>
                <template v-else>
                  <details v-if="getLarkTrace(msg).length" class="lark-progress">
                    <summary>
                      <span>{{ getLarkProgressSummary(msg) }}</span>
                      <span class="lark-progress-count">{{ getLarkTrace(msg).length }}</span>
                    </summary>
                    <div class="lark-progress-list">
                      <div v-for="(item, index) in getLarkTrace(msg)" :key="index" class="lark-progress-item">
                        <span class="lark-progress-index">{{ index + 1 }}</span>
                        <pre>{{ item }}</pre>
                      </div>
                    </div>
                  </details>
                  <div v-html="formatContent(msg.content)"></div>
                </template>
              </div>
            </div>
          </template>

          <div v-if="showWriteConfirm" class="clarify-container">
            <div class="clarify-card">
              <div class="clarify-header">确认飞书写操作</div>
              <div class="clarify-desc">{{ pendingWriteMessage }}</div>
              <div class="write-confirm-actions">
                <button type="button" class="write-confirm-btn" :disabled="isLoading" @click="confirmPendingWrite">确认执行</button>
              </div>
            </div>
          </div>

          <div v-if="planLoading || planPreview || planError" class="clarify-container">
            <div class="clarify-card plan-preview-card">
              <div class="clarify-header">执行计划预览</div>
              <div v-if="planLoading" class="clarify-desc">正在生成计划...</div>
              <div v-else-if="planError" class="clarify-desc">{{ planError }}</div>
              <template v-else-if="planPreview">
                <div class="clarify-desc">{{ planPreview.summary || pendingPlanMessage }}</div>
                <div class="plan-meta">
                  <span v-for="skill in planPreview.relevant_skills" :key="skill">{{ skill }}</span>
                </div>
                <div v-if="planPreview.commands.length" class="plan-command-list">
                  <div v-for="(item, index) in planPreview.commands" :key="index" class="plan-command-item">
                    <div class="plan-command-top">
                      <span>{{ index + 1 }}</span>
                      <strong>{{ item.write ? '写操作' : '读操作' }}</strong>
                    </div>
                    <code>{{ item.command }}</code>
                    <p>{{ item.reason }}</p>
                  </div>
                </div>
                <div v-if="planPreview.reason_for_confirmation" class="plan-warning">
                  {{ planPreview.reason_for_confirmation }}
                </div>
              </template>
              <div class="write-confirm-actions">
                <button type="button" class="write-confirm-secondary" :disabled="isLoading || planLoading" @click="clearPlanPreview">
                  取消
                </button>
                <button type="button" class="write-confirm-btn" :disabled="isLoading || planLoading || !planPreview" @click="executePlannedMessage">
                  确认执行
                </button>
              </div>
            </div>
          </div>

          <div v-if="showLarkSetup" class="clarify-container">
            <div class="clarify-card lark-setup-card">
              <div class="clarify-header">{{ larkSetupTitle }}</div>
              <div class="lark-setup-message">{{ larkSetupMessage }}</div>
              <div class="lark-setup-hintbox">{{ larkSetupHint }}</div>
              <div v-if="larkSetupSteps.length" class="lark-setup-steps">
                <div v-for="(step, index) in larkSetupSteps" :key="step.key" class="lark-setup-step" :class="step.status">
                  <div class="lark-setup-step-title">
                    <span class="lark-setup-step-index">{{ index + 1 }}</span>
                    {{ step.title }}
                  </div>
                  <div v-if="step.description" class="lark-setup-step-desc">{{ step.description }}</div>
                  <div v-if="step.command" class="lark-setup-step-command">{{ step.command }}</div>
                </div>
              </div>
              <div class="write-confirm-actions">
                <button type="button" class="write-confirm-btn" :disabled="larkSetupRunning" @click="startLarkSetup">
                  {{ larkSetupActionText }}
                </button>
              </div>
              <div v-if="larkSetupAuthUrl" class="lark-setup-auth">
                <a class="lark-setup-auth-button" :href="larkSetupAuthUrl" target="_blank" rel="noopener noreferrer">打开授权链接</a>
                <div class="lark-setup-url">{{ larkSetupAuthUrl }}</div>
                <div v-if="larkSetupUserCode" class="lark-setup-code">授权码：<strong>{{ larkSetupUserCode }}</strong></div>
                <div v-if="larkSetupExpiresIn" class="lark-setup-hint">链接有效期：{{ larkSetupExpiresIn }} 秒</div>
              </div>
              <details v-if="larkSetupTerminal" class="lark-setup-log" :open="larkSetupShowLog">
                <summary>查看执行日志</summary>
                <pre class="lark-setup-pre">{{ larkSetupTerminal }}</pre>
              </details>
            </div>
          </div>
        </div>

        <div class="chat-input-container">
          <div class="input-box">
            <div class="skill-selector">
              <div class="skill-row">
                <button
                  type="button"
                  v-for="skill in skills"
                  :key="skill.id"
                  class="skill-btn"
                  :class="{ active: currentSkill === skill.id }"
                  :title="skill.description"
                  @click.stop="selectSkill(skill.id)"
                >
                  <span>{{ skill.name }}</span>
                </button>
                <button type="button" class="skill-btn scenario-btn" @click.stop="showScenarioPanel = !showScenarioPanel">场景模板</button>
                <button type="button" class="skill-btn model-btn" @click.stop="showModelPanel = !showModelPanel">模型配置</button>
              </div>

              <div v-if="showScenarioPanel" class="scenario-popover" @click.stop>
                <div class="scenario-list">
                  <button
                    type="button"
                    v-for="template in scenarioTemplates"
                    :key="template.id"
                    :class="{ active: selectedScenario?.id === template.id }"
                    @click="selectScenarioTemplate(template)"
                  >
                    <strong>{{ template.title }}</strong>
                    <span>{{ template.description }}</span>
                  </button>
                </div>
                <div v-if="selectedScenario" class="scenario-form">
                  <label v-for="field in selectedScenario.fields" :key="field.key">
                    <span>{{ field.label }}</span>
                    <input v-model="scenarioValues[field.key]" :placeholder="field.placeholder" />
                  </label>
                  <div class="model-actions">
                    <button type="button" @click="applyScenarioTemplate">填入输入框</button>
                  </div>
                </div>
              </div>

              <div v-if="showModelPanel" class="model-popover" @click.stop>
                <label>
                  <span>服务商</span>
                  <select v-model="modelPreset" @change="applyPresetDefaults">
                    <option v-for="preset in modelPresets" :key="preset.id" :value="preset.id">{{ preset.label }}</option>
                  </select>
                </label>
                <label>
                  <span>API Key</span>
                  <input v-model="modelApiKey" type="password" placeholder="留空保留当前 Key" />
                </label>
                <label>
                  <span>Base URL</span>
                  <input v-model="modelBaseUrl" />
                </label>
                <label>
                  <span>模型名</span>
                  <input v-model="modelName" />
                </label>
                <div class="model-actions">
                  <button type="button" :disabled="modelSaving" @click="saveModelConfig(false)">保存</button>
                  <button type="button" :disabled="modelSaving" @click="saveModelConfig(true)">一键 Qwen</button>
                </div>
                <p>当前：{{ currentModel.model || '-' }}</p>
                <p v-if="modelStatus">{{ modelStatus }}</p>
              </div>
            </div>

            <div class="chat-input-wrapper">
              <textarea
                ref="textareaRef"
                v-model="inputText"
                rows="1"
                placeholder="输入飞书需求，按 Enter 发送..."
                :disabled="isLoading || planLoading"
                @input="adjustTextareaHeight"
                @keydown="handleKeydown"
              ></textarea>
              <button type="button" class="send-btn" :disabled="!inputText.trim() || isLoading || planLoading" @click="sendMessage">
                发送
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>
