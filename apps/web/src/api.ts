export type DiffSegment = {
  type: 'unchanged' | 'changed' | 'added' | 'removed'
  text: string
  original?: string
  originalFrom: number
  workingFrom: number
}

export type RewriteSession = {
  id: string
  documentId: string
  sourceRevisionId: string
  comparisonDocumentId: string
  comparisonRevisionId: string
  workingRevisionId: string
  originalText: string
  content: string
  diffSegments: DiffSegment[]
}

export type AgentMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  type: string
  metadata: Record<string, any>
}

export class AgentStreamError extends Error {
  category: string
  userMessageId?: string
  modelFailure: boolean

  constructor(message: string, category: string, userMessageId?: string, modelFailure = false) {
    super(message)
    this.name = 'AgentStreamError'
    this.category = category
    this.userMessageId = userMessageId
    this.modelFailure = modelFailure
  }
}

export type AgentThreadSummary = {
  id: string
  title: string
  preview: string
  messageCount: number
  createdAt: string
  lastActivityAt: string
}

export type Asset = {
  id: string
  fileName: string
  url: string
  prompt: string
  sceneNumber: number
  status: 'needs_review' | 'approved' | 'rejected'
  isDemo: boolean
}

export type Job = {
  id: string
  type: string
  status: string
  progress: number
  message: string
  result: Record<string, any>
  error?: string | null
}

export type RewritePreset = {
  id: string
  promptId: string
  name: string
  description: string
}

export type AgentPromptKind = 'rewrite' | 'storyboard' | 'image_style'

export type AgentPromptVersion = {
  versionId: string
  version: number
  content: string
}

export type AgentPromptPreset = {
  promptId: string
  kind: AgentPromptKind
  name: string
  isDefault: boolean
  versions: AgentPromptVersion[]
}

export type AgentPromptVersionSelection = {
  promptId: string
  versionId: string
  kind: AgentPromptKind
  name: string
  version: number
  content: string
  isDefault: boolean
}

export const DEFAULT_REWRITE_PROMPT_VERSION_ID = 'agent_rewrite_default_v1'
export const DEFAULT_STORYBOARD_PROMPT_VERSION_ID = 'agent_storyboard_default_v1'
export const DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID = 'agent_image_style_default_v1'

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { ...init, credentials: 'include' })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload.detail || payload.error?.message || `请求失败 (${response.status})`
    throw new Error(detail)
  }
  return payload as T
}

