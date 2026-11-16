# 媒体资源 ID 接口

素材和样图不再通过文件名寻址。新接口为：

- `GET /api/v1/assets/{asset_id}/content`
- `GET /api/v1/jobs/{job_id}/previews/{preview_index}/content`

## 定位与权限

正式素材通过 Asset ID 查询；应用层校验所属工作区后读取记录中的 `object_path`。样图通过 Job ID 与 previewIndex 从该任务的结果中读取，要求任务属于当前用户、是 agent 预览图任务，并再次校验 OSS 工作区前缀或本地预览根目录。路径返回私有、禁止缓存的文件响应。

已删除 `GET /api/v1/media/{collection}/{filename}` 和按文件名查询素材的 repository 方法。应用和前端不再用文件名作为定位键。

## URL 来源

- 素材列表及 Worker 新结果使用 Asset ID。
- 样图任务结果使用 Job ID 和 previewIndex。
- 历史消息优先使用其已有 ID；可恢复的旧消息通过用户拥有资源的精确 `objectPath` 映射成新 URL。遇到重复路径时不猜选，标记为不可用。
- 无 ID、无 objectPath 或无法唯一映射的旧文件名 URL 返回 `null`，不会触发旧文件名查找。

## 验证

- 完整后端测试：**287 passed**。
- OpenAPI 差异只有：删除旧文件名媒体接口，新增以上两个 ID 接口。
- 覆盖同名资源、历史路径映射、跨用户拒绝、预览任务归属及 OSS 前缀、本地预览根目录、SSE/消息历史资源 URL 和响应后临时文件清理。
- 测试无外部网络连接尝试，业务数据库内容未变；没有改表、重命名或迁移 OSS 对象。
- 结果位于 `recovery-backups/baseline-20261113-160128/`；本批修改前源码存档位于 `recovery-backups/resource-id-before/`。

已有收藏或书签若保存 `/api/v1/media/.../{filename}`，需要刷新素材列表/消息以取得新 URL；旧接口不提供兼容回退，以确保媒体不再依赖文件名。
