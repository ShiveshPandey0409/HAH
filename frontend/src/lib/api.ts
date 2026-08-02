import type {
  AuthResponse,
  Claim,
  EligibleBounty,
  ProofUpload,
  SocialProfile,
  Submission,
  SubmissionProofInput,
  Task,
  TaskInput,
  User,
  WebhookEndpoint,
  WebhookEvent,
  WorkClaim,
} from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const TOKEN_KEY = 'hah_access_token'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function detailMessage(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) return String(item.msg)
        return String(item)
      })
      .join('. ')
  }
  return 'Something went wrong. Try again.'
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch {
    throw new ApiError('Cannot reach the HAH API. Check that the backend is running.', 0)
  }

  if (!response.ok) {
    let message = response.statusText
    try {
      const body = (await response.json()) as { detail?: unknown }
      message = detailMessage(body.detail)
    } catch {
      // The status text is the safest fallback for non-JSON errors.
    }
    throw new ApiError(message || 'Request failed', response.status)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function requestBlob(path: string): Promise<Blob> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = new Headers()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { headers })
  if (!response.ok) throw new ApiError(response.statusText || 'Could not load proof', response.status)
  return response.blob()
}

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export const api = {
  signup: (body: {
    email: string
    password: string
    display_name: string
    can_create_tasks: boolean
    can_work_tasks: boolean
    bio?: string | null
  }) => request<AuthResponse>('/v1/auth/signup', { method: 'POST', body: JSON.stringify(body) }),
  login: (email: string, password: string) =>
    request<AuthResponse>('/v1/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  me: () => request<User>('/v1/auth/me'),
  logout: () => request<void>('/v1/auth/logout', { method: 'POST' }),
  forgotPassword: (email: string) =>
    request<void>('/v1/auth/forgot-password', { method: 'POST', body: JSON.stringify({ email }) }),
  resetPassword: (token: string, newPassword: string) =>
    request<void>('/v1/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify({ token, new_password: newPassword }),
    }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<void>('/v1/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
  listTasks: () => request<Task[]>('/v1/tasks'),
  getTask: (id: string) => request<Task>(`/v1/tasks/${id}`),
  createTask: (body: TaskInput) => request<Task>('/v1/tasks', { method: 'POST', body: JSON.stringify(body) }),
  replaceTask: (id: string, body: TaskInput) =>
    request<Task>(`/v1/tasks/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  openTask: (id: string) => request<Task>(`/v1/tasks/${id}/open`, { method: 'POST' }),
  deleteTask: (id: string) => request<void>(`/v1/tasks/${id}`, { method: 'DELETE' }),
  listProfiles: (userId: string) => request<SocialProfile[]>(`/v1/users/${userId}/social-profiles`),
  putProfile: (userId: string, platform: string, profileUrl: string) =>
    request<SocialProfile>(`/v1/users/${userId}/social-profiles/${platform}`, {
      method: 'PUT',
      body: JSON.stringify({ profile_url: profileUrl }),
    }),
  listBounties: (userId: string) => request<EligibleBounty[]>(`/v1/freelancers/${userId}/bounties`),
  claimBounty: (bountyId: string, socialAccountId: string) =>
    request<Claim>(`/v1/bounties/${bountyId}/claims`, {
      method: 'POST',
      body: JSON.stringify({ social_account_id: socialAccountId }),
    }),
  listClaims: (userId: string) => request<WorkClaim[]>(`/v1/freelancers/${userId}/claims`),
  uploadProof: (claimId: string, proofType: 'screenshot' | 'image', file: File) => {
    const body = new FormData()
    body.set('proof_type', proofType)
    body.set('file', file)
    return request<ProofUpload>(`/v1/claims/${claimId}/proof-uploads`, { method: 'POST', body })
  },
  submitProof: (claimId: string, proofs: SubmissionProofInput[]) =>
    request<Submission>(`/v1/claims/${claimId}/submissions`, {
      method: 'POST',
      body: JSON.stringify({ proofs }),
    }),
  getSubmission: (submissionId: string) => request<Submission>(`/v1/submissions/${submissionId}`),
  listTaskSubmissions: (taskId: string) => request<Submission[]>(`/v1/tasks/${taskId}/submissions`),
  getProofContent: (contentUrl: string) => requestBlob(contentUrl),
  verifySubmission: (
    submissionId: string,
    result: 'passed' | 'failed' | 'review_required',
    failureReason?: string,
  ) =>
    request<Submission>(`/v1/submissions/${submissionId}/verification`, {
      method: 'POST',
      body: JSON.stringify({ result, checks: {}, failure_reason: failureReason || null }),
    }),
  getWebhook: (userId: string) => request<WebhookEndpoint>(`/v1/users/${userId}/webhook`),
  putWebhook: (userId: string, url: string, subscribedEvents: WebhookEvent[]) =>
    request<WebhookEndpoint>(`/v1/users/${userId}/webhook`, {
      method: 'PUT',
      body: JSON.stringify({ url, subscribed_events: subscribedEvents }),
    }),
}
