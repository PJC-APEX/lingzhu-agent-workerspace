import { JianyingDownload } from './JianyingDownload'
import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, ArrowDownToLine, ArrowLeftRight, Bot, Check, CheckCheck, ChevronDown, CircleHelp, ExternalLink,
  Clock3, Copy, Download, FileText, Film, ImagePlus, LoaderCircle, MessageCircle, MoreHorizontal,
  Paperclip, PanelLeftClose, Plus, RefreshCw, Search, Send, Settings2, ShieldCheck, Sparkles, LogOut,
  Maximize2, Minimize2, Pencil, Trash2, Upload, WandSparkles, X,
} from 'lucide-react'
import {
  api, AgentStreamError, AgentMessage, AgentPromptKind, AgentPromptPreset, AgentPromptVersionSelection, AgentThreadSummary, Asset,
  DEFAULT_REWRITE_PROMPT_VERSION_ID, DEFAULT_STORYBOARD_PROMPT_VERSION_ID, DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID,
  DiffSegment, Job, RewritePreset, RewriteSession,
} from './api'

type DocumentRef = { id: string; revisionId: string; fileName: string; content: string }
type Selection = { from: number; to: number; text: string }
type AgentPromptOption = AgentPromptVersionSelection
type AgentPromptEditorState = { kind: AgentPromptKind; promptId?: string; versionId?: string }
type AgentThreadGroup = { label: string; items: AgentThreadSummary[] }
type PendingChatRetry = {
  message: string
  contextText?: string
  subtitleFileId?: string
  rewritePromptVersionId: string
  storyboardPromptVersionId: string
  imageStylePromptVersionId: string
  userMessageId: string
}
type PendingRewriteRetry = {
  sessionId: string
  workingRevisionId: string
  scope: 'document' | 'selection'
  request: { scope: Record<string, unknown>; modelId: string; styleId: string; prompt: string; strength: number; candidateCount: number }
}

function flattenAgentPrompts(prompts: AgentPromptPreset[]): AgentPromptOption[] {
  return prompts.flatMap((prompt) => prompt.versions.map((version) => ({
    promptId: prompt.promptId,
    versionId: version.versionId,
    kind: prompt.kind,
    name: prompt.name,
    version: version.version,
    content: version.content,
    isDefault: prompt.isDefault,
  })))
}

function saveAgentPromptSelection(workspaceId: string, rewritePromptVersionId: string, storyboardPromptVersionId: string, imageStylePromptVersionId: string) {
  if (!workspaceId) return
  localStorage.setItem(`lingzhu-agent-prompts-${workspaceId}`, JSON.stringify({ rewritePromptVersionId, storyboardPromptVersionId, imageStylePromptVersionId }))
}

function groupAgentThreads(items: AgentThreadSummary[]): AgentThreadGroup[] {
  const groups = new Map<string, AgentThreadSummary[]>()
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  for (const item of items) {
    const date = new Date(item.lastActivityAt || item.createdAt)
    const day = new Date(date)
    day.setHours(0, 0, 0, 0)
    const ageDays = Math.floor((today.getTime() - day.getTime()) / 86_400_000)
    const label = ageDays <= 0 ? '今天' : ageDays === 1 ? '昨天' : ageDays < 7 ? '最近 7 天' : '更早'
    groups.set(label, [...(groups.get(label) ?? []), item])
  }
  return [...groups].map(([label, groupedItems]) => ({ label, items: groupedItems }))
}

function formatThreadActivity(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}

const SAMPLE_TEXT = `我认为，很多时候一篇文案只要把事情说清楚就够了。但真正让读者愿意继续看下去的，往往是那些具体、自然的表达，这一点非常重要。\n\n写作时不必急着追求华丽的词语。先把自己想表达的观点说出来，再检查每一段是否都围绕主题展开。删掉重复的内容，补上必要的细节，因此文章会更清楚，也更容易让人记住。\n\n如果需要改写，重要的是保留原来的事实和想法，再根据读者和使用场景调整语气。这样改出来的文字，既有变化，也不会偏离原意。`

function formatCount(text: string) {
  return text.replace(/\s/g, '').length
}

function selectionOffsets(root: HTMLElement): { from: number; to: number } | null {
  const selected = window.getSelection()
  if (!selected || selected.rangeCount === 0 || !root.contains(selected.anchorNode)) return null
  const range = selected.getRangeAt(0)
  const offsetOf = (node: Node, offset: number) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    let total = 0
    let current: Node | null
    while ((current = walker.nextNode())) {
      if (current === node) return total + offset
      total += current.textContent?.length ?? 0
    }
    return total
  }
  const from = offsetOf(range.startContainer, range.startOffset)
  const to = offsetOf(range.endContainer, range.endOffset)
  if (from === null || to === null || from === to) return null
  return { from: Math.min(from, to), to: Math.max(from, to) }
}

