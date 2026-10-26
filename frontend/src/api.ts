import axios from 'axios'
import { useAuthStore } from './stores/auth'
import router from './router'

const client = axios.create({ baseURL: '/', timeout: 30000 })

client.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

client.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      const auth = useAuthStore()
      auth.logout()
      const current = router.currentRoute.value
      // 会话过期时也保留当前来源路径,登录后回到同一位置;登录页本身不递归携带
      if (current.name !== 'login') {
        router.push({ name: 'login', query: { redirect: current.fullPath } })
      }
    }
    return Promise.reject(error)
  },
)

export default client

export function errMsg(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const detail = e.response?.data?.detail
    if (typeof detail === 'string') return detail
    return e.message
  }
  return String(e)
}
