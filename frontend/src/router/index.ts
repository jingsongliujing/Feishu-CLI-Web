import { createRouter, createWebHistory } from 'vue-router'
import { getAuthToken } from '../lib/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: () => import('../components/Chat.vue') },
    { path: '/login', name: 'login', component: () => import('../components/Login.vue') }
  ]
})

router.beforeEach((to) => {
  const token = getAuthToken()
  if (to.path !== '/login' && !token) {
    return '/login'
  }
  if (to.path === '/login' && token) {
    return '/'
  }
  return true
})

export default router
