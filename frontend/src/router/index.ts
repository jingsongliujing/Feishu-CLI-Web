import { createRouter, createWebHistory } from 'vue-router'
import { clearAuthSession, getAuthToken, verifyAuthSession } from '../lib/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: () => import('../components/Chat.vue') },
    { path: '/login', name: 'login', component: () => import('../components/Login.vue') }
  ]
})

router.beforeEach(async (to) => {
  const token = getAuthToken()
  if (to.path !== '/login' && !token) {
    return '/login'
  }
  if (!token) {
    return true
  }

  const valid = await verifyAuthSession()
  if (!valid) {
    clearAuthSession()
    return to.path === '/login' ? true : '/login'
  }
  if (to.path === '/login') {
    return '/'
  }
  return true
})

export default router