function App() {
  const [authStatus, setAuthStatus] = useState<'checking' | 'anonymous' | 'authenticated'>('checking')
  const [authEmail, setAuthEmail] = useState('')
  const [authPassword, setAuthPassword] = useState('')
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login')
  const [authBusy, setAuthBusy] = useState(false)
  const [authError, setAuthError] = useState('')
  const [currentUser, setCurrentUser] = useState<{ id: string; email: string } | null>(null)
  const [workspaceId, setWorkspaceId] = useState('')
  const [documentRef, setDocumentRef] = useState<DocumentRef | null>(null)
  const [sourceEditing, setSourceEditing] = useState(false)
  const [sourceEditDraft, setSourceEditDraft] = useState('')
  const [session, setSession] = useState<RewriteSession | null>(null)
  const [editing, setEditing] = useState(false)
  const [editDraft, setEditDraft] = useState('')
  const [selection, setSelection] = useState<Selection | null>(null)
  const [models, setModels] = useState<Array<{ id: string; name: string; available: boolean }>>([])
  const [presets, setPresets] = useState<RewritePreset[]>([])
  const [mode, setMode] = useState('demo')
  const [modelId, setModelId] = useState('deepseek-v4-flash')
  const [agentModelId, setAgentModelId] = useState('deepseek-v4-flash')
  const [agentPromptCatalog, setAgentPromptCatalog] = useState<Record<AgentPromptKind, AgentPromptPreset[]>>({ rewrite: [], storyboard: [], image_style: [] })
  const [rewritePromptVersionId, setRewritePromptVersionId] = useState(DEFAULT_REWRITE_PROMPT_VERSION_ID)
  const [storyboardPromptVersionId, setStoryboardPromptVersionId] = useState(DEFAULT_STORYBOARD_PROMPT_VERSION_ID)
  const [imageStylePromptVersionId, setImageStylePromptVersionId] = useState(DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID)
  const [promptEditor, setPromptEditor] = useState<AgentPromptEditorState | null>(null)
  const [promptNameDraft, setPromptNameDraft] = useState('')
  const [promptContentDraft, setPromptContentDraft] = useState('')
  const [promptSaving, setPromptSaving] = useState(false)
  const [styleId, setStyleId] = useState('humanize')
  const [prompt, setPrompt] = useState('')
  const [strength, setStrength] = useState(55)
  const [candidateCount, setCandidateCount] = useState(1)
  const [lastRun, setLastRun] = useState<{ id: string; candidates: string[]; selectedIndex: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pendingChatRetry, setPendingChatRetry] = useState<PendingChatRetry | null>(null)
  const [pendingRewriteRetry, setPendingRewriteRetry] = useState<PendingRewriteRetry | null>(null)
  const [notice, setNotice] = useState('')
  const [threadId, setThreadId] = useState('')
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [threadHistoryOpen, setThreadHistoryOpen] = useState(false)
  const [threadHistoryQuery, setThreadHistoryQuery] = useState('')
  const [threadHistory, setThreadHistory] = useState<AgentThreadSummary[]>([])
  const [threadHistoryOffset, setThreadHistoryOffset] = useState(0)
  const [threadHistoryHasMore, setThreadHistoryHasMore] = useState(false)
  const [threadHistoryLoading, setThreadHistoryLoading] = useState(false)
  const [switchingThreadId, setSwitchingThreadId] = useState('')
  const [chatText, setChatText] = useState('')
  const [attachCopy, setAttachCopy] = useState(false)
  const [expandedMessages, setExpandedMessages] = useState<Set<string>>(() => new Set())
  const [subtitleFileId, setSubtitleFileId] = useState('')
  const [subtitleName, setSubtitleName] = useState('')
  const [expandedConfirmationId, setExpandedConfirmationId] = useState('')
  const [previewImage, setPreviewImage] = useState<{ url: string; alt: string } | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [assetSelectionMode, setAssetSelectionMode] = useState(false)
  const [selectedAssetIds, setSelectedAssetIds] = useState<Set<string>>(() => new Set())
  const [downloadingAssets, setDownloadingAssets] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [cancelingJobs, setCancelingJobs] = useState<Set<string>>(() => new Set())
  const [retryingJobs, setRetryingJobs] = useState<Set<string>>(() => new Set())
  const [resolvingActionIds, setResolvingActionIds] = useState<Set<string>>(() => new Set())
  const [previewStyleDrafts, setPreviewStyleDrafts] = useState<Record<string, string>>({})
  const [selectedPreviewIndices, setSelectedPreviewIndices] = useState<Record<string, number>>({})
  const [rightTab, setRightTab] = useState<'chat' | 'assets'>('chat')
  const [chatStreaming, setChatStreaming] = useState(false)
  const [mobilePanel, setMobilePanel] = useState<'source' | 'rewrite' | 'agent'>('rewrite')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false)
  const originalFileInput = useRef<HTMLInputElement>(null)
  const subtitleInput = useRef<HTMLInputElement>(null)
  const comparisonRef = useRef<HTMLDivElement>(null)
  const editRef = useRef<HTMLTextAreaElement>(null)
  const chatScrollRef = useRef<HTMLDivElement>(null)
  const chatShouldFollowRef = useRef(true)
  const chatStreamingRef = useRef(false)
  const pendingChatRequestIdRef = useRef<string | null>(null)
  const resolvingActionIdsRef = useRef(new Set<string>())
  const [showChatLatestButton, setShowChatLatestButton] = useState(false)
  const timers = useRef<number[]>([])

  useEffect(() => {
    if (!expandedConfirmationId && !previewImage) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setExpandedConfirmationId('')
        setPreviewImage(null)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [expandedConfirmationId, previewImage])

  const showNotice = useCallback((text: string) => {
    setNotice(text)
    window.setTimeout(() => setNotice(''), 3000)
  }, [])

  const refreshAssets = useCallback(async () => {
    if (!workspaceId) return
    try { setAssets(await api.assets(workspaceId)) } catch (err) { setError((err as Error).message) }
  }, [workspaceId])

  const refreshJobs = useCallback(async () => {
    if (!workspaceId) return
    try { setJobs(await api.jobs(workspaceId)) } catch (err) { setError((err as Error).message) }
  }, [workspaceId])

  const refreshMessages = useCallback(async () => {
    if (!threadId || chatStreamingRef.current) return
    try { setMessages(await api.messages(threadId)) } catch (err) { setError((err as Error).message) }
  }, [threadId])

  const loadThreadHistoryPage = useCallback(async (offset: number, append: boolean, query: string) => {
    if (!workspaceId) return
    setThreadHistoryLoading(true)
    try {
      const result = await api.agentThreads(workspaceId, { q: query.trim(), offset, limit: 40 })
      setThreadHistory((current) => append ? [...current, ...result.items] : result.items)
      setThreadHistoryOffset(result.nextOffset)
      setThreadHistoryHasMore(result.hasMore)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setThreadHistoryLoading(false)
    }
  }, [workspaceId])

  useEffect(() => {
    if (!threadHistoryOpen || !workspaceId) return
    const timer = window.setTimeout(() => loadThreadHistoryPage(0, false, threadHistoryQuery), 220)
    return () => window.clearTimeout(timer)
  }, [threadHistoryOpen, workspaceId, threadHistoryQuery, loadThreadHistoryPage])

  useEffect(() => {
    api.auth.me().then(({ user }) => {
      setCurrentUser(user)
      setAuthStatus('authenticated')
    }).catch(() => setAuthStatus('anonymous'))
  }, [])

  useEffect(() => {
    if (authStatus !== 'authenticated') return
    let active = true
    async function initialize() {
      try {
        let workspaces = await api.workspaces()
        if (!workspaces.length) workspaces = [await api.createWorkspace('灵珠工作区')]
        if (!active) return
        setWorkspaceId(workspaces[0].id)
        const [modelResponse, presetResponse, promptResponse] = await Promise.all([api.models(), api.presets(), api.agentPrompts()])
        if (!active) return
        setModels(modelResponse.models)
        setPresets(presetResponse)
        setAgentPromptCatalog(promptResponse)
        const promptOptions = [...flattenAgentPrompts(promptResponse.rewrite), ...flattenAgentPrompts(promptResponse.storyboard), ...flattenAgentPrompts(promptResponse.image_style)]
        let savedPromptSelection: { rewritePromptVersionId?: string; storyboardPromptVersionId?: string; imageStylePromptVersionId?: string } = {}
        try {
          savedPromptSelection = JSON.parse(localStorage.getItem(`lingzhu-agent-prompts-${workspaces[0].id}`) || '{}')
        } catch { /* Use the system defaults when saved selection is invalid. */ }
        const initialRewritePromptId = promptOptions.some((item) => item.kind === 'rewrite' && item.versionId === savedPromptSelection.rewritePromptVersionId)
          ? savedPromptSelection.rewritePromptVersionId!
          : DEFAULT_REWRITE_PROMPT_VERSION_ID
        const initialStoryboardPromptId = promptOptions.some((item) => item.kind === 'storyboard' && item.versionId === savedPromptSelection.storyboardPromptVersionId)
          ? savedPromptSelection.storyboardPromptVersionId!
          : DEFAULT_STORYBOARD_PROMPT_VERSION_ID
        const initialImageStylePromptId = promptOptions.some((item) => item.kind === 'image_style' && item.versionId === savedPromptSelection.imageStylePromptVersionId)
          ? savedPromptSelection.imageStylePromptVersionId!
          : DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
        setRewritePromptVersionId(initialRewritePromptId)
        setStoryboardPromptVersionId(initialStoryboardPromptId)
        setImageStylePromptVersionId(initialImageStylePromptId)
        saveAgentPromptSelection(workspaces[0].id, initialRewritePromptId, initialStoryboardPromptId, initialImageStylePromptId)
        if (presetResponse.length > 0) setStyleId(presetResponse[0].id)
        setMode(modelResponse.mode)
        const availableDefault = modelResponse.models.find((item) => item.available)
        if (availableDefault) {
          setModelId(availableDefault.id)
          setAgentModelId(availableDefault.id)
        }
        const documentKey = `lingzhu-document-${workspaces[0].id}`
        const savedDocument = localStorage.getItem(documentKey)
        if (savedDocument) {
          try {
            const reference = JSON.parse(savedDocument) as { id: string }
            const document = await api.getDocument(reference.id)
            if (!active) return
            setDocumentRef({ id: document.id, revisionId: document.revisionId, fileName: document.fileName, content: document.content })
            const savedSession = localStorage.getItem(`lingzhu-session-${workspaces[0].id}`)
            if (savedSession) setSession(await api.getRewriteSession(savedSession))
          } catch {
            localStorage.removeItem(documentKey)
            localStorage.removeItem(`lingzhu-session-${workspaces[0].id}`)
          }
        }
      } catch (err) {
        setError(`后端连接失败：${(err as Error).message}`)
      }
    }
    initialize()
    return () => { active = false; timers.current.forEach(window.clearInterval) }
  }, [authStatus])

  useEffect(() => {
    if (!workspaceId) return
    let active = true
    async function loadThread() {
      try {
        const storageKey = `lingzhu-thread-${workspaceId}`
        const savedThreadId = localStorage.getItem(storageKey)
        if (savedThreadId) {
          try {
            const history = await api.messages(savedThreadId)
            if (!active) return
            setThreadId(savedThreadId)
            setMessages(history)
            return
          } catch {
            localStorage.removeItem(storageKey)
          }
        }
        const thread = await api.createThread(workspaceId)
        if (!active) return
        setThreadId(thread.id)
        localStorage.setItem(storageKey, thread.id)
        setMessages([{
          id: 'welcome', role: 'assistant', type: 'text', metadata: {},
          content: '你好，我是灵珠智能助手。你可以在这里像平常聊天一样讨论或改写文案；需要分镜和图片时，我会先请你确认最终文案。',
        }])
      } catch (err) { setError((err as Error).message) }
    }
    loadThread()
    return () => { active = false }
  }, [workspaceId])

  useEffect(() => {
    if (!threadId) return
    refreshMessages()
  }, [threadId, refreshMessages])

  useEffect(() => {
    if (!workspaceId) return
    refreshAssets()
    refreshJobs()
    const timer = window.setInterval(() => {
      refreshJobs()
      refreshAssets()
      refreshMessages()
    }, 2500)
    timers.current.push(timer)
    return () => window.clearInterval(timer)
  }, [workspaceId, refreshAssets, refreshJobs, refreshMessages])

  useEffect(() => {
    chatShouldFollowRef.current = true
    setShowChatLatestButton(false)
  }, [threadId])

  useEffect(() => {
    const chat = chatScrollRef.current
    if (!chat || !chatShouldFollowRef.current) return
    chat.scrollTo({ top: chat.scrollHeight, behavior: 'auto' })
  }, [messages])

  const currentText = session?.content ?? ''
  const activeJobs = useMemo(() => jobs.filter((job) => ['queued', 'running', 'cancel_requested'].includes(job.status)), [jobs])
  const approvedAssets = assets.filter((asset) => asset.status === 'approved').length
  const groupedThreadHistory = useMemo(() => groupAgentThreads(threadHistory), [threadHistory])

  async function uploadDocument(file: File) {
    if (!workspaceId || busy || editing || sourceEditing) return
    setBusy(true)
    setError('')
    try {
      const result = await api.uploadDocument(workspaceId, file)
      // 上传新原文只切换左侧对比基准，中间稿保留在原改写会话中。
      const updatedSession = session
        ? await api.updateComparisonBaseline(session.id, result.documentId, result.revisionId)
        : null
      setDocumentRef({ id: result.documentId, revisionId: result.revisionId, fileName: result.fileName, content: result.content })
      if (updatedSession) setSession(updatedSession)
      setSourceEditing(false)
      setSourceEditDraft('')
      localStorage.setItem(`lingzhu-document-${workspaceId}`, JSON.stringify({ id: result.documentId, revisionId: result.revisionId }))
      showNotice(updatedSession ? `已载入 ${result.fileName}，中间改写稿已保留并改为与新原文对比` : `已载入 ${result.fileName}`)
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  function clearWorkbench() {
    if (!workspaceId) return
    localStorage.removeItem(`lingzhu-document-${workspaceId}`)
    localStorage.removeItem(`lingzhu-session-${workspaceId}`)
    setDocumentRef(null)
    setSourceEditing(false)
    setSourceEditDraft('')
    setSession(null)
    setEditing(false)
    setEditDraft('')
    setSelection(null)
    setLastRun(null)
    setError('')
    setClearConfirmOpen(false)
    showNotice('工作台已清空，历史文案仍保留')
  }

  async function loadSample() {
    await uploadDocument(new File([SAMPLE_TEXT], '灵珠文案示例.txt', { type: 'text/plain' }))
  }

  async function copyToRewrite() {
    if (!documentRef || !workspaceId || sourceEditing || editing || busy) return
    setBusy(true)
    try {
      const next = await api.createRewriteSession(workspaceId, documentRef.id, documentRef.revisionId)
      setSession(next)
      localStorage.setItem(`lingzhu-session-${workspaceId}`, next.id)
      setLastRun(null)
      setSelection(null)
      setEditing(false)
      setMobilePanel('rewrite')
      showNotice('已复制到改写区')
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  function beginSourceEdit() {
    if (busy || editing) return
    setSourceEditDraft(documentRef?.content ?? '')
    setSourceEditing(true)
  }

  async function saveSourceEdit() {
    if (busy || editing) return
    if (!workspaceId || !sourceEditDraft.trim()) {
      setError('原文不能为空')
      return
    }
    setBusy(true)
    setError('')
    try {
      if (documentRef) {
        const result = await api.saveDocumentRevision(
          documentRef.id,
          documentRef.revisionId,
          sourceEditDraft,
          session?.id,
        )
        setDocumentRef({ id: result.documentId, revisionId: result.revisionId, fileName: result.fileName, content: result.content })
        if (result.session) setSession(result.session)
        localStorage.setItem(`lingzhu-document-${workspaceId}`, JSON.stringify({ id: result.documentId, revisionId: result.revisionId }))
      } else {
        const result = await api.uploadText(workspaceId, '手动输入文案.txt', sourceEditDraft)
        setDocumentRef({ id: result.documentId, revisionId: result.revisionId, fileName: result.fileName, content: result.content })
        localStorage.setItem(`lingzhu-document-${workspaceId}`, JSON.stringify({ id: result.documentId, revisionId: result.revisionId }))
      }
      setSourceEditing(false)
      setSourceEditDraft('')
      showNotice('原文已保存；中间改写稿保持不变')
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  function readSelection() {
    if (!comparisonRef.current) return
    const selectedText = window.getSelection()?.toString() ?? ''
    const offsets = selectionOffsets(comparisonRef.current)
    if (!selectedText.trim() || !offsets) { setSelection(null); return }
    setSelection({ ...offsets, text: selectedText })
  }

  async function saveManualEdit() {
    if (!session || busy || sourceEditing) return
    setBusy(true)
    try {
      const next = await api.updateRewriteSession(session.id, session.workingRevisionId, editDraft)
      setSession(next)
      setEditing(false)
      setSelection(null)
      setLastRun(null)
      showNotice('中间文案已保存，对比已更新')
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  async function runRewrite(scope: 'document' | 'selection', retry?: PendingRewriteRetry) {
    if (editing || sourceEditing || busy) return
    if (!session) { setError('请先把左侧原文复制到改写区'); return }
    if (retry && (retry.sessionId !== session.id || retry.workingRevisionId !== session.workingRevisionId)) {
      setPendingRewriteRetry(null)
      setError('中间文案已变化，请按当前内容重新发起改写')
      return
    }
    if (!retry && scope === 'selection' && !selection) { setError('请先在中间文案中选中要改写的部分'); return }
    setBusy(true)
    setError('')
    const requestBody = retry
      ? { ...retry.request, modelId }
      : {
          scope: scope === 'document' ? { type: 'document' } : { type: 'selection', from: selection!.from, to: selection!.to, selectedText: selection!.text },
          modelId,
          styleId,
          prompt,
          strength,
          candidateCount,
        }
    try {
      const result = await api.rewrite(session, requestBody)
      setSession({ ...session, workingRevisionId: result.workingRevisionId, content: result.content, diffSegments: result.diffSegments })
      setSelection(null)
      setEditing(false)
      setLastRun({ id: result.runId, candidates: result.candidates, selectedIndex: result.selectedIndex })
      setMode(result.mode)
      setPendingRewriteRetry(null)
      if (result.mode === 'demo') showNotice('体验模式：已更新中间文案并生成差异对比')
    } catch (err) {
      setPendingRewriteRetry({ sessionId: session.id, workingRevisionId: session.workingRevisionId, scope, request: requestBody })
      setError((err as Error).message)
    } finally { setBusy(false) }
  }

  function restoreThreadPromptSelection(history: AgentMessage[]) {
    const latestUser = [...history].reverse().find((message) => message.role === 'user')
    const available = [...flattenAgentPrompts(agentPromptCatalog.rewrite), ...flattenAgentPrompts(agentPromptCatalog.storyboard), ...flattenAgentPrompts(agentPromptCatalog.image_style)]
    const savedRewriteId = latestUser?.metadata?.rewritePrompt?.versionId
    const savedStoryboardId = latestUser?.metadata?.storyboardPrompt?.versionId
    const savedImageStyleId = latestUser?.metadata?.imageStylePrompt?.versionId
    const nextRewriteId = available.some((item) => item.kind === 'rewrite' && item.versionId === savedRewriteId)
      ? savedRewriteId
      : DEFAULT_REWRITE_PROMPT_VERSION_ID
    const nextStoryboardId = available.some((item) => item.kind === 'storyboard' && item.versionId === savedStoryboardId)
      ? savedStoryboardId
      : DEFAULT_STORYBOARD_PROMPT_VERSION_ID
    const nextImageStyleId = available.some((item) => item.kind === 'image_style' && item.versionId === savedImageStyleId)
      ? savedImageStyleId
      : DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
    setRewritePromptVersionId(nextRewriteId)
    setStoryboardPromptVersionId(nextStoryboardId)
    setImageStylePromptVersionId(nextImageStyleId)
    saveAgentPromptSelection(workspaceId, nextRewriteId, nextStoryboardId, nextImageStyleId)
  }

  async function selectAgentThread(summary: AgentThreadSummary) {
    if (chatStreamingRef.current || switchingThreadId) return
    if (summary.id === threadId) {
      setThreadHistoryOpen(false)
      return
    }
    setSwitchingThreadId(summary.id)
    setError('')
    try {
      const history = await api.messages(summary.id)
      setThreadId(summary.id)
      localStorage.setItem(`lingzhu-thread-${workspaceId}`, summary.id)
      setMessages(history)
      setExpandedMessages(new Set())
      restoreThreadPromptSelection(history)
      setThreadHistoryOpen(false)
      chatShouldFollowRef.current = true
      setShowChatLatestButton(false)
      showNotice('已切换到历史会话')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSwitchingThreadId('')
    }
  }

  async function createNewAgentThread() {
    if (!workspaceId || chatStreamingRef.current || switchingThreadId) return
    setSwitchingThreadId('__new__')
    try {
      const thread = await api.createThread(workspaceId)
      setThreadId(thread.id)
      localStorage.setItem(`lingzhu-thread-${workspaceId}`, thread.id)
      setMessages([])
      setExpandedMessages(new Set())
      setThreadHistoryOpen(false)
      chatShouldFollowRef.current = true
      setShowChatLatestButton(false)
      showNotice('已新建会话')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSwitchingThreadId('')
    }
  }

  async function chooseCandidate(candidateIndex: number) {
    if (!lastRun || !session || candidateIndex === lastRun.selectedIndex) return
    setBusy(true)
    try {
      const result = await api.selectCandidate(lastRun.id, candidateIndex)
      setSession({ ...session, workingRevisionId: result.workingRevisionId, content: result.content, diffSegments: result.diffSegments })
      setLastRun({ ...lastRun, selectedIndex: candidateIndex })
      showNotice(`已应用第 ${candidateIndex + 1} 个候选`)
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  function setSelectedAgentPrompt(kind: AgentPromptKind, versionId: string) {
    const nextRewriteId = kind === 'rewrite' ? versionId : rewritePromptVersionId
    const nextStoryboardId = kind === 'storyboard' ? versionId : storyboardPromptVersionId
    const nextImageStyleId = kind === 'image_style' ? versionId : imageStylePromptVersionId
    if (kind === 'rewrite') setRewritePromptVersionId(versionId)
    else if (kind === 'storyboard') setStoryboardPromptVersionId(versionId)
    else setImageStylePromptVersionId(versionId)
    saveAgentPromptSelection(workspaceId, nextRewriteId, nextStoryboardId, nextImageStyleId)
  }

  function openAgentPromptEditor(kind: AgentPromptKind, promptId?: string, versionId?: string) {
    if (!promptId) {
      setPromptEditor({ kind })
      setPromptNameDraft('')
      setPromptContentDraft('')
      return
    }
    const preset = agentPromptCatalog[kind].find((item) => item.promptId === promptId)
    const version = preset?.versions.find((item) => item.versionId === versionId)
    if (!preset || !version) return
    setPromptEditor({ kind, promptId, versionId })
    setPromptNameDraft(preset.name)
    setPromptContentDraft(version.content)
  }

  async function saveAgentPrompt() {
    if (!promptEditor || promptSaving || !promptNameDraft.trim() || !promptContentDraft.trim()) return
    setPromptSaving(true)
    setError('')
    try {
      const saved = promptEditor.promptId
        ? promptEditor.kind === 'image_style'
          ? await api.updateAgentPromptVersion(promptEditor.promptId, promptEditor.versionId!, promptContentDraft)
          : await api.createAgentPromptVersion(promptEditor.promptId, promptContentDraft)
        : await api.createAgentPrompt(promptEditor.kind, promptNameDraft, promptContentDraft)
      const catalog = await api.agentPrompts()
      setAgentPromptCatalog(catalog)
      setSelectedAgentPrompt(promptEditor.kind, saved.versionId)
      setPromptEditor(null)
      showNotice(promptEditor.kind === 'image_style' && promptEditor.promptId
        ? `已更新「${saved.name}」风格提示词`
        : `已保存「${saved.name}」v${saved.version}`)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setPromptSaving(false)
    }
  }

  function renderAgentPromptSelector(kind: AgentPromptKind, label: string, selectedId: string) {
    const options = flattenAgentPrompts(agentPromptCatalog[kind])
    const selected = options.find((item) => item.versionId === selectedId)
    const createValue = `__create_${kind}__`
    return <div className="agent-control-cell">
      <div className="agent-control-heading">
        <span>{label}</span>
        <div className="agent-control-actions">
          <button type="button" className="icon-button" title={`新建${label}`} aria-label={`新建${label}`} disabled={chatStreaming} onClick={() => openAgentPromptEditor(kind)}><Plus size={13} /></button>
          <button type="button" className="icon-button" title={selected?.isDefault ? '系统默认提示词不可编辑' : kind === 'image_style' ? '编辑并覆盖当前生图风格' : '编辑并保存为新版本'} aria-label={kind === 'image_style' ? '编辑生图风格' : '编辑并保存为新版本'} disabled={chatStreaming || !selected || selected.isDefault} onClick={() => selected && openAgentPromptEditor(kind, selected.promptId, selected.versionId)}><Pencil size={12} /></button>
        </div>
      </div>
      <select aria-label={`选择${label}`} value={selectedId} disabled={chatStreaming} onChange={(event) => {
        if (event.target.value === createValue) openAgentPromptEditor(kind)
        else setSelectedAgentPrompt(kind, event.target.value)
      }}>
        {options.map((option) => <option key={option.versionId} value={option.versionId}>{option.isDefault ? '系统默认' : `${option.name} · v${option.version}`}</option>)}
        <option value={createValue}>自定义新提示词…</option>
      </select>
    </div>
  }

  async function sendMessage(event?: FormEvent, overrideMessage?: string, retry?: PendingChatRetry) {
    event?.preventDefault()
    const message = (overrideMessage ?? chatText).trim()
    if (!threadId || !message || chatStreamingRef.current) return
    const requestId = retry
      ? crypto.randomUUID()
      : pendingChatRequestIdRef.current ?? crypto.randomUUID()
    pendingChatRequestIdRef.current = requestId
    if (!retry) setChatText('')
    const localUser: AgentMessage = { id: retry?.userMessageId ?? `local-${Date.now()}`, role: 'user', content: message, type: 'text', metadata: {} }
    const streamingId = `stream-${Date.now()}`
    const streamingAssistant: AgentMessage = { id: streamingId, role: 'assistant', content: '', type: 'text', metadata: { streaming: true } }
    chatShouldFollowRef.current = true
    setShowChatLatestButton(false)
    chatStreamingRef.current = true
    setChatStreaming(true)
    setMessages((current) => retry
      ? [...current.filter((item) => item.id !== streamingId), streamingAssistant]
      : [...current, localUser, streamingAssistant])
    try {
      const response = await api.sendMessage(threadId, {
        message,
        modelId: agentModelId,
        requestId,
        rewritePromptVersionId: retry?.rewritePromptVersionId ?? rewritePromptVersionId,
        storyboardPromptVersionId: retry?.storyboardPromptVersionId ?? storyboardPromptVersionId,
        imageStylePromptVersionId: retry?.imageStylePromptVersionId ?? imageStylePromptVersionId,
        ...(retry?.contextText ? { contextText: retry.contextText } : !retry && attachCopy && currentText ? { contextText: currentText } : {}),
        ...(retry?.subtitleFileId ? { subtitleFileId: retry.subtitleFileId } : !retry && subtitleFileId ? { subtitleFileId } : {}),
        ...(retry ? { retryMessageId: retry.userMessageId } : {}),
      }, (delta) => {
        setMessages((current) => current.map((item) => item.id === streamingId ? { ...item, content: item.content + delta } : item))
      })
      setMessages((current) => current.map((item) => item.id === streamingId ? response.message : item))
      setAttachCopy(false)
      setPendingChatRetry(null)
      pendingChatRequestIdRef.current = null
    } catch (err) {
      const failure = err as Error
      setMessages((current) => current.filter((item) => item.id !== streamingId && (retry || item.id !== localUser.id)))
      if (err instanceof AgentStreamError && err.modelFailure && err.userMessageId) {
        pendingChatRequestIdRef.current = null
        setPendingChatRetry({
          message,
          contextText: retry?.contextText ?? (!retry && attachCopy && currentText ? currentText : undefined),
          subtitleFileId: retry?.subtitleFileId ?? (!retry ? subtitleFileId || undefined : undefined),
          rewritePromptVersionId: retry?.rewritePromptVersionId ?? rewritePromptVersionId,
          storyboardPromptVersionId: retry?.storyboardPromptVersionId ?? storyboardPromptVersionId,
          imageStylePromptVersionId: retry?.imageStylePromptVersionId ?? imageStylePromptVersionId,
          userMessageId: err.userMessageId,
        })
        await refreshMessages()
      } else if (err instanceof AgentStreamError) {
        pendingChatRequestIdRef.current = null
      } else {
        setChatText(message)
      }
      setError(failure.message)
    } finally {
      chatStreamingRef.current = false
      setChatStreaming(false)
      refreshMessages()
    }
  }

  function handleChatScroll() {
    const chat = chatScrollRef.current
    if (!chat) return
    const isNearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight <= 64
    chatShouldFollowRef.current = isNearBottom
    setShowChatLatestButton(!isNearBottom)
  }

  function scrollChatToLatest() {
    const chat = chatScrollRef.current
    if (!chat) return
    chatShouldFollowRef.current = true
    setShowChatLatestButton(false)
    chat.scrollTo({ top: chat.scrollHeight, behavior: 'auto' })
  }

  async function resolveAction(actionId: string, body: { decision: 'approve' | 'reject' | 'retry_preview'; selectedPreviewIndex?: number; stylePrompt?: string; imageStylePromptVersionId?: string }) {
    if (!threadId || resolvingActionIdsRef.current.has(actionId)) return
    resolvingActionIdsRef.current.add(actionId)
    setResolvingActionIds(new Set(resolvingActionIdsRef.current))
    try {
      const result = await api.resolveAction(threadId, actionId, body)
      await refreshMessages()
      await refreshJobs()
      if (result.status !== 'already_processed') {
        showNotice(body.decision === 'retry_preview' ? '已按新风格重新生成样图' : result.jobId ? '已确认，后台任务开始执行' : '已取消操作')
      }
    } catch (err) {
      const message = (err as Error).message
      if (message === '该操作已经处理') {
        await Promise.all([refreshMessages(), refreshJobs()])
      } else {
        setError(message)
      }
    } finally {
      resolvingActionIdsRef.current.delete(actionId)
      setResolvingActionIds(new Set(resolvingActionIdsRef.current))
    }
  }

  async function cancelJob(jobId: string) {
    setCancelingJobs((current) => new Set(current).add(jobId))
    try {
      const result = await api.cancelJob(jobId)
      await Promise.all([refreshJobs(), refreshMessages()])
      showNotice(result.status === 'cancelled' ? '任务已取消' : result.status === 'cancel_requested' ? '正在停止当前任务' : '任务已经结束，无法取消')
    } catch (err) { setError((err as Error).message) } finally {
      setCancelingJobs((current) => {
        const next = new Set(current)
        next.delete(jobId)
        return next
      })
    }
  }

  async function retryJob(jobId: string) {
    setRetryingJobs((current) => new Set(current).add(jobId))
    try {
      await api.retryJob(jobId)
      await Promise.all([refreshJobs(), refreshMessages()])
      showNotice('任务已重新排队')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setRetryingJobs((current) => {
        const next = new Set(current)
        next.delete(jobId)
        return next
      })
    }
  }

  async function uploadSubtitleBundle(files: File[]) {
    if (!workspaceId) return
    const audioExtensions = ['.mp3', '.wav', '.m4a', '.aac', '.flac']
    const subtitleFiles = files.filter((file) => file.name.toLowerCase().endsWith('.srt'))
    const audioFiles = files.filter((file) => audioExtensions.some((extension) => file.name.toLowerCase().endsWith(extension)))
    if (subtitleFiles.length !== 1 || audioFiles.length !== 1) {
      setError('请选择包含一个 SRT 和一个音频文件的文件夹')
      return
    }
    try {
      const result = await api.uploadSubtitleBundle(workspaceId, [subtitleFiles[0], audioFiles[0]])
      setSubtitleFileId(result.id)
      setSubtitleName(`${result.fileName} + ${result.audioFileName} · ${result.cueCount} 段字幕`)
      showNotice('配音和字幕已上传，发送消息继续生成剪映草稿')
    } catch (err) { setError((err as Error).message) }
  }

  async function reviewAsset(asset: Asset, decision: 'approve' | 'reject') {
    try {
      await api.reviewAsset(asset.id, decision)
      await refreshAssets()
      showNotice(decision === 'approve' ? '素材已通过审核' : '素材已标记为不通过')
    } catch (err) { setError((err as Error).message) }
  }

  async function downloadSelectedAssets() {
    if (!workspaceId || !selectedAssetIds.size || downloadingAssets) return
    setDownloadingAssets(true)
    try {
      const result = await api.downloadAssets(workspaceId, [...selectedAssetIds])
      const url = URL.createObjectURL(result.blob)
      const link = document.createElement('a')
      link.href = url
      link.download = result.fileName
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      showNotice(selectedAssetIds.size === 1 ? '图片已开始下载' : `已开始下载 ${selectedAssetIds.size} 张图片`)
      setSelectedAssetIds(new Set())
      setAssetSelectionMode(false)
    } catch (err) { setError((err as Error).message) } finally { setDownloadingAssets(false) }
  }

  function toggleAssetSelection(assetId: string) {
    setSelectedAssetIds((current) => {
      const next = new Set(current)
      if (next.has(assetId)) next.delete(assetId)
      else next.add(assetId)
      return next
    })
  }

  async function copyText(text: string) {
    try { await navigator.clipboard.writeText(text); showNotice('已复制到剪贴板') } catch { setError('复制失败，请手动选择文案') }
  }

  async function submitAuth(event: FormEvent) {
    event.preventDefault()
    setAuthBusy(true)
    setAuthError('')
    try {
      const result = authMode === 'login'
        ? await api.auth.login(authEmail, authPassword)
        : await api.auth.register(authEmail, authPassword)
      setCurrentUser(result.user)
      setAuthPassword('')
      setAuthStatus('authenticated')
    } catch (err) {
      setAuthError((err as Error).message)
    } finally {
      setAuthBusy(false)
    }
  }

  async function signOut() {
    try { await api.auth.logout() } catch { /* Clear the local view even if the session expired. */ }
    setCurrentUser(null)
    setWorkspaceId('')
    setDocumentRef(null)
    setSession(null)
    setThreadId('')
    setMessages([])
    setAssets([])
    setJobs([])
    setAuthStatus('anonymous')
  }

  function renderChatContent(message: AgentMessage) {
    const isLongUserMessage = message.role === 'user' && message.content.length > 360
    const isExpanded = expandedMessages.has(message.id)
    const collapsed = isLongUserMessage && !isExpanded
    return <>
      <div className="message-content">{collapsed ? `${message.content.slice(0, 320).trimEnd()}…` : message.content}</div>
      {isLongUserMessage && <button className="message-expand" onClick={() => setExpandedMessages((current) => {
        const next = new Set(current)
        if (next.has(message.id)) next.delete(message.id)
        else next.add(message.id)
        return next
      })}>{isExpanded ? '收起' : '展开全文'}</button>}
    </>
  }

  function onOriginalFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (file) uploadDocument(file)
    event.target.value = ''
  }

  function chooseSubtitleFolder() {
    const input = subtitleInput.current
    if (!input) return
    input.setAttribute('webkitdirectory', '')
    input.click()
  }

  function onSubtitleFolder(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    if (files.length) uploadSubtitleBundle(files)
    event.target.value = ''
  }

  function selectMobilePanel(panel: 'source' | 'rewrite' | 'agent') { setMobilePanel(panel) }

  if (authStatus === 'checking') return <div className="auth-screen"><span className="auth-loading">正在检查登录状态</span></div>
  if (authStatus === 'anonymous') return (
    <main className="auth-screen">
      <section className="auth-panel">
        <div className="auth-brand"><span><Sparkles size={19} /></span><div><strong>灵珠智能助手</strong><small>文案工作台</small></div></div>
        <h1>{authMode === 'login' ? '登录' : '创建账号'}</h1>
        <form onSubmit={submitAuth}>
          <label><span>邮箱</span><input type="email" autoComplete="email" required maxLength={320} value={authEmail} onChange={(event) => setAuthEmail(event.target.value)} /></label>
          <label><span>密码</span><input type="password" autoComplete={authMode === 'login' ? 'current-password' : 'new-password'} required minLength={10} maxLength={128} value={authPassword} onChange={(event) => setAuthPassword(event.target.value)} /></label>
          {authError && <p className="auth-error" role="alert">{authError}</p>}
          <button className="auth-submit" type="submit" disabled={authBusy}>{authBusy ? '请稍候…' : authMode === 'login' ? '登录' : '注册并登录'}</button>
        </form>
        <button className="auth-switch" type="button" onClick={() => { setAuthMode(authMode === 'login' ? 'register' : 'login'); setAuthError('') }}>
          {authMode === 'login' ? '还没有账号？注册' : '已有账号？登录'}
        </button>
      </section>
    </main>
  )

  return (
    <div className="app-shell">
      <aside className="side-rail">
        <button className="brand-mark" title="灵珠智能助手"><Sparkles size={19} /></button>
        <div className="rail-group">
          <button className="rail-button active" title="工作台"><PanelLeftClose size={18} /></button>
          <button className="rail-button" title="智能体"><MessageCircle size={18} /></button>
          <button className="rail-button" title="任务记录"><Activity size={18} /></button>
          <button className="rail-button" title="素材库" onClick={() => setRightTab('assets')}><ImagePlus size={18} /></button>
        </div>
        <div className="rail-spacer" />
        <button className="rail-button help-button" title="帮助"><CircleHelp size={18} /></button>
        <div className="user-avatar">灵</div>
      </aside>

      <main className="main-shell">
        <header className="topbar">
          <div className="brand-lockup">
            <div className="brand-mark small"><Sparkles size={15} /></div>
            <div><strong>灵珠智能助手</strong><span>文案工作台</span></div>
          </div>
          <div className="topbar-center">
            <span className="workspace-dot" />
            <span>灵珠工作区</span>
            <ChevronDown size={14} />
          </div>
          <div className="topbar-actions">
            <span className={`mode-pill ${mode === 'live' ? 'live' : ''}`}><span />{mode === 'live' ? '模型已连接' : '本地体验模式'}</span>
            <span className="account-email" title={currentUser?.email}>{currentUser?.email}</span>
            <button className="icon-button" title="退出登录" aria-label="退出登录" onClick={signOut}><LogOut size={16} /></button>
            <button className="icon-button" title="设置"><Settings2 size={17} /></button>
            <button className="icon-button" title="更多"><MoreHorizontal size={18} /></button>
          </div>
        </header>

        <nav className="mobile-tabs" aria-label="工作区面板">
          <button className={mobilePanel === 'source' ? 'selected' : ''} onClick={() => selectMobilePanel('source')}>原文</button>
          <button className={mobilePanel === 'rewrite' ? 'selected' : ''} onClick={() => selectMobilePanel('rewrite')}>改写对比</button>
          <button className={mobilePanel === 'agent' ? 'selected' : ''} onClick={() => selectMobilePanel('agent')}>智能体</button>
        </nav>

        <section className="workspace-grid">
          <section className={`work-panel source-panel ${mobilePanel !== 'source' ? 'mobile-hidden' : ''}`}>
            <div className="panel-heading">
              <div className="panel-title"><span className="step-index">01</span><div><h1>原文</h1><p>{sourceEditing ? '粘贴或修改后保存' : '编辑后作为当前比较基准'}</p></div></div>
              <div className="heading-actions">
                <button className="icon-button" title="上传原文" disabled={busy || sourceEditing} onClick={() => originalFileInput.current?.click()}><Upload size={17} /></button>
                {documentRef && <button className={`icon-button ${sourceEditing ? 'pressed' : ''}`} title="手动编辑原文" aria-label="手动编辑原文" disabled={busy || editing} onClick={beginSourceEdit}><Pencil size={16} /></button>}
                <button className="icon-button" title="复制到改写" disabled={!documentRef || busy || sourceEditing || editing} onClick={copyToRewrite}><ArrowLeftRight size={17} /></button>
                <button className="clear-workbench-button" title="清空当前工作台；历史数据保留" disabled={(!documentRef && !session) || busy || sourceEditing || editing} onClick={() => setClearConfirmOpen(true)}><Trash2 size={14} />清空</button>
              </div>
            </div>
            <input ref={originalFileInput} type="file" accept=".txt,.md,.markdown,.docx,.pdf" hidden onChange={onOriginalFile} />
            <div className="document-meta">
              <div className="file-label"><FileText size={14} /><span>{documentRef?.fileName ?? '尚未载入文案'}</span></div>
              <span className="count-label">{sourceEditing ? `${formatCount(sourceEditDraft)} 字` : documentRef ? `${formatCount(documentRef.content)} 字` : '支持 TXT / MD / DOCX / PDF'}</span>
            </div>
            <div className="panel-content source-content">
              {sourceEditing ? <textarea className="source-editor" value={sourceEditDraft} onChange={(event) => setSourceEditDraft(event.target.value)} spellCheck={false} autoFocus /> : documentRef ? <div className="source-text">{documentRef.content}</div> : (
                <div className="empty-upload">
                  <div className="upload-illustration"><FileText size={23} /></div>
                  <strong>载入一份原文</strong>
                  <span>支持 TXT、MD、DOCX、PDF</span>
                  <button className="primary-outline" disabled={busy || editing} onClick={() => originalFileInput.current?.click()}><Upload size={15} />选择文件</button>
                  <button className="text-action" disabled={busy || editing} onClick={beginSourceEdit}><Pencil size={13} />手动输入或粘贴</button>
                  <button className="text-action" disabled={busy || editing} onClick={loadSample}>载入示例文案</button>
                </div>
              )}
            </div>
            <div className="panel-footer source-footer">
              {sourceEditing ? <>
                <div className="footer-note"><ShieldCheck size={14} /><span>保存后更新左侧对比原文，中间稿不变</span></div>
                <div className="source-edit-actions"><button className="ghost-button" disabled={busy} onClick={() => { setSourceEditing(false); setSourceEditDraft('') }}>取消</button><button className="primary-button" disabled={busy || !sourceEditDraft.trim()} onClick={saveSourceEdit}><Check size={15} />保存原文</button></div>
              </> : <>
                <div className="footer-note"><ShieldCheck size={14} /><span>左侧可编辑；修改只更新对比原文</span></div>
                <button className="primary-button" disabled={!documentRef || busy} onClick={copyToRewrite}><ArrowLeftRight size={16} />复制到改写</button>
              </>}
            </div>
          </section>

          <section className={`work-panel rewrite-panel ${mobilePanel !== 'rewrite' ? 'mobile-hidden' : ''}`}>
            <div className="panel-heading">
              <div className="panel-title"><span className="step-index amber">02</span><div><h1>改写对比</h1><p>{editing ? '手动编辑稿件；保存后继续与左侧对比' : session ? '可手动编辑或模型改写，结果对照左侧原文' : '复制原文后开始改写'}</p></div></div>
              <div className="heading-actions">
                <button className={`icon-button ${editing ? 'pressed' : ''}`} title={editing ? '请使用下方保存或取消结束手动编辑' : '手动编辑改写稿'} aria-label="手动编辑改写稿" disabled={!session || busy || sourceEditing || editing} onClick={() => { setEditDraft(currentText); setEditing(true) }}><Pencil size={16} /></button>
                <button className="icon-button" title="复制改写稿" disabled={!currentText} onClick={() => copyText(currentText)}><Copy size={16} /></button>
              </div>
            </div>
            <div className="rewrite-toolbar">
              <label className="select-field"><span>模型</span><select value={modelId} disabled={busy || editing || sourceEditing} onChange={(event) => setModelId(event.target.value)}>{models.map((model) => <option key={model.id} value={model.id}>{model.name}{model.available ? '' : ' · 体验模式'}</option>)}</select></label>
              <label className="select-field"><span>风格</span><select value={styleId} disabled={busy || editing || sourceEditing} onChange={(event) => setStyleId(event.target.value)}>{presets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name}</option>)}</select></label>
              <label className="strength-field"><span>力度</span><input type="range" min="0" max="100" value={strength} disabled={busy || editing || sourceEditing} onChange={(event) => setStrength(Number(event.target.value))} /><b>{strength}</b></label>
              <button className="icon-button subtle" title="更多改写设置" disabled={busy || editing || sourceEditing} onClick={() => setDrawerOpen(true)}><Settings2 size={16} /></button>
            </div>
            {!editing && lastRun && lastRun.candidates.length > 1 && <div className="candidate-strip">候选版本 {lastRun.candidates.map((_, index) => <button key={index} className={lastRun.selectedIndex === index ? 'selected' : ''} disabled={busy} onClick={() => chooseCandidate(index)}>版本 {index + 1}</button>)}</div>}
            <div className="panel-content rewrite-content">
              {!session ? <div className="empty-state"><div className="empty-symbol"><ArrowLeftRight size={21} /></div><strong>原文复制后会显示在这里</strong><span>全篇改写或选中局部改写，差异会自动标黄</span><button className="secondary-button" disabled={!documentRef || busy} onClick={copyToRewrite}>复制左侧原文</button></div> : editing ? (
                <textarea ref={editRef} className="rewrite-editor" value={editDraft} onChange={(event) => setEditDraft(event.target.value)} spellCheck={false} />
              ) : (
                <div className="compare-scroll">
                  <div className="compare-copy" ref={comparisonRef} onMouseUp={readSelection} onKeyUp={readSelection}>
                    {session.diffSegments.map((segment: DiffSegment, index: number) => {
                      if (segment.type === 'removed') return <span key={index} className="deleted-inline" title={`原文删除：${segment.original}`} aria-hidden="true" />
                      if (segment.type === 'changed' || segment.type === 'added') return <mark key={index} className="diff-highlight">{segment.text}</mark>
                      return <span key={index}>{segment.text}</span>
                    })}
                  </div>
                </div>
              )}
            </div>
            <div className="prompt-row"><input value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="补充改写要求，例如：保留口语感" disabled={!session || busy || editing || sourceEditing} /><span>{formatCount(prompt)} 字</span></div>
            <div className="rewrite-controls">
              <div className="rewrite-options">
                <label>候选 <select value={candidateCount} disabled={busy || editing || sourceEditing} onChange={(event) => setCandidateCount(Number(event.target.value))}><option value={1}>1 版</option><option value={2}>2 版</option><option value={3}>3 版</option></select></label>
                <span className="selection-hint">{editing ? '手动编辑中，保存或取消后可继续模型改写' : selection ? `已选 ${formatCount(selection.text)} 字` : '可在文案中选择局部'}</span>
              </div>
              <div className="rewrite-actions">
                {editing ? <><button className="ghost-button" disabled={busy} onClick={() => { setEditing(false); setEditDraft(currentText) }}>取消</button><button className="primary-button" disabled={busy} onClick={saveManualEdit}><Check size={15} />保存编辑</button></> : <>
                  <button className="secondary-button" disabled={!session || busy || sourceEditing || !selection} onClick={() => runRewrite('selection')}><WandSparkles size={15} />局部改写</button>
                  <button className="primary-button rewrite-main" disabled={!session || busy || sourceEditing} onClick={() => runRewrite('document')}>{busy ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={15} />}一键改写</button>
                </>}
              </div>
            </div>
            <div className="comparison-legend"><span><i className="legend-yellow" />改写或新增</span><span><i className="legend-delete" />原文删除</span><span>{session ? `${formatCount(currentText)} 字` : '等待文案'}</span></div>
          </section>

          <section className={`work-panel agent-panel ${mobilePanel !== 'agent' ? 'mobile-hidden' : ''}`}>
            <div className="panel-heading agent-heading">
              <div className="panel-title"><span className="agent-avatar"><Bot size={17} /></span><div><h1>灵珠智能体</h1><p><i className="online-dot" />独立对话，不修改中间文案</p></div></div>
              <div className="heading-actions"><button className="icon-button" title="新建对话" aria-label="新建对话" disabled={chatStreaming || Boolean(switchingThreadId)} onClick={createNewAgentThread}><Plus size={17} /></button><button className={`icon-button ${threadHistoryOpen ? 'pressed' : ''}`} title="对话记录" aria-label="对话记录" disabled={chatStreaming || Boolean(switchingThreadId)} onClick={() => setThreadHistoryOpen(true)}><Clock3 size={17} /></button></div>
            </div>
            <div className="agent-tabs"><button className={rightTab === 'chat' ? 'active' : ''} onClick={() => setRightTab('chat')}><MessageCircle size={14} />对话</button><button className={rightTab === 'assets' ? 'active' : ''} onClick={() => { setRightTab('assets'); refreshAssets() }}><ImagePlus size={14} />素材库 <span>{assets.length || ''}</span></button></div>
            <input ref={subtitleInput} type="file" hidden multiple onChange={onSubtitleFolder} />
            {rightTab === 'chat' ? <>
              <div className="agent-controls-row">
                <div className="agent-control-cell">
                  <div className="agent-control-heading"><span>模型</span></div>
                    <select aria-label="选择智能体模型" value={agentModelId} disabled={chatStreaming} onChange={(event) => setAgentModelId(event.target.value)}>{models.map((model) => <option key={model.id} value={model.id}>{model.name}{model.available ? '' : ' · 体验模式'}</option>)}</select>
                </div>
                {renderAgentPromptSelector('rewrite', '文案改写', rewritePromptVersionId)}
                {renderAgentPromptSelector('storyboard', '分镜生成', storyboardPromptVersionId)}
                {renderAgentPromptSelector('image_style', '生图风格', imageStylePromptVersionId)}
              </div>
              <div className="chat-scroll-area">
                <div className="chat-scroll" ref={chatScrollRef} onScroll={handleChatScroll}>
                {(messages.length ? messages : [{ id: 'welcome', role: 'assistant' as const, content: '你好，我是灵珠智能助手。你可以在这里像平常聊天一样讨论或改写文案；需要分镜和图片时，我会先请你确认最终文案。', type: 'text', metadata: {} }]).map((message) => <article key={message.id} className={`chat-message ${message.role}`}>
                  {message.role === 'assistant' && <div className="message-avatar"><Sparkles size={13} /></div>}
                  <div className="message-body">
                    {renderChatContent(message)}
                    {message.metadata?.streaming && <span className="message-mode">正在生成…</span>}
                    {message.type === 'confirmation' && message.metadata?.actionId && <>
                      {expandedConfirmationId === message.id && <button type="button" className="confirmation-expand-backdrop" aria-label="关闭放大的确认卡片" onClick={() => setExpandedConfirmationId('')} />}
                      <div className={`confirmation-card ${message.metadata.flowStage === 'confirm_image_style' ? 'image-style-card' : ''} ${expandedConfirmationId === message.id ? 'confirmation-card-expanded' : ''}`}>
                      <div className="confirm-heading"><ShieldCheck size={16} /><strong>{message.metadata.actionStatus === 'confirmed' ? '已确认' : message.metadata.actionStatus === 'rejected' ? '已取消' : '等待你的确认'}</strong><button type="button" className="confirmation-expand-button" title={expandedConfirmationId === message.id ? '缩小确认卡片' : '放大确认卡片'} aria-label={expandedConfirmationId === message.id ? '缩小确认卡片' : '放大确认卡片'} onClick={() => setExpandedConfirmationId(expandedConfirmationId === message.id ? '' : message.id)}>{expandedConfirmationId === message.id ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button></div>
                      {message.metadata.previewText && <div className="preview-text">{message.metadata.previewText}</div>}
                      {message.metadata.flowStage === 'confirm_image_style' && message.metadata.previewAssets?.length > 0 && <>
                        <div className="preview-style-heading"><strong>生图风格</strong><span>{message.metadata.imageStyle?.versionId === DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID ? '系统默认' : `v${message.metadata.imageStyle?.version ?? 1}`}</span></div>
                        <textarea
                          className="preview-style-editor"
                          aria-label="本次样图使用的生图风格提示词"
                          maxLength={12000}
                          value={previewStyleDrafts[message.metadata.actionId] ?? message.metadata.imageStyle?.content ?? ''}
                          onChange={(event) => setPreviewStyleDrafts((current) => ({ ...current, [message.metadata.actionId]: event.target.value }))}
                          disabled={Boolean(message.metadata.actionStatus && message.metadata.actionStatus !== 'awaiting_confirmation') || resolvingActionIds.has(message.metadata.actionId)}
                          placeholder="描述所有图片共同遵循的画风，例如水彩、剪纸或电影写实风格"
                        />
                        <button
                          type="button"
                          className="use-saved-style"
                          disabled={Boolean(message.metadata.actionStatus && message.metadata.actionStatus !== 'awaiting_confirmation') || resolvingActionIds.has(message.metadata.actionId)}
                          onClick={() => {
                            const saved = flattenAgentPrompts(agentPromptCatalog.image_style).find((item) => item.versionId === imageStylePromptVersionId)
                            setPreviewStyleDrafts((current) => ({ ...current, [message.metadata.actionId]: saved?.content ?? '' }))
                          }}
                        >使用上方已保存风格</button>
                        <div className="confirmation-assets preview-choice-grid">{message.metadata.previewAssets.map((asset: any) => {
                          const selected = (selectedPreviewIndices[message.metadata.actionId] ?? 0) === asset.previewIndex
                          return <div className={`preview-choice ${selected ? 'selected' : ''}`} key={asset.id || asset.previewIndex}>
                            <button type="button" className="confirmation-image-button" onClick={() => setPreviewImage({ url: asset.url, alt: `第一镜样图 ${asset.previewIndex + 1}` })} aria-label={`放大查看第一镜样图 ${asset.previewIndex + 1}`}><img src={asset.url} alt={`第一镜样图 ${asset.previewIndex + 1}`} /></button>
                            <label><input type="radio" name={`preview-${message.metadata.actionId}`} checked={selected} onChange={() => setSelectedPreviewIndices((current) => ({ ...current, [message.metadata.actionId]: asset.previewIndex }))} disabled={Boolean(message.metadata.actionStatus && message.metadata.actionStatus !== 'awaiting_confirmation')} />选择这张作为第一镜</label>
                          </div>
                        })}</div>
                      </>}
                      {message.metadata.flowStage !== 'confirm_image_style' && message.metadata.previewAssets?.length > 0 && <div className="confirmation-assets">{message.metadata.previewAssets.map((asset: any) => <figure key={asset.id || asset.sceneNumber}><button type="button" className="confirmation-image-button" onClick={() => setPreviewImage({ url: asset.url, alt: `分镜 ${asset.sceneNumber}` })} aria-label={`放大查看分镜 ${asset.sceneNumber}`}><img src={asset.url} alt={`分镜 ${asset.sceneNumber}`} /></button><figcaption>分镜 {asset.sceneNumber}</figcaption></figure>)}</div>}
                      {(!message.metadata.actionStatus || message.metadata.actionStatus === 'awaiting_confirmation') && message.metadata.flowStage === 'confirm_image_style' && <div className="confirm-actions">
                        <button className="ghost-button" disabled={resolvingActionIds.has(message.metadata.actionId) || !(previewStyleDrafts[message.metadata.actionId] ?? message.metadata.imageStyle?.content ?? '').trim()} onClick={() => {
                          const styleText = previewStyleDrafts[message.metadata.actionId] ?? message.metadata.imageStyle?.content ?? ''
                          const savedStyle = flattenAgentPrompts(agentPromptCatalog.image_style).find((item) => item.versionId === imageStylePromptVersionId)
                          const styleVersionId = savedStyle?.content === styleText ? savedStyle.versionId : message.metadata.imageStyle?.versionId
                          resolveAction(message.metadata.actionId, { decision: 'retry_preview', stylePrompt: styleText, imageStylePromptVersionId: styleVersionId })
                        }}>{resolvingActionIds.has(message.metadata.actionId) ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}重新生成样图</button>
                        <button className="approve-button" disabled={resolvingActionIds.has(message.metadata.actionId)} onClick={() => resolveAction(message.metadata.actionId, { decision: 'approve', selectedPreviewIndex: selectedPreviewIndices[message.metadata.actionId] ?? 0 })}>{resolvingActionIds.has(message.metadata.actionId) ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}确认生图</button>
                        <button className="reject-button" disabled={resolvingActionIds.has(message.metadata.actionId)} onClick={() => resolveAction(message.metadata.actionId, { decision: 'reject' })}>返回分镜</button>
                      </div>}
                      {(!message.metadata.actionStatus || message.metadata.actionStatus === 'awaiting_confirmation') && message.metadata.flowStage !== 'confirm_image_style' && <div className="confirm-actions"><button className="approve-button" disabled={resolvingActionIds.has(message.metadata.actionId)} onClick={() => resolveAction(message.metadata.actionId, { decision: 'approve' })}>{resolvingActionIds.has(message.metadata.actionId) ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{resolvingActionIds.has(message.metadata.actionId) ? '处理中…' : '确认并开始'}</button><button className="reject-button" disabled={resolvingActionIds.has(message.metadata.actionId)} onClick={() => resolveAction(message.metadata.actionId, { decision: 'reject' })}>返回修改</button></div>}
                      </div>
                    </>}
                    {message.type === 'job' && message.metadata?.jobId && <JobInline job={jobs.find((item) => item.id === message.metadata.jobId)} onCancel={cancelJob} canceling={cancelingJobs.has(message.metadata.jobId)} onRetry={retryJob} retrying={retryingJobs.has(message.metadata.jobId)} />}
                    {message.type === 'job_result' && message.metadata?.result?.assets && <div className="chat-result-strip">{message.metadata.result.assets.slice(0, 3).map((asset: any) => <img key={asset.id} src={asset.url} alt="生成素材" />)}<button onClick={() => setRightTab('assets')}>检查素材</button></div>}
                    {message.type === 'job_result' && message.metadata?.jobId && jobs.some((job) => job.id === message.metadata.jobId && job.type === 'video_generation' && job.status === 'completed') && <JianyingDownload jobId={message.metadata.jobId} />}{message.type === 'job_result' && message.metadata?.result?.downloadUrl && <a className="download-draft" href={message.metadata.result.downloadUrl} target="_blank" rel="noreferrer"><ArrowDownToLine size={13} />下载 JSON 时间线（小助手格式未验证）</a>}
                    {message.type === 'job_result' && message.metadata?.result?.draftUrl && <button type="button" className="download-draft" onClick={() => copyText(message.metadata.result.draftUrl)} title="复制后粘贴到剪映小助手的草稿地址输入框"><Copy size={13} />复制 OSS 草稿地址</button>}
                    {message.type === 'job_result' && message.metadata?.result?.helperUrl && <a className="download-draft" href={message.metadata.result.helperUrl} target="_blank" rel="noreferrer" title="当前导入 JSON 格式尚未验证"><ExternalLink size={13} />在剪映小助手尝试导入</a>}
                    {message.role === 'assistant' && message.metadata?.mode === 'demo' && <span className="message-mode">体验模式回复</span>}
                  </div>
                </article>)}
                {activeJobs.map((job) => <div className="job-progress" key={job.id}><div className="job-progress-title"><LoaderCircle className="spin" size={14} /><span>{job.message}</span><b>{job.progress}%</b></div><div className="progress-track"><i style={{ width: `${job.progress}%` }} /></div></div>)}
                </div>
                {showChatLatestButton && <button type="button" className="chat-jump-latest" title="回到最新消息" aria-label="回到最新消息" onClick={scrollChatToLatest}><ChevronDown size={17} /></button>}
              </div>
              {subtitleName && <div className="subtitle-chip"><Film size={14} /><span>{subtitleName}</span><button onClick={() => { setSubtitleFileId(''); setSubtitleName('') }} title="移除字幕"><X size={13} /></button></div>}
              <form className="composer" onSubmit={sendMessage}>
                <textarea value={chatText} onChange={(event) => setChatText(event.target.value)} placeholder="和智能体聊聊文案、分镜或图片…" rows={3} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendMessage() } }} />
                <div className="composer-bottom">
                  <div className="composer-tools">
                    <button type="button" className={`tool-chip ${attachCopy ? 'active' : ''}`} disabled={!currentText || chatStreaming} onClick={() => setAttachCopy(!attachCopy)} title="仅将中间文案附加到下一条请求"><Paperclip size={13} />附带中间文案</button>
                    <button type="button" className="tool-icon" title="上传配音和 SRT 文件夹" onClick={chooseSubtitleFolder}><Film size={15} /></button>
                  </div>
                  <button className="send-button" type="submit" disabled={!chatText.trim() || chatStreaming} title="发送消息">{chatStreaming ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}</button>
                </div>
              </form>
            </> : <div className="asset-library">
              <div className="asset-library-top"><div><strong>生成素材</strong><span>{assets.length} 项 · {approvedAssets} 项已通过</span></div><div className="asset-library-actions">
                {assetSelectionMode ? <>
                  <button type="button" className="asset-select-all" onClick={() => setSelectedAssetIds(selectedAssetIds.size === assets.length ? new Set() : new Set(assets.map((asset) => asset.id)))}>{selectedAssetIds.size === assets.length ? '取消全选' : '全选'}</button>
                  <button type="button" className="asset-download-action" title={selectedAssetIds.size === 1 ? '下载选中图片' : '打包下载选中图片'} aria-label={selectedAssetIds.size === 1 ? '下载选中图片' : '打包下载选中图片'} disabled={!selectedAssetIds.size || downloadingAssets} onClick={downloadSelectedAssets}>{downloadingAssets ? <LoaderCircle className="spin" size={14} /> : <Download size={14} />}<span>{selectedAssetIds.size ? `下载 ${selectedAssetIds.size}` : '下载'}</span></button>
                  <button type="button" className="icon-button" title="退出选择" aria-label="退出选择" onClick={() => { setAssetSelectionMode(false); setSelectedAssetIds(new Set()) }}><X size={15} /></button>
                </> : <button type="button" className="icon-button" title="选择图片下载" aria-label="选择图片下载" disabled={!assets.length} onClick={() => setAssetSelectionMode(true)}><Download size={15} /></button>}
                <button className="icon-button" title="刷新素材" onClick={refreshAssets}><RefreshCw size={15} /></button>
              </div></div>
              {assetSelectionMode && <div className="asset-selection-status">{selectedAssetIds.size ? `已选择 ${selectedAssetIds.size} 张` : '选择要下载的图片'}</div>}
              {assets.length ? <div className="asset-grid">{assets.map((asset) => {
                const selected = selectedAssetIds.has(asset.id)
                return <article className={`asset-card ${selected ? 'selected-for-download' : ''}`} key={asset.id}>
                <div className="asset-image"><img src={asset.url} alt={`分镜 ${asset.sceneNumber}`} /><span className={`asset-status ${asset.status}`}>{asset.status === 'approved' ? '已通过' : asset.status === 'rejected' ? '未通过' : '待检查'}</span>{assetSelectionMode && <button type="button" className={`asset-select-circle ${selected ? 'selected' : ''}`} aria-label={`${selected ? '取消选择' : '选择'}分镜 ${asset.sceneNumber}`} aria-pressed={selected} onClick={() => toggleAssetSelection(asset.id)}>{selected && <Check size={14} />}</button>}</div>
                <div className="asset-info"><strong>分镜 {String(asset.sceneNumber).padStart(2, '0')}</strong><span>{asset.isDemo ? '体验模式素材' : asset.fileName}</span></div>
                {asset.status === 'needs_review' && <div className="asset-review"><button onClick={() => reviewAsset(asset, 'reject')} title="标记未通过"><X size={14} /></button><button onClick={() => reviewAsset(asset, 'approve')} title="审核通过"><Check size={14} /></button></div>}
              </article>})}</div> : <div className="asset-empty"><ImagePlus size={21} /><strong>素材库还是空的</strong><span>在智能体对话中确认文案后生成图片</span></div>}
              {assets.length > 0 && approvedAssets > 0 && !subtitleName && <button className="upload-subtitle-button" onClick={chooseSubtitleFolder}><Upload size={15} />上传配音和 SRT 文件夹</button>}
              {subtitleName && <div className="asset-next-step"><CheckCheck size={15} />字幕已就绪，可回到对话请求生成视频。</div>}
            </div>}
            <div className="agent-bottom"><span><ShieldCheck size={13} />智能体操作需要确认</span><button onClick={() => setRightTab('assets')}><ImagePlus size={14} />素材 <b>{assets.length}</b></button></div>
          </section>
        </section>
      </main>

      {previewImage && <div className="image-preview-backdrop" role="presentation" onClick={() => setPreviewImage(null)}>
        <section className="image-preview-dialog" role="dialog" aria-modal="true" aria-label={previewImage.alt} onClick={(event) => event.stopPropagation()}>
          <button type="button" className="icon-button image-preview-close" title="关闭图片预览" aria-label="关闭图片预览" onClick={() => setPreviewImage(null)}><X size={17} /></button>
          <img src={previewImage.url} alt={previewImage.alt} />
          <span>{previewImage.alt}</span>
        </section>
      </div>}
      {error && <div className="toast error-toast"><span>{error}</span>
        {pendingChatRetry && <button type="button" className="error-retry-button" onClick={() => sendMessage(undefined, pendingChatRetry.message, pendingChatRetry)}><RefreshCw size={13} />重试</button>}
        {pendingRewriteRetry && <button type="button" className="error-retry-button" disabled={busy || !session || session.id !== pendingRewriteRetry.sessionId || session.workingRevisionId !== pendingRewriteRetry.workingRevisionId} onClick={() => runRewrite(pendingRewriteRetry.scope, pendingRewriteRetry)}><RefreshCw size={13} />重试改写</button>}
        <button aria-label="关闭错误提示" onClick={() => setError('')}><X size={15} /></button></div>}
      {notice && <div className="toast notice-toast"><Check size={15} /><span>{notice}</span></div>}
      {clearConfirmOpen && <div className="dialog-backdrop" onClick={() => setClearConfirmOpen(false)}>
        <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="clear-dialog-title" onClick={(event) => event.stopPropagation()}>
          <div className="dialog-icon"><Trash2 size={18} /></div>
          <h2 id="clear-dialog-title">清空当前工作台？</h2>
          <p>左侧原文、中间改写稿和差异状态会从当前工作台移除。历史文案和改写记录会保留，不会从数据库删除。</p>
          <div className="dialog-actions"><button className="ghost-button" onClick={() => setClearConfirmOpen(false)}>取消</button><button className="danger-button" onClick={clearWorkbench}><Trash2 size={14} />清空工作台</button></div>
        </section>
      </div>}
      {promptEditor && <div className="dialog-backdrop" onClick={() => !promptSaving && setPromptEditor(null)}>
        <section className="agent-prompt-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-prompt-dialog-title" onClick={(event) => event.stopPropagation()}>
          <div className="agent-prompt-dialog-header">
            <div><h2 id="agent-prompt-dialog-title">{promptEditor.promptId ? promptEditor.kind === 'image_style' ? `编辑「${promptNameDraft}」生图风格` : `保存「${promptNameDraft}」的新版本` : `新建${promptEditor.kind === 'rewrite' ? '文案改写' : promptEditor.kind === 'storyboard' ? '分镜生成' : '生图风格'}提示词`}</h2><p>{promptEditor.promptId ? promptEditor.kind === 'image_style' ? '保存后会覆盖所选生图风格。' : '旧版本会保留，可随时重新选择。' : '保存后会加入对应提示词列表。'}</p></div>
            <button type="button" className="icon-button" title="关闭" disabled={promptSaving} onClick={() => setPromptEditor(null)}><X size={16} /></button>
          </div>
          <label className="agent-prompt-field"><span>名称</span><input value={promptNameDraft} maxLength={120} disabled={Boolean(promptEditor.promptId) || promptSaving} onChange={(event) => setPromptNameDraft(event.target.value)} placeholder="例如：温暖叙事" /></label>
          <label className="agent-prompt-field"><span>提示词</span><textarea value={promptContentDraft} maxLength={12000} disabled={promptSaving} onChange={(event) => setPromptContentDraft(event.target.value)} placeholder="写下希望模型遵循的风格和方向" rows={9} /></label>
          <div className="agent-prompt-dialog-footer"><span>{promptContentDraft.length}/12000</span><div><button type="button" className="ghost-button" disabled={promptSaving} onClick={() => setPromptEditor(null)}>取消</button><button type="button" className="primary-button" disabled={promptSaving || !promptNameDraft.trim() || !promptContentDraft.trim()} onClick={saveAgentPrompt}>{promptSaving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{promptEditor.promptId ? promptEditor.kind === 'image_style' ? '覆盖保存' : '保存新版本' : '保存提示词'}</button></div></div>
        </section>
      </div>}
      {threadHistoryOpen && <div className="thread-history-backdrop" onClick={() => setThreadHistoryOpen(false)}>
        <aside className="thread-history-drawer" role="dialog" aria-modal="true" aria-labelledby="thread-history-title" onClick={(event) => event.stopPropagation()}>
          <header className="thread-history-header">
            <div><h2 id="thread-history-title">对话记录</h2><span>{threadHistory.length ? `共显示 ${threadHistory.length} 条` : '当前工作区'}</span></div>
            <button type="button" className="icon-button" title="关闭对话记录" aria-label="关闭对话记录" onClick={() => setThreadHistoryOpen(false)}><X size={16} /></button>
          </header>
          <label className="thread-history-search"><Search size={15} /><input autoFocus value={threadHistoryQuery} onChange={(event) => setThreadHistoryQuery(event.target.value)} placeholder="搜索会话或消息" /><kbd>⌕</kbd></label>
          <div className="thread-history-list">
            {threadHistoryLoading && !threadHistory.length && <div className="thread-history-state"><LoaderCircle className="spin" size={18} /><span>正在加载会话…</span></div>}
            {!threadHistoryLoading && !threadHistory.length && <div className="thread-history-state"><MessageCircle size={19} /><span>{threadHistoryQuery.trim() ? '没有匹配的会话' : '还没有历史会话'}</span></div>}
            {groupedThreadHistory.map((group) => <section className="thread-history-group" key={group.label}>
              <h3>{group.label}</h3>
              {group.items.map((item) => <button type="button" key={item.id} className={`thread-history-item ${item.id === threadId ? 'current' : ''}`} disabled={Boolean(switchingThreadId)} onClick={() => selectAgentThread(item)}>
                <span className="thread-history-item-top"><strong>{item.title || '新对话'}</strong><time>{formatThreadActivity(item.lastActivityAt || item.createdAt)}</time></span>
                <span className="thread-history-preview">{item.preview || '暂无消息'}</span>
                {switchingThreadId === item.id && <span className="thread-history-loading"><LoaderCircle className="spin" size={13} />加载中</span>}
              </button>)}
            </section>)}
            {threadHistoryHasMore && <button type="button" className="thread-history-more" disabled={threadHistoryLoading} onClick={() => loadThreadHistoryPage(threadHistoryOffset, true, threadHistoryQuery)}>{threadHistoryLoading ? '正在加载…' : '加载更多'}</button>}
          </div>
        </aside>
      </div>}
      {drawerOpen && <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)}><aside className="settings-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header"><div><span>改写设置</span><small>仅影响普通改写面板</small></div><button className="icon-button" onClick={() => setDrawerOpen(false)}><X size={17} /></button></div>
        <label className="drawer-field"><span>改写提示词</span><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="补充要保留或避免的表达" /></label>
        <label className="drawer-field"><span>候选版本</span><select value={candidateCount} onChange={(event) => setCandidateCount(Number(event.target.value))}><option value={1}>1 个候选</option><option value={2}>2 个候选</option><option value={3}>3 个候选</option></select></label>
        <div className="drawer-info"><ShieldCheck size={16} /><p>普通改写和右侧智能体对话分开运行。智能体中的文案不会自动写入这里。</p></div>
        <button className="primary-button drawer-done" onClick={() => setDrawerOpen(false)}>完成</button>
      </aside></div>}
    </div>
  )
}

function JobInline({ job, onCancel, canceling, onRetry, retrying }: { job?: Job; onCancel: (jobId: string) => void; canceling: boolean; onRetry: (jobId: string) => void; retrying: boolean }) {
  if (!job) return <div className="job-inline">任务已提交，正在读取进度…</div>
  return <div className="job-inline"><div>{job.status === 'completed' ? <CheckCheck size={14} /> : ['failed', 'dead_letter', 'cancelled'].includes(job.status) ? <X size={14} /> : <LoaderCircle className="spin" size={14} />}<span>{job.message}</span><b>{job.progress}%</b>{['queued', 'running'].includes(job.status) && <button type="button" className="job-cancel-button" title="取消任务" aria-label="取消任务" disabled={canceling} onClick={() => onCancel(job.id)}><X size={14} /></button>}{['failed', 'dead_letter'].includes(job.status) && <button type="button" className="job-cancel-button" title="重新提交任务" aria-label="重新提交任务" disabled={retrying} onClick={() => onRetry(job.id)}>{retrying ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}</button>}</div><div className="progress-track"><i style={{ width: `${job.progress}%` }} /></div>{['failed', 'dead_letter'].includes(job.status) && <small>{job.error}</small>}{job.status === 'completed' && job.type === 'video_generation' && <JianyingDownload jobId={job.id} />}{job.status === 'completed' && job.result?.downloadUrl && <a className="download-draft" href={job.result.downloadUrl} target="_blank" rel="noreferrer"><ArrowDownToLine size={13} />下载 JSON 时间线（小助手格式未验证）</a>}</div>
}

export default App
