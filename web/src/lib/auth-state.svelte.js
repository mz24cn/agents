export const authDialog = $state({
  open: false,
  password: '',
  error: '',
  submitting: false,
})

let pendingLogin = null
let pendingResolve = null
let pendingReject = null

export function ensureAuthenticated() {
  if (pendingLogin) return pendingLogin
  authDialog.open = true
  authDialog.password = ''
  authDialog.error = ''
  authDialog.submitting = false
  pendingLogin = new Promise((resolve, reject) => {
    pendingResolve = resolve
    pendingReject = reject
  })
  return pendingLogin
}

function finishLogin(ok) {
  const resolve = pendingResolve
  const reject = pendingReject
  pendingLogin = null
  pendingResolve = null
  pendingReject = null
  authDialog.submitting = false
  if (ok) {
    authDialog.open = false
    authDialog.password = ''
    authDialog.error = ''
    if (resolve) resolve(true)
  } else {
    const err = new Error('Authentication cancelled')
    err.name = 'AuthCancelled'
    if (reject) reject(err)
  }
}

export async function submitAuthLogin() {
  if (authDialog.submitting) return
  authDialog.submitting = true
  authDialog.error = ''
  try {
    const res = await fetch('/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: authDialog.password }),
    })
    let data = null
    try { data = await res.json() } catch { data = null }
    if (!res.ok) {
      authDialog.error = data?.message || data?.error || 'Invalid password'
      authDialog.submitting = false
      return
    }
    finishLogin(true)
  } catch (err) {
    authDialog.error = err?.message || 'Login failed'
    authDialog.submitting = false
  }
}

export function cancelAuthLogin() {
  finishLogin(false)
}
