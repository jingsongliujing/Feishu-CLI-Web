<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { setAuthSession } from '../lib/auth'

const router = useRouter()
const account = ref('')
const password = ref('000000')
const errorMessage = ref('')
const isSubmitting = ref(false)

const parseApiJson = async (response: Response) => {
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) {
    throw new Error('后端没有返回 JSON，请确认前端代理已指向正确的后端服务。')
  }
  return response.json()
}

const handleLogin = async () => {
  if (!account.value.trim() || !password.value) {
    errorMessage.value = '请输入账号和密码'
    return
  }

  isSubmitting.value = true
  errorMessage.value = ''

  try {
    const response = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        account: account.value.trim(),
        password: password.value
      })
    })
    const data = await parseApiJson(response)
    if (!response.ok || data.code !== 0) {
      throw new Error(data.detail || data.message || '登录失败')
    }
    setAuthSession(data.data.token, data.data.account)
    router.replace('/')
  } catch (error: any) {
    errorMessage.value = error.message || '登录失败'
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-title">账号登录</div>
      <div class="login-subtitle">登录后再进行飞书 CLI 初始化与授权；每个账号拥有独立的会话记录和 CLI 授权环境。</div>

      <form class="login-form" @submit.prevent="handleLogin">
        <label class="login-label">
          <span>账号</span>
          <input v-model="account" type="text" placeholder="例如 admin123 或 admin" autocomplete="username" />
        </label>

        <label class="login-label">
          <span>密码</span>
          <input v-model="password" type="password" placeholder="请输入密码" autocomplete="current-password" />
        </label>

        <div v-if="errorMessage" class="login-error">{{ errorMessage }}</div>

        <button class="login-button" type="submit" :disabled="isSubmitting">
          {{ isSubmitting ? '登录中...' : '登录' }}
        </button>
      </form>

      <div class="login-demo">
        <div>初始化账号：</div>
        <div>admin123 / 000000</div>
        <div>admin / 000000</div>
      </div>
    </div>
  </div>
</template>
