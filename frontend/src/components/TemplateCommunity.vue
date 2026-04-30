<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { buildAuthHeaders, getAuthAccount } from '../lib/auth'

interface TemplateField {
  key: string
  label: string
  placeholder: string
}

interface UserTemplate {
  id: string
  template_id: number
  template_key: string
  title: string
  category: string
  description: string
  visibility: 'private' | 'public'
  owner: { account: string; name: string }
  current_version: number
  updated_at: number
  published_at?: number
  prompt: string
  fields: TemplateField[]
}

interface TemplateVersion {
  id: number
  version: number
  prompt: string
  fields: TemplateField[]
  editor: { account: string; name: string }
  change_note: string
  created_at: number
  is_current: boolean
}

const router = useRouter()
const account = getAuthAccount()
const scope = ref<'accessible' | 'mine' | 'community'>('accessible')
const templates = ref<UserTemplate[]>([])
const selected = ref<UserTemplate | null>(null)
const versions = ref<TemplateVersion[]>([])
const loading = ref(false)
const saving = ref(false)
const generating = ref(false)
const status = ref('')
const aiRequirement = ref('')
const templateSearch = ref('')

const form = ref({
  title: '',
  category: '自定义模板',
  description: '',
  visibility: 'private' as 'private' | 'public',
  prompt: '',
  change_note: '',
  fields: [] as TemplateField[]
})

const isOwner = computed(() => selected.value && selected.value.owner.account === account?.account)
const canEdit = computed(() => !selected.value || isOwner.value)
const filteredTemplates = computed(() => {
  const keyword = templateSearch.value.trim().toLowerCase()
  if (!keyword) return templates.value
  return templates.value.filter((template) => {
    return [template.title, template.category, template.description, template.owner.name, template.owner.account]
      .join(' ')
      .toLowerCase()
      .includes(keyword)
  })
})

const apiJson = async (response: Response) => {
  const payload = await response.json()
  if (!response.ok || payload.code !== 0) {
    throw new Error(payload.detail || payload.message || '请求失败')
  }
  return payload.data
}

const formatTime = (timestamp?: number) => {
  if (!timestamp) return '-'
  return new Date(timestamp * 1000).toLocaleString()
}

const loadTemplates = async () => {
  loading.value = true
  status.value = ''
  try {
    const response = await fetch(`/api/v1/templates?scope=${scope.value}`, { headers: buildAuthHeaders() })
    templates.value = await apiJson(response)
    if (selected.value) {
      const current = templates.value.find((item) => item.template_id === selected.value?.template_id)
      if (current) selectTemplate(current)
    }
  } catch (error: any) {
    status.value = error.message || '加载失败'
  } finally {
    loading.value = false
  }
}

const resetForm = () => {
  selected.value = null
  versions.value = []
  form.value = {
    title: '',
    category: '自定义模板',
    description: '',
    visibility: 'private',
    prompt: '',
    change_note: '',
    fields: [{ key: 'input', label: '输入', placeholder: '请输入内容' }]
  }
}

const selectTemplate = async (template: UserTemplate) => {
  selected.value = template
  form.value = {
    title: template.title,
    category: template.category,
    description: template.description,
    visibility: template.visibility,
    prompt: template.prompt,
    change_note: '',
    fields: template.fields.map((item) => ({ ...item }))
  }
  await loadVersions(template.template_id)
}

const loadVersions = async (templateId: number) => {
  try {
    const response = await fetch(`/api/v1/templates/${templateId}/versions`, { headers: buildAuthHeaders() })
    versions.value = await apiJson(response)
  } catch (error: any) {
    status.value = error.message || '版本加载失败'
  }
}

const addField = () => {
  form.value.fields.push({ key: '', label: '', placeholder: '' })
}

const removeField = (index: number) => {
  form.value.fields.splice(index, 1)
}

