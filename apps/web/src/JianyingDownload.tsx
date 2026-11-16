import { useState } from 'react'

export function JianyingDownload({ jobId }: { jobId: string }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function download() {
    setBusy(true)
    setError('')
    try {
      const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/jianying-package`, { credentials: 'include' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || '素材包下载失败，请重试')
      }
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `lingzhu-jianying-${jobId}.zip`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setError((err as Error).message)
    } finally { setBusy(false) }
  }
  return <div>
    <button type="button" className="download-draft" disabled={busy} onClick={download}>{busy ? '正在打包下载…' : '下载剪映素材包'}</button>
    <a className="download-draft" href="/api/v1/jianying/importer">下载 Windows 导入工具.exe</a>
    <small>双击导入工具，选择素材包并确认剪映草稿目录，无需安装 Python。生成草稿后在剪映中检查并导出视频。</small>
    {error && <p role="alert">{error}</p>}
  </div>
}
