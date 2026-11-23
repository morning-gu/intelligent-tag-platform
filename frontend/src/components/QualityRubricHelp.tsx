import { Button, Modal, Table, Tabs, Typography } from 'antd'
import { useState } from 'react'

const relevance = [
  '材料直接反驳核心概念或必要限定，或可核查的材料里没有相关对象、事件与属性。',
  '只有相关对象、场所或话题。核心动作、属性或关系无明确记录，也无法从材料推出。',
  '核心概念有明确依据，但至少一个必要限定缺失，或全片级概括只由同一事件支撑。',
  '核心与全部必要限定均有依据，证据覆盖相应层级。需要从材料里的细节完成一步推理。',
  '核心与全部必要限定均能从材料直接定位，证据覆盖相应层级。',
]

const semantics = [
  '核心含义相反，或名称与释义没有可对应的概念。',
  '共享话题，但中心指代或类别不同。例如把音乐类型定义成一类节目。',
  '核心概念相同，适用边界不同。应举出一个只符合名称或只符合释义的实例。',
  '适用边界一致，但释义仅换词复述名称，或只给了本片例子。',
  '适用边界一致，且释义写出对象及可跨片判断的区分条件。',
]

const value = [
  {
    scene: '离开剧情后无法解释成可检索的内容。',
    film: '名称依赖本片剧情才能理解，无法独立表达影片属性。',
    character: '离开剧情后无法解释成独立属性。',
  },
  {
    scene: '只表达有无、编号或泛泛评价，无法排除常见无关片段。',
    film: '只传达属性的有无、泛泛评价或单一局部动作，提供的全片信息很有限。',
    character: '只表达有无、编号或泛泛评价，无法筛掉常见无关项。',
  },
  {
    scene: '只写出普通事件类目，或需要改词序、补主体。例如国际象棋对弈。',
    film: '只给出宽泛的选片方向，观众难以形成清楚的题材、叙事或视听预期，或名称需要改写。',
    character: '只能筛到泛类目，或名称需要改写。',
  },
  {
    scene: '以自然词序写出事件或视听现象，且有恰好一项可核查的独立限定。例如AI下国际象棋。',
    film: '名称自然，准确传达有据的次要属性，补充观众的观影预期。',
    character: '名称自然，描述有据的具体次要属性。',
  },
  {
    scene: '以自然词序写出事件或视听现象，且有至少两项可核查的独立限定。例如AI拒开舱门。',
    film: '名称自然，准确传达核心题材、主要叙事类型、主线、主要冲突或主导视听体验。写明观影预期及至少两个独立段落如何各自支撑它。',
    character: '名称直接指出主线、主要冲突、主导视听风格，或人物贯穿剧情的身份与行为。至少用两个独立段落说明它怎样改变预期。',
  },
]

function CriteriaTable({ criteria }: { criteria: string[] }) {
  return <Table
    size="small"
    pagination={false}
    rowKey="score"
    dataSource={criteria.map((text, score) => ({ score, text }))}
    columns={[
      { title: '分数', dataIndex: 'score', width: 62 },
      { title: '判定标准', dataIndex: 'text' },
    ]}
  />
}

export default function QualityRubricHelp() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button type="link" size="small" onClick={() => setOpen(true)}>评分标准</Button>
      <Modal title="五档标签评分标准" open={open} onCancel={() => setOpen(false)}
        footer={null} width={840} destroyOnHidden>
        <Typography.Paragraph>
          每个维度独立按 0–4 分判定，从低分档往上核对，取首个符合条件的分档。材料不足以区分相邻档时记为「无法判断」。
        </Typography.Paragraph>
        <Tabs items={[
          {
            key: 'relevance', label: '内容相关性',
            children: <>
              <CriteriaTable criteria={relevance} />
              <Typography.Paragraph className="task-tag-drawer-section">
                场景词的 3–4 分要引用来源场景。全片词的 3–4 分要有至少两个独立剧情或视听段落。同一事件的发生与解决、同一转场的两侧，都按一个段落计。人物词以指定人物为评价对象。
              </Typography.Paragraph>
            </>,
          },
          {
            key: 'semantics', label: '语义准确性',
            children: <>
              <CriteriaTable criteria={semantics} />
              <Typography.Paragraph className="task-tag-drawer-section">
                判断范围以名称和最终释义为准。缺少最终释义时，记为「无法判断」。
              </Typography.Paragraph>
            </>,
          },
          {
            key: 'value', label: '标签价值',
            children: <>
              <Table size="small" pagination={false} rowKey="score"
                dataSource={value.map((row, score) => ({ score, ...row }))}
                columns={[
                  { title: '分数', dataIndex: 'score', width: 62 },
                  { title: '场景搜索', dataIndex: 'scene', width: '30%' },
                  { title: '全片展示', dataIndex: 'film' },
                  { title: '人物', dataIndex: 'character', width: '25%' },
                ]} />
              <Typography.Paragraph className="task-tag-drawer-section">
                全片标签按名称让观众形成的预期及其在本片中的重要性评分。常见题材和类型采用同一标准，与封闭标签的重复另行判断。历史 v4 评估先按名称具体性判断全片标签能否升至 3–4 分。
              </Typography.Paragraph>
              <Typography.Paragraph>
                场景只计算名称写明且证据支持的限定。可计入参与者、对象、地点、时间、动作方式或可识别的视听细节。通用参与者、事件必然包含的信息和同义重复均不构成额外限定。人物标签达到 3–4 分须能说明名称排除了哪些同类内容。
              </Typography.Paragraph>
            </>,
          },
        ]} />
      </Modal>
    </>
  )
}