const saveTemplate = async (visibility?: 'private' | 'public') => {
  if (!canEdit.value) return
  saving.value = true
  status.value = ''
  try {
    const body = {
      ...form.value,
      visibility: visibility || form.value.visibility,
      fields: form.value.fields.filter((item) => item.key.trim() && item.label.trim())
    }
    const response = await fetch(selected.value ? `/api/v1/templates/${selected.value.template_id}` : '/api/v1/templates', {
      method: selected.value ? 'PUT' : 'POST',
      headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
    const saved = await apiJson(response)
    status.value = selected.value ? `已保存为版本 ${saved.current_version}` : '模板已创建'
    selected.value = saved
    await loadTemplates()
    await loadVersions(saved.template_id)
  } catch (error: any) {
    status.value = error.message || '保存失败'
  } finally {
    saving.value = false
  }
}

const saveAndPublish = async () => {
  await saveTemplate('public')
  if (selected.value && selected.value.visibility !== 'public') {
    await publishTemplate()
  }
}

const syncFieldsFromPrompt = () => {
  const keys = Array.from(new Set(Array.from(form.value.prompt.matchAll(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g)).map((match) => match[1])))
  if (!keys.length) {
    status.value = 'Prompt 里还没有 {{field_key}} 变量。'
    return
  }
  const existing = new Map(form.value.fields.map((item) => [item.key, item]))
  form.value.fields = keys.map((key) => existing.get(key) || {
    key,
    label: key.replace(/_/g, ' '),
    placeholder: `请输入${key.replace(/_/g, ' ')}`
  })
  status.value = `已识别 ${keys.length} 个字段。`
}

const generateTemplateDraft = async () => {
  if (!aiRequirement.value.trim()) {
    status.value = '先输入你想固定下来的流程，例如“读妙记并按负责人创建任务”。'
    return
  }
  generating.value = true
  status.value = ''
  try {
    const response = await fetch('/api/v1/templates/generate', {
      method: 'POST',
      headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement: aiRequirement.value })
    })
    const draft = await apiJson(response)
    selected.value = null
    versions.value = []
    form.value = {
      title: draft.title || '',
      category: draft.category || '自定义模板',
      description: draft.description || '',
      visibility: draft.visibility || 'private',
      prompt: draft.prompt || '',
      change_note: draft.change_note || 'AI 生成初稿',
      fields: Array.isArray(draft.fields) ? draft.fields : []
    }
    status.value = 'AI 已生成模板草稿，可以直接保存为私有模板，或调整后再保存发布。'
  } catch (error: any) {
    status.value = error.message || 'AI 生成失败'
  } finally {
    generating.value = false
  }
}

const publishTemplate = async () => {
  if (!selected.value || !isOwner.value) return
  saving.value = true
  try {
    const response = await fetch(`/api/v1/templates/${selected.value.template_id}/publish`, {
      method: 'POST',
      headers: buildAuthHeaders()
    })
    selected.value = await apiJson(response)
    form.value.visibility = 'public'
    status.value = '已发布到模板社区，所有人都可以使用'
    await loadTemplates()
  } catch (error: any) {
    status.value = error.message || '发布失败'
  } finally {
    saving.value = false
  }
}

const rollbackVersion = async (version: TemplateVersion) => {
  if (!selected.value || !isOwner.value || version.is_current) return
  saving.value = true
  try {
    const response = await fetch(`/api/v1/templates/${selected.value.template_id}/versions/${version.version}/rollback`, {
      method: 'POST',
      headers: buildAuthHeaders()
    })
    const rolledBack = await apiJson(response)
    status.value = `已基于版本 ${version.version} 创建新版本 ${rolledBack.current_version}`
    await selectTemplate(rolledBack)
    await loadTemplates()
  } catch (error: any) {
    status.value = error.message || '回滚失败'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  resetForm()
  await loadTemplates()
})
</script>