export const api = {
  auth: {
    me: () => request<{ user: { id: string; email: string } }>('/auth/me'),
    login: (email: string, password: string) => request<{ user: { id: string; email: string } }>('/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) }),
    register: (email: string, password: string) => request<{ user: { id: string; email: string } }>('/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) }),
    logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  },
  workspaces: () => request<Array<{ id: string; name: string }>>('/workspaces'),
  createWorkspace: (name: string) => request<{ id: string; name: string }>('/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }),
  uploadDocument: (workspaceId: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<{ documentId: string; revisionId: string; fileName: string; content: string; characterCount: number }>(`/workspaces/${workspaceId}/documents`, { method: 'POST', body })
  },
  uploadText: (workspaceId: string, name: string, content: string) => api.uploadDocument(workspaceId, new File([content], name, { type: 'text/plain' })),
  getDocument: (documentId: string) => request<{ id: string; fileName: string; revisionId: string; content: string }>(`/documents/${documentId}`),
  saveDocumentRevision: (documentId: string, baseRevisionId: string, content: string, rewriteSessionId?: string) => request<{ documentId: string; revisionId: string; revisionNumber: number; fileName: string; content: string; session: RewriteSession | null }>(`/documents/${documentId}/revisions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ baseRevisionId, content, rewriteSessionId }) }),
  createRewriteSession: (workspaceId: string, documentId: string, sourceRevisionId: string) => request<RewriteSession>(`/workspaces/${workspaceId}/rewrite-sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ documentId, sourceRevisionId }) }),
  getRewriteSession: (sessionId: string) => request<RewriteSession>(`/rewrite-sessions/${sessionId}`),
  updateComparisonBaseline: (sessionId: string, documentId: string, revisionId: string) => request<RewriteSession>(`/rewrite-sessions/${sessionId}/comparison-baseline`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ documentId, revisionId }) }),
  updateRewriteSession: (sessionId: string, baseWorkingRevisionId: string, content: string) => request<RewriteSession>(`/rewrite-sessions/${sessionId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ baseWorkingRevisionId, content }) }),
  rewrite: (session: RewriteSession, body: { scope: Record<string, unknown>; modelId: string; styleId: string; prompt: string; strength: number; candidateCount: number }) => request<{ runId: string; workingRevisionId: string; content: string; candidates: string[]; selectedIndex: number; mode: string; diffSegments: DiffSegment[] }>(`/rewrite-sessions/${session.id}/rewrite-runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ baseWorkingRevisionId: session.workingRevisionId, ...body }) }),
  selectCandidate: (runId: string, candidateIndex: number) => request<{ workingRevisionId: string; content: string; diffSegments: DiffSegment[] }>(`/rewrite-runs/${runId}/select`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ candidateIndex }) }),
  models: () => request<{ mode: string; models: Array<{ id: string; name: string; provider: string; available: boolean }> }>('/models'),
  presets: () => request<RewritePreset[]>('/rewrite-presets'),
  agentPrompts: () => request<Record<AgentPromptKind, AgentPromptPreset[]>>('/agent/prompts'),
  createAgentPrompt: (kind: AgentPromptKind, name: string, content: string) => request<AgentPromptVersionSelection>('/agent/prompts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, name, content }) }),
  createAgentPromptVersion: (promptId: string, content: string) => request<AgentPromptVersionSelection>(`/agent/prompts/${promptId}/versions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) }),
  updateAgentPromptVersion: (promptId: string, versionId: string, content: string) => request<AgentPromptVersionSelection>(`/agent/prompts/${promptId}/versions/${versionId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) }),
  createThread: (workspaceId: string) => request<{ id: string; title: string }>('/agent/threads', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspaceId }) }),
  agentThreads: (workspaceId: string, options: { q?: string; offset?: number; limit?: number } = {}) => {
    const params = new URLSearchParams()
    if (options.q) params.set('q', options.q)
    if (options.offset) params.set('offset', String(options.offset))
    if (options.limit) params.set('limit', String(options.limit))
    return request<{ items: AgentThreadSummary[]; hasMore: boolean; nextOffset: number }>(`/workspaces/${workspaceId}/agent/threads?${params.toString()}`)
  },
  messages: (threadId: string) => request<AgentMessage[]>(`/agent/threads/${threadId}/messages`),
  sendMessage: async (
    threadId: string,
    body: { message: string; modelId: string; requestId: string; contextText?: string; subtitleFileId?: string; rewritePromptVersionId: string; storyboardPromptVersionId: string; imageStylePromptVersionId: string; retryMessageId?: string },
    onDelta: (delta: string) => void,
  ): Promise<{ message: AgentMessage }> => {
    const response = await fetch(`/api/v1/agent/threads/${threadId}/messages/stream`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || `请求失败 (${response.status})`)
    }
    if (!response.body) throw new Error('浏览器不支持流式响应')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let completed: AgentMessage | null = null
    let streamError = ''
    let streamErrorCategory = 'request'
    let modelFailure = false
    let userMessageId: string | undefined

    function consumeFrame(frame: string) {
      const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
      if (!data || data === '[DONE]') return
      const event = JSON.parse(data)
      if (event.type === 'message.started') userMessageId = event.userMessageId
      if (event.type === 'message.delta') onDelta(event.delta)
      if (event.type === 'message.completed') completed = event.message
      if (event.type === 'message.error') {
        streamError = event.message
        streamErrorCategory = event.category || 'request'
        modelFailure = Boolean(event.modelFailure)
        userMessageId = event.userMessageId || userMessageId
      }
    }

    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() ?? ''
      for (const frame of frames) consumeFrame(frame)
      if (done) break
    }
    if (buffer.trim()) consumeFrame(buffer)
    if (streamError) throw new AgentStreamError(streamError, streamErrorCategory, userMessageId, modelFailure)
    if (!completed) throw new Error('流式响应提前结束，请查看后端日志')
    return { message: completed }
  },
  resolveAction: (threadId: string, actionId: string, body: { decision: 'approve' | 'reject' | 'retry_preview'; selectedPreviewIndex?: number; stylePrompt?: string; imageStylePromptVersionId?: string }) => request<{ status: string; jobId?: string; message?: AgentMessage }>(`/agent/threads/${threadId}/actions/${actionId}/resolve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  assets: (workspaceId: string) => request<Asset[]>(`/workspaces/${workspaceId}/assets`),
  downloadAssets: async (workspaceId: string, assetIds: string[]) => {
    const response = await fetch(`/api/v1/workspaces/${workspaceId}/assets/download`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assetIds }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || `下载失败 (${response.status})`)
    }
    const disposition = response.headers.get('Content-Disposition') || ''
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
    const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1]
    return {
      blob: await response.blob(),
      fileName: encodedName ? decodeURIComponent(encodedName) : plainName || '素材下载',
    }
  },
  reviewAsset: (assetId: string, decision: 'approve' | 'reject') => request<{ id: string; status: Asset['status'] }>(`/assets/${assetId}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision }) }),
  uploadSubtitle: (workspaceId: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<{ id: string; fileName: string; cueCount: number }>(`/workspaces/${workspaceId}/subtitle-files`, { method: 'POST', body })
  },
  uploadSubtitleBundle: (workspaceId: string, files: File[]) => {
    const body = new FormData()
    files.forEach((file) => body.append('files', file, file.name))
    return request<{ id: string; fileName: string; audioFileName: string; cueCount: number }>(`/workspaces/${workspaceId}/subtitle-bundles`, { method: 'POST', body })
  },
  jobs: (workspaceId: string) => request<Job[]>(`/workspaces/${workspaceId}/jobs`),
  cancelJob: (jobId: string) => request<{ id: string; status: string; message: string }>(`/jobs/${jobId}/cancel`, { method: 'POST' }),
  retryJob: (jobId: string) => request<{ id: string; status: string; message: string }>(`/jobs/${jobId}/retry`, { method: 'POST' }),
}

