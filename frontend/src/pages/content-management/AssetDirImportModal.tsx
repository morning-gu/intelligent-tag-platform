// 目录导入浏览器（Task 15 从 AssetList 拆出）：先浏览/选择服务器上的一个
// 目录，再把里面带 content.json 的影片目录注册成资产（不传视频、不跑生成）。
// 与上传那条路并行：上传是"视频 → 现场生成内容"，这条是"内容已就绪 → 只登记"。
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  Modal,
  Progress,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useCallback, useEffect, useState } from 'react'
import {
  browseDirs,
  bulkImportMovieDirs,
  type DirEntry,
  type MovieImportResult,
} from '../../api/movieImport'

/** 目录导入 Modal props：open 由父级持有（工具栏按钮触发）；导入成功后由父级刷新列表。 */
interface AssetDirImportModalProps {
  /** Modal 显隐（父级持有开关） */
  open: boolean
  /** 请求关闭 Modal（父级把 open 置 false） */
  onClose: () => void
  /** 导入成功后刷新资产列表 */
  refreshAssets: () => void
}

export default function AssetDirImportModal({
  open,
  onClose,
  refreshAssets,
}: AssetDirImportModalProps) {
  const [dirLoading, setDirLoading] = useState(false)
  // 当前浏览到的目录（绝对路径）+ 它的上级（根目录时为 null）
  const [dirPath, setDirPath] = useState('')
  const [dirParent, setDirParent] = useState<string | null>(null)
  // 路径输入框（可以和当前目录不同，点"打开"才生效）
  const [dirInput, setDirInput] = useState('')
  const [dirEntries, setDirEntries] = useState<DirEntry[]>([])
  const [dirError, setDirError] = useState<string | null>(null)
  // 勾选的影片目录名（默认全选可导入且库里还没有的）
  const [dirPicked, setDirPicked] = useState<string[]>([])
  // 覆盖已存在资产的内容（默认不覆盖，避免冲掉上传那条路产出的内容）
  const [dirOverwrite, setDirOverwrite] = useState(false)
  const [dirImporting, setDirImporting] = useState(false)
  const [dirResult, setDirResult] = useState<MovieImportResult | null>(null)

  /** 浏览一个目录（缺省落在后端配置的根）：列出子目录 + 里面套着的影片数。 */
  const loadDir = useCallback(async (path?: string | null) => {
    setDirLoading(true)
    setDirError(null)
    setDirResult(null)
    try {
      const result = await browseDirs(path ?? null)
      setDirPath(result.path)
      setDirInput(result.path)
      setDirParent(result.parent)
      setDirEntries(result.entries)
      // 不默认勾选：几百个目录里默认全勾中，用户一按导入就可能整批写库。
      // 需要批量时用下面的「全选可导入」按钮显式选择。
      setDirPicked([])
    } catch (err) {
      setDirError(err instanceof Error ? err.message : '目录读取失败')
    } finally {
      setDirLoading(false)
    }
  }, [])

  // 打开即从后端默认根开始浏览：先清掉上一轮的勾选/结果再扫根目录
  // （原 openDirImport 的开场序列，随组件收编；打开路径复位即可，
  // 关闭路径无需清场——下次打开总会重扫）。
  useEffect(() => {
    if (!open) return
    setDirPicked([])
    setDirEntries([])
    setDirOverwrite(false)
    setDirResult(null)
    void loadDir(null)
  }, [open, loadDir])

  /** 执行导入：只提交当前目录下勾选的影片；结果按 imported/skipped/failed 汇总。 */
  async function handleDirImport() {
    if (dirPicked.length === 0) return
    setDirImporting(true)
    setDirError(null)
    try {
      const result = await bulkImportMovieDirs({
        dirNames: dirPicked,
        overwrite: dirOverwrite,
        root: dirPath,
      })
      setDirResult(result)
      if (result.imported > 0) {
        message.success(`已注册 ${result.imported} 部影片资产`)
        await refreshAssets()
      }
      // 结果里被导入的会变成"已导入"，重扫这一层让状态与勾选同步
      await loadDir(dirPath)
      setDirResult(result)
    } catch (err) {
      setDirError(err instanceof Error ? err.message : '目录导入失败')
    } finally {
      setDirImporting(false)
    }
  }

  return (
    <Modal
      title="从目录导入资产"
      open={open}
      width={880}
      okText={`导入所选（${dirPicked.length}）`}
      cancelText="关闭"
      okButtonProps={{ disabled: dirPicked.length === 0 || dirImporting }}
      confirmLoading={dirImporting}
      onOk={() => void handleDirImport()}
      onCancel={onClose}
      destroyOnHidden
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Typography.Text type="secondary">
          选择服务器上的目录：本层带 content.json 的即影片目录，可勾选直接注册为资产
          （不传视频、不跑生成）；普通文件夹点名称进去继续找。关键帧留在原目录，
          生成阶段按场景直接取用。
        </Typography.Text>

        {/* 路径栏：粘贴绝对路径直接打开，或上一级/进入子目录逐层走 */}
        <Space.Compact style={{ width: '100%' }}>
          <Input
            aria-label="目录路径"
            value={dirInput}
            onChange={(e) => setDirInput(e.target.value)}
            onPressEnter={() => void loadDir(dirInput)}
            placeholder="绝对路径，如 /cache/h50065879/movie_asset/movie_asset"
            disabled={dirLoading || dirImporting}
          />
          <Button onClick={() => void loadDir(dirInput)} disabled={dirLoading || dirImporting}>
            打开
          </Button>
          <Button
            onClick={() => void loadDir(dirParent)}
            disabled={dirParent === null || dirLoading || dirImporting}
          >
            上一级
          </Button>
        </Space.Compact>

        {dirError !== null && <Alert type="error" showIcon message="目录读取失败" description={dirError} />}
        {dirLoading ? (
          <div data-testid="dir-scan-loading">
            <Progress percent={100} status="active" showInfo={false} />
            <Typography.Text type="secondary">正在读取目录…</Typography.Text>
          </div>
        ) : (
          <>
            <Typography.Text>
              当前目录：<Typography.Text code>{dirPath}</Typography.Text>
            </Typography.Text>
            {(() => {
              // 本层"可导入"的影片目录：显式全选只作用于这些行
              const selectable = dirEntries.filter(
                (item) => item.is_movie && item.error === null && (!item.exists || dirOverwrite),
              )
              const allPicked =
                selectable.length > 0 && selectable.every((item) => dirPicked.includes(item.name))
              return (
                <Space wrap>
                  <Button
                    size="small"
                    disabled={dirLoading || dirImporting || selectable.length === 0}
                    onClick={() => setDirPicked(selectable.map((item) => item.name))}
                  >
                    全选可导入（{selectable.length}）
                  </Button>
                  <Button
                    size="small"
                    disabled={dirPicked.length === 0}
                    onClick={() => setDirPicked([])}
                  >
                    清空选择
                  </Button>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {allPicked
                      ? `已全选 ${dirPicked.length} 部`
                      : dirPicked.length === 0
                        ? '未选：勾选要导入的影片，或点「全选可导入」'
                        : `已选 ${dirPicked.length} 部`}
                  </Typography.Text>
                </Space>
              )
            })()}
            <Table<DirEntry>
              size="small"
              // 行键必须是"目录名"：勾选列表与导入请求里的 dir_names 都是这个名字。
              // 用 path 当行键时，用户点行勾选拿回的是路径，后端按名字找不到 →
              // 静默 0 导入（页面还显示"导入完成"）。
              rowKey="name"
              pagination={{ pageSize: 10, showSizeChanger: false }}
              dataSource={dirEntries}
              rowSelection={{
                selectedRowKeys: dirPicked,
                onChange: (keys) => setDirPicked(keys as string[]),
                getCheckboxProps: (row) => ({
                  disabled:
                    !row.is_movie || row.error !== null || (row.exists && !dirOverwrite),
                }),
              }}
              columns={[
                {
                  title: '名称',
                  dataIndex: 'name',
                  render: (_, row) =>
                    row.is_movie ? (
                      <span>{row.name}</span>
                    ) : (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: 0 }}
                        onClick={() => void loadDir(row.path)}
                      >
                        📁 {row.name}
                      </Button>
                    ),
                },
                {
                  title: '类型',
                  width: 150,
                  render: (_, row) =>
                    row.is_movie ? (
                      <Tag color="geekblue">影片目录</Tag>
                    ) : (
                      <Tag>文件夹{row.movies > 0 ? ` · 含 ${row.movies} 部` : ''}</Tag>
                    ),
                },
                { title: '场景', dataIndex: 'scenes', width: 70, render: (v, row) => (row.is_movie ? v : '-') },
                { title: '关键帧', dataIndex: 'frames', width: 80, render: (v, row) => (row.is_movie ? v : '-') },
                {
                  title: '状态',
                  width: 190,
                  render: (_, row) =>
                    !row.is_movie ? (
                      <Typography.Text type="secondary">—</Typography.Text>
                    ) : row.error !== null ? (
                      <Tag color="red">无法导入</Tag>
                    ) : row.exists ? (
                      <span>
                        <Tag color="blue">已导入</Tag>
                        {row.matched_by !== null && row.matched_by !== 'asset_id' && (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {row.matched_by === 'title' ? '按片名匹配' : '按 IMDb 匹配'}
                            {row.existing_asset_id !== null ? ` → ${row.existing_asset_id}` : ''}
                          </Typography.Text>
                        )}
                      </span>
                    ) : (
                      <Tag color="green">可导入</Tag>
                    ),
                },
              ]}
              expandable={{
                expandedRowRender: (row) =>
                  row.error !== null ? (
                    <Typography.Text type="danger">{row.error}</Typography.Text>
                  ) : row.is_movie ? (
                    <Typography.Text type="secondary">
                      {row.asset_id ?? '无 asset_id'} · {row.scenes} 个场景 ·{' '}
                      {row.frames} 张关键帧
                      {row.existing_asset_id !== null && row.existing_asset_id !== row.asset_id
                        ? ` · 库里已有同片：${row.existing_asset_id}（${row.matched_by === 'title' ? '按片名' : '按 IMDb'}）`
                        : ''}
                    </Typography.Text>
                  ) : (
                    <Typography.Text type="secondary">
                      {row.path}
                    </Typography.Text>
                  ),
              }}
            />
            <Space wrap>
              <Checkbox
                checked={dirOverwrite}
                onChange={(e) => {
                  const next = e.target.checked
                  setDirOverwrite(next)
                  if (!next) {
                    // 取消覆盖时，把"已存在"的行从勾选里剔除
                    setDirPicked((picked) =>
                      picked.filter((name) => {
                        const item = dirEntries.find((i) => i.name === name)
                        return item !== undefined && !item.exists
                      }),
                    )
                  }
                }}
              >
                覆盖已导入资产的内容
              </Checkbox>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                已导入的行默认不可选；勾选此项可重导（会覆盖内容，但不动上传的视频地址）
              </Typography.Text>
            </Space>
            {dirResult !== null && (
              <Alert
                type={
                  dirResult.failed > 0
                    ? 'warning'
                    : dirResult.imported === 0 && dirResult.skipped === 0
                      ? 'warning'
                      : 'success'
                }
                showIcon
                message={
                  dirResult.imported === 0 && dirResult.skipped === 0 && dirResult.failed === 0
                    ? '没有执行任何导入：请确认勾选的行属于"当前目录"'
                    : `导入完成：成功 ${dirResult.imported} · 跳过 ${dirResult.skipped} · 失败 ${dirResult.failed}`
                }
                description={
                  dirResult.failed > 0 ? (
                    <div>
                      {dirResult.items
                        .filter((item) => item.status === 'failed')
                        .slice(0, 5)
                        .map((item) => (
                          <div key={item.dir_name}>
                            {item.dir_name}：{item.reason ?? '未知原因'}
                          </div>
                        ))}
                    </div>
                  ) : dirResult.imported === 0 && dirResult.skipped > 0 ? (
                    <div>
                      {dirResult.items
                        .filter((item) => item.status === 'skipped')
                        .slice(0, 5)
                        .map((item) => (
                          <div key={item.dir_name}>
                            {item.dir_name}：{item.reason ?? '已在库内'}
                          </div>
                        ))}
                    </div>
                  ) : undefined
                }
              />
            )}
            {dirResult === null && dirEntries.length === 0 && (
              <Empty description="这个目录是空的" />
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