<template>
  <div class="template-page">
    <header class="template-header">
      <div>
        <p>模板社区</p>
        <h1>配置、版本化并发布常用流程模板</h1>
      </div>
      <div class="header-actions">
        <button type="button" @click="router.push('/')">返回对话</button>
        <button type="button" class="primary" @click="resetForm">新建模板</button>
      </div>
    </header>

    <main class="template-shell">
      <aside class="template-list-panel">
        <div class="tabs">
          <button :class="{ active: scope === 'accessible' }" @click="scope = 'accessible'; loadTemplates()">可用</button>
          <button :class="{ active: scope === 'mine' }" @click="scope = 'mine'; loadTemplates()">我的</button>
          <button :class="{ active: scope === 'community' }" @click="scope = 'community'; loadTemplates()">社区</button>
        </div>
        <input v-model="templateSearch" class="template-search" placeholder="搜索模板、分类或创建者" />
        <div v-if="loading" class="empty">加载中...</div>
        <div v-else-if="!filteredTemplates.length" class="empty">暂无匹配模板</div>
        <button
          v-for="template in filteredTemplates"
          v-else
          :key="template.id"
          type="button"
          class="template-card"
          :class="{ active: selected?.template_id === template.template_id }"
          @click="selectTemplate(template)"
        >
          <span class="badge" :class="template.visibility">{{ template.visibility === 'public' ? '公开' : '私有' }}</span>
          <strong>{{ template.title }}</strong>
          <small>{{ template.category }} · v{{ template.current_version }}</small>
          <p>{{ template.description || '暂无描述' }}</p>
          <small>创建者：{{ template.owner.name }}（{{ template.owner.account }}）</small>
        </button>
      </aside>

      <section class="editor-panel">
        <div class="ai-draft-panel">
          <div>
            <h2>AI 一键生成模板</h2>
            <p>描述你想固定的流程，AI 会先生成可保存、可发布的模板草稿。</p>
          </div>
          <textarea
            v-model="aiRequirement"
            rows="3"
            placeholder="例如：读一下妙记链接，提取所有 action items，按负责人创建任务，并在项目群里通知每个人"
          ></textarea>
          <button type="button" class="primary" :disabled="generating" @click="generateTemplateDraft">
            {{ generating ? '生成中...' : 'AI 生成草稿' }}
          </button>
        </div>

        <div class="editor-head">
          <div>
            <h2>{{ selected ? '编辑模板' : '新建模板' }}</h2>
            <p v-if="selected">当前版本 v{{ selected.current_version }} · 更新于 {{ formatTime(selected.updated_at) }}</p>
            <p v-else>保存后会立即进入你的可用模板列表。</p>
          </div>
          <div class="editor-actions">
            <button type="button" :disabled="!selected || !isOwner || selected.visibility === 'public' || saving" @click="publishTemplate">
              发布
            </button>
            <button type="button" :disabled="!canEdit || saving" @click="saveAndPublish">
              保存并发布
            </button>
            <button type="button" class="primary" :disabled="!canEdit || saving" @click="saveTemplate()">
              {{ saving ? '保存中...' : '保存版本' }}
            </button>
          </div>
        </div>

        <div v-if="selected && !isOwner" class="notice">这是社区模板，你可以使用它；只有创建者可以修改和发布。</div>
        <div v-if="status" class="notice">{{ status }}</div>

        <div class="form-grid">
          <label>
            <span>模板名称</span>
            <input v-model="form.title" :disabled="!canEdit" />
          </label>
          <label>
            <span>分类</span>
            <input v-model="form.category" :disabled="!canEdit" />
          </label>
          <label>
            <span>可见性</span>
            <select v-model="form.visibility" :disabled="!canEdit">
              <option value="private">私有</option>
              <option value="public">公开</option>
            </select>
          </label>
          <label>
            <span>版本说明</span>
            <input v-model="form.change_note" :disabled="!canEdit" placeholder="例如：补充会议室偏好字段" />
          </label>
        </div>

        <label class="full">
          <span>描述</span>
          <input v-model="form.description" :disabled="!canEdit" />
        </label>

        <label class="full">
          <span>Prompt 模板</span>
          <textarea v-model="form.prompt" :disabled="!canEdit" rows="8" placeholder="使用 {{field_key}} 插入字段值"></textarea>
        </label>

        <div class="field-section">
          <div class="section-title">
            <h3>字段配置</h3>
            <div class="field-actions">
              <button type="button" :disabled="!canEdit" @click="syncFieldsFromPrompt">识别字段</button>
              <button type="button" :disabled="!canEdit" @click="addField">添加字段</button>
            </div>
          </div>
          <div v-if="!form.fields.length" class="empty">这个模板没有可填字段。</div>
          <div v-for="(field, index) in form.fields" :key="index" class="field-row">
            <input v-model="field.key" :disabled="!canEdit" placeholder="字段 key，例如 group_name" />
            <input v-model="field.label" :disabled="!canEdit" placeholder="字段名称，例如 群名称" />
            <input v-model="field.placeholder" :disabled="!canEdit" placeholder="占位提示" />
            <button type="button" :disabled="!canEdit" @click="removeField(index)">删除</button>
          </div>
        </div>
      </section>

      <aside class="version-panel">
        <h2>版本历史</h2>
        <div v-if="!selected" class="empty">选择一个模板后查看版本。</div>
        <div v-else-if="!versions.length" class="empty">暂无版本。</div>
        <div v-for="version in versions" :key="version.id" class="version-card">
          <div>
            <strong>v{{ version.version }}</strong>
            <span v-if="version.is_current">当前</span>
          </div>
          <p>{{ version.change_note || '无版本说明' }}</p>
          <small>{{ version.editor.name }}（{{ version.editor.account }}）</small>
          <small>{{ formatTime(version.created_at) }}</small>
          <button type="button" :disabled="!isOwner || version.is_current || saving" @click="rollbackVersion(version)">
            回滚为新版本
          </button>
        </div>
      </aside>
    </main>
  </div>
