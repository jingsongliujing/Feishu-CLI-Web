export const AUTH_TOKEN_KEY = 'feishu-cli.auth.token'
export const AUTH_ACCOUNT_KEY = 'feishu-cli.auth.account'

export interface AuthAccount {
  account: string
  name: string
}

export const getAuthToken = () => localStorage.getItem(AUTH_TOKEN_KEY) || ''

export const getAuthAccount = (): AuthAccount | null => {
  try {
    const raw = localStorage.getItem(AUTH_ACCOUNT_KEY)
    return raw ? JSON.parse(raw) : null
  } catch (_error) {
    return null
  }
}

export const setAuthSession = (token: string, account: AuthAccount) => {
  localStorage.setItem(AUTH_TOKEN_KEY, token)
  localStorage.setItem(AUTH_ACCOUNT_KEY, JSON.stringify(account))
}

export const clearAuthSession = () => {
  localStorage.removeItem(AUTH_TOKEN_KEY)
  localStorage.removeItem(AUTH_ACCOUNT_KEY)
}

export const buildAuthHeaders = (): Record<string, string> => {
  const token = getAuthToken()
  return token ? { 'X-Auth-Token': token } : {}
}

export const verifyAuthSession = async (): Promise<boolean> => {
  const token = getAuthToken()
  if (!token) return false
  try {
    const response = await fetch('/api/v1/auth/me', { headers: buildAuthHeaders() })
    if (response.status === 401) {
      clearAuthSession()
      return false
    }
    return response.ok
  } catch (_error) {
    return true
  }
}