</template>

<style scoped>
.template-page {
  min-height: 100vh;
  background: #f7f8fb;
  color: #172033;
  padding: 24px;
}

.template-header,
.template-shell,
.editor-head,
.section-title,
.header-actions,
.editor-actions,
.tabs {
  display: flex;
  gap: 12px;
}

.template-header {
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}

.template-header p {
  margin: 0 0 4px;
  color: #5d6b82;
}

.template-header h1,
.editor-panel h2,
.version-panel h2 {
  margin: 0;
}

.template-shell {
  align-items: stretch;
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr) 280px;
  gap: 16px;
}

.template-list-panel,
.editor-panel,
.version-panel {
  background: #fff;
  border: 1px solid #e5e8ef;
  border-radius: 8px;
  padding: 16px;
}

.template-list-panel,
.version-panel {
  max-height: calc(100vh - 120px);
  overflow: auto;
}

button {
  border: 1px solid #d8dee9;
  background: #fff;
  border-radius: 6px;
  padding: 8px 12px;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

button.primary {
  background: #2563eb;
  border-color: #2563eb;
  color: #fff;
}

.tabs button.active {
  background: #eef4ff;
  border-color: #8bb4ff;
}

.template-card,
.version-card {
  display: grid;
  gap: 6px;
  width: 100%;
  text-align: left;
  margin-top: 10px;
}

.template-card.active {
  border-color: #2563eb;
  background: #f4f8ff;
}

.template-card p,
.version-card p,
.editor-head p,
.ai-draft-panel p {
  margin: 0;
  color: #5d6b82;
}

.badge {
  width: fit-content;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 12px;
  background: #edf2f7;
}

.badge.public {
  background: #e7f7ed;
  color: #137333;
}

.badge.private {
  background: #fff4df;
  color: #8a5a00;
}

.editor-head {
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.ai-draft-panel {
  display: grid;
  gap: 10px;
  border: 1px solid #cfe0ff;
  background: #f6f9ff;
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 16px;
}

.ai-draft-panel h2 {
  margin: 0 0 4px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

label,
.full {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
}

label span {
  font-weight: 600;
  color: #334155;
}

input,
select,
textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #d8dee9;
  border-radius: 6px;
  padding: 9px 10px;
  font: inherit;
}

textarea {
  resize: vertical;
  line-height: 1.55;
}

.notice {
  background: #f4f8ff;
  border: 1px solid #cfe0ff;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 12px;
  color: #28518f;
}

.field-section {
  border-top: 1px solid #e5e8ef;
  padding-top: 14px;
}

.section-title {
  align-items: center;
  justify-content: space-between;
}

.section-title h3 {
  margin: 0;
}

.field-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1.5fr auto;
  gap: 8px;
  margin-top: 10px;
}

.empty {
  color: #718096;
  padding: 18px 0;
}

.version-card {
  border: 1px solid #e5e8ef;
  border-radius: 8px;
  padding: 12px;
}

.version-card div {
  display: flex;
  justify-content: space-between;
}

.version-card span {
  color: #2563eb;
  font-size: 12px;
}

@media (max-width: 1100px) {
  .template-shell {
    grid-template-columns: 1fr;
  }

  .template-list-panel,
  .version-panel {
    max-height: none;
  }
}

/* Modern console refresh */
.template-page {
  background:
    radial-gradient(circle at 8% 6%, rgba(15, 118, 110, 0.09), transparent 28%),
    linear-gradient(180deg, #f8fafc 0%, #eef3f8 100%);
  color: #172033;
  padding: 28px;
}

.template-header {
  max-width: 1480px;
  margin: 0 auto 18px;
  padding: 18px 20px;
  border: 1px solid rgba(215, 222, 232, 0.9);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow: 0 12px 32px rgba(16, 24, 40, 0.07);
  backdrop-filter: blur(16px);
}

.template-header p {
  color: #0f766e;
  font-weight: 800;
}

.template-header h1 {
  color: #111827;
  font-size: 26px;
  font-weight: 850;
}

.template-shell {
  max-width: 1480px;
  margin: 0 auto;
  grid-template-columns: minmax(280px, 320px) minmax(0, 1fr) minmax(260px, 300px);
}

.template-list-panel,
.editor-panel,
.version-panel {
  border-color: rgba(215, 222, 232, 0.92);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 12px 32px rgba(16, 24, 40, 0.07);
  backdrop-filter: blur(14px);
}

button {
  border-color: #d7dee8;
  border-radius: 10px;
  color: #344054;
  font-weight: 750;
  transition: border-color 0.18s ease, background 0.18s ease, color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
}

button:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: #99d8d0;
  color: #0f766e;
  box-shadow: 0 10px 22px rgba(16, 24, 40, 0.08);
}

button.primary {
  border-color: #0f766e;
  background: linear-gradient(135deg, #0f766e, #2563eb);
  box-shadow: 0 12px 26px rgba(15, 118, 110, 0.16);
}

.tabs {
  padding: 4px;
  border: 1px solid #e3e9f1;
  border-radius: 12px;
  background: #f8fafc;
}

.tabs button {
  flex: 1;
  border: 0;
  background: transparent;
  box-shadow: none;
}

.tabs button.active {
  background: #ffffff;
  border-color: transparent;
  color: #0f766e;
  box-shadow: 0 6px 16px rgba(16, 24, 40, 0.08);
}

.template-search {
  margin-top: 12px;
  height: 40px;
}

.template-card {
  border: 1px solid #e3e9f1;
  border-radius: 12px;
  background: #fbfcfe;
  padding: 12px;
  box-shadow: none;
}

.template-card.active {
  border-color: #7dd3c7;
  background: #f0fbf8;
  box-shadow: inset 3px 0 0 #0f766e;
}

.template-card strong {
  color: #172033;
  font-size: 15px;
}

.template-card small {
  color: #667085;
}

.badge.public {
  background: #dcfce7;
  color: #166534;
}

.badge.private {
  background: #fef3c7;
  color: #92400e;
}

.ai-draft-panel {
  grid-template-columns: minmax(220px, 0.7fr) minmax(0, 1fr) auto;
  align-items: end;
  border-color: #b8e3dd;
  background: linear-gradient(135deg, #f0fbf8, #f5f8ff);
  border-radius: 14px;
}

.ai-draft-panel textarea {
  min-height: 76px;
}

.editor-head {
  padding-bottom: 14px;
  border-bottom: 1px solid #e8edf3;
}

.editor-head h2,
.version-panel h2,
.section-title h3 {
  color: #111827;
  font-weight: 850;
}

.editor-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
}

.form-grid {
  margin-top: 16px;
}

input,
select,
textarea {
  border-color: #d7dee8;
  border-radius: 10px;
  background: #ffffff;
  color: #172033;
  transition: border-color 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}

input:focus,
select:focus,
textarea:focus {
  outline: none;
  border-color: #0f766e;
  box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12);
}

label span {
  color: #344054;
  font-size: 13px;
}

.notice {
  border-color: #bfdbfe;
  background: #eff6ff;
  color: #1d4ed8;
  border-radius: 10px;
}

.field-section {
  margin-top: 16px;
}

.field-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.field-row {
  grid-template-columns: minmax(120px, 0.8fr) minmax(140px, 0.9fr) minmax(180px, 1.3fr) auto;
  align-items: center;
  border: 1px solid #e8edf3;
  border-radius: 12px;
  background: #fbfcfe;
  padding: 10px;
}

.version-card {
  border-color: #e3e9f1;
  background: #fbfcfe;
  border-radius: 12px;
  box-shadow: none;
}

.version-card div span {
  border-radius: 999px;
  background: #e7f7f4;
  color: #0f766e;
  padding: 2px 8px;
  font-weight: 800;
}

@media (max-width: 920px) {
  .template-page {
    padding: 16px;
  }

  .template-header,
  .editor-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .ai-draft-panel,
  .form-grid,
  .field-row {
    grid-template-columns: 1fr;
  }
}

/* ByteDance / Feishu style pass */
.template-page {
  background: #f5f6f7;
  color: #1f2329;
  padding: 24px;
}

.template-header {
  border-color: #eff0f1;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(31, 35, 41, 0.06);
  backdrop-filter: none;
}

.template-header p {
  color: #1456f0;
  font-weight: 700;
}

.template-header h1 {
  color: #1f2329;
  font-size: 24px;
  font-weight: 800;
}

.template-shell {
  gap: 14px;
  grid-template-columns: minmax(280px, 312px) minmax(0, 1fr) minmax(260px, 292px);
}

.template-list-panel,
.editor-panel,
.version-panel {
  border-color: #eff0f1;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(31, 35, 41, 0.06);
  backdrop-filter: none;
}

button {
  border-color: #dee0e3;
  border-radius: 8px;
  color: #1f2329;
  font-weight: 650;
  box-shadow: none;
}

button:hover:not(:disabled) {
  transform: none;
  border-color: #d0dcff;
  background: #f8faff;
  color: #1456f0;
  box-shadow: none;
}

button.primary {
  border-color: #1456f0;
  background: #1456f0;
  color: #ffffff;
  box-shadow: none;
}

button.primary:hover:not(:disabled) {
  border-color: #3370ff;
  background: #3370ff;
  color: #ffffff;
}

.tabs {
  border-color: #eff0f1;
  border-radius: 10px;
  background: #f7f8fa;
}

.tabs button {
  color: #646a73;
}

.tabs button.active {
  color: #1456f0;
  box-shadow: 0 1px 2px rgba(31, 35, 41, 0.08);
}

.template-search {
  border-color: #dee0e3;
  border-radius: 8px;
}

.template-card {
  border-color: #eff0f1;
  border-radius: 10px;
  background: #ffffff;
}

.template-card:hover {
  border-color: #d0dcff;
  background: #f8faff;
}

.template-card.active {
  border-color: #d0dcff;
  background: #f8faff;
  box-shadow: inset 3px 0 0 #1456f0;
}

.template-card strong,
.editor-head h2,
.version-panel h2,
.section-title h3 {
  color: #1f2329;
}

.template-card small,
.template-card p,
.version-card p,
.editor-head p,
.ai-draft-panel p {
  color: #646a73;
}

.badge.public {
  background: #e8f7ef;
  color: #168a49;
}

.badge.private {
  background: #fff3d6;
  color: #9b6500;
}

.ai-draft-panel {
  border-color: #d0dcff;
  background: #f8faff;
  border-radius: 12px;
}

.ai-draft-panel h2 {
  color: #1f2329;
}

.editor-head {
  border-bottom-color: #eff0f1;
}

input,
select,
textarea {
  border-color: #dee0e3;
  border-radius: 8px;
  color: #1f2329;
}

input:focus,
select:focus,
textarea:focus {
  border-color: #3370ff;
  box-shadow: 0 0 0 3px rgba(51, 112, 255, 0.12);
}

label span {
  color: #1f2329;
  font-weight: 650;
}

.notice {
  border-color: #d0dcff;
  background: #f8faff;
  color: #1456f0;
}

.field-row,
.version-card {
  border-color: #eff0f1;
  border-radius: 10px;
  background: #ffffff;
}

.version-card div span {
  background: #eaf0ff;
  color: #1456f0;
}
</style>
