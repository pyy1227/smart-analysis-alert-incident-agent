import { useEffect, useRef, useState } from 'react'
import './styles.css'
import './tool-panel.css'
import './pool-panel.css'
import './dead-letter.css'

type Incident = {
  id: string
  title: string
  severity: string
  service_id: string
  environment?: string
  status: string
  report?: string
  citations?: any[]
  risk_level?: string
  action_risk?: { level: string; action?: string; reason: string } | null
  evidence_assessment?: {
    level: string
    scope: string
    usable_count: number
    total_count: number
    gaps: string[]
    limitations: string[]
    sources: { evidence_id: number; tool: string; label: string; usable: boolean }[]
  } | null
  proposed_action?: any
  approval_status?: string
  execution_result?: any
  audits?: any[]
  messages?: AgentMessage[]
  alert?: { description?: string }
}

type AgentMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  tool_calls?: { tool: string; result: unknown }[]
}

type DeadLetter = {
  id: string
  incident_id?: string | null
  stage: string
  error: string
  payload: Record<string, unknown>
  status: string
  kafka_topic: string
  kafka_partition?: number | null
  kafka_offset?: number | null
  publish_error?: string | null
  created_at?: string | null
  handled_at?: string | null
}

type ToolResult = {
  tool: string
  result: unknown
}

const actionRiskLabels: Record<string, string> = {
  none: '无建议操作', unknown: '未评估', low: '低', medium: '中', high: '高',
}
const evidenceLabels: Record<string, string> = {
  insufficient: '证据不足', partial: '已有部分证据', sufficient_for_proposal: '满足操作提案校验',
}
const deadLetterStageLabels: Record<string, string> = {
  intake: '告警接收', analysis: '事件分析', action: '操作执行',
}
const deadLetterStatusLabels: Record<string, string> = {
  publishing: '正在写入 Kafka', pending: '等待处理', publish_failed: 'Kafka 写入失败',
  replaying: '正在重新投递', replayed: '已重新投递', ignored: '已忽略',
}

const reportConclusion = (report?: string) => {
  if (!report) return '暂无分析结论'
  const current = report.match(/## 分析结论\s*\n([\s\S]*?)(?=\n###|\n##|$)/)
  return current?.[1].trim() || report.slice(0, 300)
}

type ConcurrencyState = {
  current_limit: number
  target_limit?: number
  pending_reduction?: number
  active_requests?: number
  waiting_requests?: number
  max_burst: number
}

const isConcurrencyIncident = (incident: Incident) => {
  if (incident.proposed_action?.step === 'scale_request_concurrency') return true
  const text = [incident.title, incident.alert?.description].filter(Boolean).join(' ')
  return /并发|concurrency|连接池|connection\s*pool|pool\s*(?:exhausted|exhaustion)/i.test(text)
}

export default function App() {
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [selected, setSelected] = useState<Incident | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [chatStatus, setChatStatus] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [showDeadLetters, setShowDeadLetters] = useState(false)
  const [deadLetters, setDeadLetters] = useState<DeadLetter[]>([])
  const [deadLetterBusy, setDeadLetterBusy] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [toolResult, setToolResult] = useState<ToolResult | null>(null)
  const [concurrency, setConcurrency] = useState<ConcurrencyState | null>(null)
  const [concurrencyLimit, setConcurrencyLimit] = useState('')
  const [concurrencyFetchError, setConcurrencyFetchError] = useState<string | null>(null)
  const [concurrencyActionError, setConcurrencyActionError] = useState<string | null>(null)
  const [concurrencyActionNotice, setConcurrencyActionNotice] = useState<string | null>(null)
  const concurrencyDraftEdited = useRef(false)
  const concurrencyIncidentId = useRef<string | null>(null)
  const chatStreaming = useRef(false)

  const readJson = async <T,>(res: Response): Promise<T> => {
    const text = await res.text()
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        if (!res.ok) {
          throw new Error(text.trim() || `请求失败（${res.status}）`)
        }
        throw new Error(`服务返回了无效的 JSON（${res.status}）`)
      }
    }
    if (!res.ok) {
      const detail = data && typeof data === 'object' && 'detail' in data
        ? String((data as { detail: unknown }).detail)
        : ''
      throw new Error(detail || `请求失败（${res.status}）`)
    }
    return data as T
  }

  const loadList = async () => {
    try {
      const res = await fetch('/api/incidents')
      setIncidents(await readJson<Incident[]>(res))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法加载事件列表')
    }
  }

  const loadDetail = async (id: string) => {
    try {
      const res = await fetch(`/api/incidents/${id}`)
      setSelected(await readJson<Incident>(res))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法加载事件详情')
    }
  }

  const loadDeadLetters = async () => {
    try {
      const response = await fetch('/api/dead-letters?limit=100')
      setDeadLetters(await readJson<DeadLetter[]>(response))
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法加载死信消息')
    }
  }

  const loadConcurrency = async (incidentId: string) => {
    try {
      const current = await readJson<ConcurrencyState>(await fetch('/api/concurrency'))
      // A response from the previous incident must not update the new form.
      if (concurrencyIncidentId.current !== incidentId) return
      setConcurrency(current)
      setConcurrencyFetchError(null)
      // Live metrics may refresh, but never replace a manually edited draft.
      if (!concurrencyDraftEdited.current) {
        setConcurrencyLimit(String(current.target_limit ?? current.current_limit))
      }
    } catch (err) {
      if (concurrencyIncidentId.current === incidentId) {
        setConcurrencyFetchError(err instanceof Error ? err.message : '无法读取请求并发限制器')
      }
    }
  }

  useEffect(() => {
    loadList()
    loadDeadLetters()
    const listTimer = setInterval(loadList, 5000)
    const deadLetterTimer = setInterval(loadDeadLetters, 5000)
    return () => {
      clearInterval(listTimer)
      clearInterval(deadLetterTimer)
    }
  }, [])

  // Analysis now runs asynchronously through Kafka, so keep the active incident
  // current without requiring an operator to reselect it after it is queued.
  useEffect(() => {
    if (!selected) return
    const t = setInterval(() => {
      if (!chatStreaming.current) void loadDetail(selected.id)
    }, 5000)
    return () => clearInterval(t)
  }, [selected?.id])

  const selectedIncidentId = selected?.id
  const showConcurrencyPanel = Boolean(selected && isConcurrencyIncident(selected))

  useEffect(() => {
    // Changing incidents starts a new draft; polling the same incident does not.
    concurrencyIncidentId.current = showConcurrencyPanel && selectedIncidentId ? selectedIncidentId : null
    concurrencyDraftEdited.current = false
    setConcurrency(null)
    setConcurrencyLimit('')
    setConcurrencyFetchError(null)
    setConcurrencyActionError(null)
    setConcurrencyActionNotice(null)
    if (!showConcurrencyPanel || !selectedIncidentId) return

    void loadConcurrency(selectedIncidentId)
    const timer = setInterval(() => void loadConcurrency(selectedIncidentId), 5000)
    return () => {
      clearInterval(timer)
      concurrencyIncidentId.current = null
    }
  }, [selectedIncidentId, showConcurrencyPanel])

  const decide = async (approved: boolean) => {
    if (!selected) return
    setBusy(true)
    try {
      const res = await fetch(`/api/incidents/${selected.id}/approval`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          approved,
          reason: approved ? '同意执行' : '风险过高'
        })
      })
      await readJson(res)
      await loadDetail(selected.id)
      await loadList()
      if (isConcurrencyIncident(selected)) await loadConcurrency(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作确认失败')
    } finally {
      setBusy(false)
    }
  }

  const reanalyze = async () => {
    if (!selected) return
    setBusy(true)
    try {
      await readJson(await fetch(`/api/incidents/${selected.id}/reanalyze`, { method: 'POST' }))
      await loadDetail(selected.id)
      await loadList()
    } catch (err) {
      setError(err instanceof Error ? err.message : '重新分析失败')
    } finally {
      setBusy(false)
    }
  }

  const invokeTool = async (tool: 'query_service_status' | 'query_service_metrics' | 'query_database_health' | 'query_logs' | 'search_incident_history') => {
    if (!selected) return
    setBusy(true)
    setToolResult(null)
    setError(null)
    try {
      const response = await readJson<ToolResult>(await fetch(`/api/incidents/${selected.id}/tools/${tool}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: tool === 'search_incident_history' || tool === 'query_logs' ? selected.title : '' })
      }))
      setToolResult(response)
      await loadDetail(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'MCP 工具调用失败')
    } finally {
      setBusy(false)
    }
  }

  const requestConcurrencyChange = async () => {
    if (!selected) return
    setConcurrencyActionError(null)
    setConcurrencyActionNotice(null)
    const requestedLimit = Number(concurrencyLimit)
    if (!concurrencyLimit.trim() || !Number.isInteger(requestedLimit) || requestedLimit < 1 || requestedLimit > 200) {
      setConcurrencyActionError('请输入 1 到 200 之间的整数。')
      return
    }
    setBusy(true)
    try {
      await readJson(await fetch(`/api/incidents/${selected.id}/concurrency-limit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ concurrency_limit: requestedLimit })
      }))
      // The operator may have selected another incident while the request ran.
      if (concurrencyIncidentId.current !== selected.id) {
        await loadList()
        return
      }
      setConcurrencyActionNotice(`已准备并发上限 ${requestedLimit} 的操作，请在下方确认或取消。`)
      await loadDetail(selected.id)
      await loadList()
    } catch (err) {
      if (concurrencyIncidentId.current === selected.id) {
        setConcurrencyActionError(err instanceof Error ? err.message : '无法创建并发上限调整确认')
      }
    } finally {
      setBusy(false)
    }
  }

  const requestedConcurrencyLimit = Number(concurrencyLimit)
  const isConcurrencyReduction = Boolean(concurrency && Number.isFinite(requestedConcurrencyLimit) && requestedConcurrencyLimit < concurrency.current_limit)

  const handleDeadLetter = async (item: DeadLetter, action: 'ignore' | 'replay') => {
    if (action === 'replay' && !window.confirm('确认重新投递这条死信消息？操作消息会生成新的确认 ID 和幂等键。')) return
    setDeadLetterBusy(item.id)
    setError(null)
    try {
      await readJson(await fetch(`/api/dead-letters/${item.id}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: action === 'replay' ? JSON.stringify({ confirmed: true }) : undefined,
      }))
      await loadDeadLetters()
      await loadList()
      if (item.incident_id && selected?.id === item.incident_id) await loadDetail(item.incident_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '死信消息处理失败')
    } finally {
      setDeadLetterBusy(null)
    }
  }

  const openRelatedIncident = async (incidentId?: string | null) => {
    if (!incidentId) return
    setShowDeadLetters(false)
    await loadDetail(incidentId)
  }

  const sendMessage = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!selected || !message.trim()) return
    const content = message.trim()
    const incidentId = selected.id
    const userMessageId = `local-user-${Date.now()}`
    const assistantMessageId = `stream-assistant-${Date.now()}`
    setMessage('')
    setBusy(true)
    setError(null)
    setChatStatus('正在连接事件助手…')
    chatStreaming.current = true
    setSelected(current => current?.id === incidentId ? {
      ...current,
      messages: [
        ...(current.messages || []),
        { id: userMessageId, role: 'user', content },
        { id: assistantMessageId, role: 'assistant', content: '', tool_calls: [] },
      ],
    } : current)

    try {
      const response = await fetch(`/api/incidents/${incidentId}/messages/stream`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content })
      })
      if (!response.ok) {
        const body = await response.text()
        let detail = body
        try { detail = JSON.parse(body).detail || body } catch { /* 使用原始错误文本 */ }
        throw new Error(detail || `请求失败（${response.status}）`)
      }
      if (!response.body) throw new Error('浏览器没有收到流式响应')

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      const updateAssistant = (update: Partial<AgentMessage>) => {
        setSelected(current => current?.id === incidentId ? {
          ...current,
          messages: (current.messages || []).map(item =>
            item.id === assistantMessageId ? { ...item, ...update } : item
          ),
        } : current)
      }

      const handleFrame = (frame: string) => {
        if (!frame.trim()) return
        let eventName = 'message'
        const dataLines: string[] = []
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith('event:')) eventName = line.slice(6).trim()
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
        }
        if (!dataLines.length) return
        const data = JSON.parse(dataLines.join('\n'))
        if (eventName === 'status') setChatStatus(String(data.message || '系统正在处理…'))
        if (eventName === 'tools') updateAssistant({ tool_calls: data.tool_calls || [] })
        if (eventName === 'delta') {
          const delta = String(data.content || '')
          setSelected(current => current?.id === incidentId ? {
            ...current,
            messages: (current.messages || []).map(item =>
              item.id === assistantMessageId ? { ...item, content: item.content + delta } : item
            ),
          } : current)
        }
        if (eventName === 'done' && data.message) {
          setSelected(current => current?.id === incidentId ? {
            ...current,
            messages: (current.messages || []).map(item =>
              item.id === assistantMessageId ? data.message as AgentMessage : item
            ),
          } : current)
          setChatStatus(null)
        }
        if (eventName === 'error') throw new Error(String(data.detail || '流式回答失败'))
      }

      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const frames = buffer.split(/\r?\n\r?\n/)
        buffer = frames.pop() || ''
        frames.forEach(handleFrame)
        if (done) break
      }
      if (buffer.trim()) handleFrame(buffer)
      await loadDetail(incidentId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '事件助手未能回复')
      setMessage(content)
      await loadDetail(incidentId)
    } finally {
      chatStreaming.current = false
      setChatStatus(null)
      setBusy(false)
    }
  }

  const createIncident = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setBusy(true)
    try {
      const incident = await readJson<Incident>(await fetch('/api/incidents', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: form.get('title'), description: form.get('description'), service_id: form.get('service_id'),
          severity: form.get('severity'), environment: form.get('environment')
        })
      }))
      setShowCreate(false)
      await loadList()
      await loadDetail(incident.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建事件失败')
    } finally {
      setBusy(false)
    }
  }

  const activeDeadLetterCount = deadLetters.filter(item =>
    ['pending', 'publish_failed', 'replaying'].includes(item.status)
  ).length

  return (
    <main className="shell">
      <header className="header">
        <div><p className="eyebrow">事件处理</p><h1>Sentinel <span>事件中心</span></h1></div>
        <div><span className="live">● 系统在线</span><button className={`dead-letter-button ${activeDeadLetterCount ? 'has-items' : ''}`} onClick={() => setShowDeadLetters(true)}>死信消息：{activeDeadLetterCount} 条</button><button className="primary" onClick={() => setShowCreate(true)}>＋ 新建事件</button></div>
      </header>
      <p className="summary">{incidents.length} 个活动事件 · {incidents.filter(i => i.approval_status === 'pending').length} 个等待你的确认 · 系统会收集证据并给出处理建议</p>
      {error && <p role="alert" className="error">{error}</p>}
    <div className="workspace">
      <div style={{ width: 420 }}>
        <h2>事件队列</h2>
        <button onClick={loadList}>刷新</button>
        {incidents.map((i) => (
          <div
            key={i.id}
            onClick={() => loadDetail(i.id)}
            className={selected?.id === i.id ? 'incident selected' : 'incident'}
          >
            <b>{i.title}</b>
            <div style={{ fontSize: 12, color: '#666' }}>
              {i.service_id} · {i.severity} · {i.status} · {i.approval_status || '-'}
            </div>
          </div>
        ))}
      </div>

      <div style={{ flex: 1 }}>
        <h2>事件工作台</h2>
        {!selected ? (
          <p>左侧选择一条告警</p>
        ) : (
          <div>
            <div className="case-header"><div><p className="eyebrow">{selected.service_id} / {selected.environment || 'prod'}</p><h3>{selected.title}</h3></div><button className="secondary" disabled={busy} onClick={reanalyze}>↻ 重新分析</button></div>
            <p>
              状态：{selected.status}，建议操作风险：{selected.action_risk ? actionRiskLabels[selected.action_risk.level] || '未评估' : '尚未单独评估'}，操作确认：
              {selected.approval_status || '-'}
            </p>
            <p>{selected.action_risk?.reason}</p>
            <p>证据评估：{selected.evidence_assessment ? `${evidenceLabels[selected.evidence_assessment.level] || '未评估'}（可用 ${selected.evidence_assessment.usable_count}/${selected.evidence_assessment.total_count}）` : '尚未评估，可重新分析'}</p>
            {selected.evidence_assessment && <details>
              <summary>查看证据缺口与数据来源限制</summary>
              <p>{selected.evidence_assessment.scope}。满足提案校验不代表根因已查明。</p>
              <ul>{selected.evidence_assessment.sources.map(source => <li key={source.evidence_id}>证据 {source.evidence_id}：{source.label} · {source.usable ? '有可用结果' : '不能作为当前事件证据'}</li>)}</ul>
              <ul>{selected.evidence_assessment.gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul>
              <ul>{selected.evidence_assessment.limitations.map((limitation, i) => <li key={i}>{limitation}</li>)}</ul>
            </details>}

            <h4>分析结论</h4>
            <div className="report-summary">{reportConclusion(selected.report)}</div>
            {selected.report && <details className="report-details">
              <summary>查看完整分析报告</summary>
              <pre className="analysis-report">{selected.report}</pre>
            </details>}

            <h4>引用来源</h4>
            <ul>
              {selected.citations?.map((c, i) => (
                <li key={i}>
                  {c.url
                    ? <a href={c.url} target="_blank" rel="noreferrer">{c.section}</a>
                    : <>{c.source} · {c.section}</>}
                </li>
              ))}
            </ul>

            <h4>操作计划</h4>
            <pre>{JSON.stringify(selected.proposed_action, null, 2)}</pre>

            <section className="tool-panel">
              <div className="tool-panel-head"><div><h4>MCP 工具</h4><p>只读工具可直接查询；有副作用的工具必须由你确认。日志查询本地 Docker；数据库工具查询 Prometheus 指标；历史工具查询同租户、同服务、同环境和同分类的已完成事件。</p></div><span className="tool-state">{busy ? '调用中…' : '可发起查询'}</span></div>
              <div className="tool-actions">
                <button className="secondary" disabled={busy} onClick={() => invokeTool('query_service_status')}>查询服务状态</button>
                <button className="secondary" disabled={busy} onClick={() => invokeTool('query_service_metrics')}>查询实时指标</button>
                <button className="secondary" disabled={busy} onClick={() => invokeTool('query_database_health')}>查询数据库监控指标</button>
                <button className="secondary" disabled={busy} onClick={() => invokeTool('query_logs')}>查询错误日志</button>
                <button className="secondary" disabled={busy} onClick={() => invokeTool('search_incident_history')}>检索历史事件</button>
              </div>
              {toolResult && <div className="tool-result"><b>{toolResult.tool}</b><pre>{JSON.stringify(toolResult.result, null, 2)}</pre></div>}
            </section>

            {isConcurrencyIncident(selected) && <section className="pool-panel">
              <div><h4>payment_api 支付请求并发限制器</h4><p>当前上限 {concurrency?.current_limit ?? '—'}，目标上限 {concurrency?.target_limit ?? concurrency?.current_limit ?? '—'}，活跃请求 {concurrency?.active_requests ?? '—'}，等待请求 {concurrency?.waiting_requests ?? '—'}{concurrency?.pending_reduction ? `，待减少 ${concurrency.pending_reduction}` : ''}。这是应用请求信号量，不是 SQLAlchemy/MySQL 连接池；提交后由你确认执行。</p></div>
              <div className="pool-form">
                <label>请求并发上限<input type="number" min="1" max="200" value={concurrencyLimit} onChange={(event) => { concurrencyDraftEdited.current = true; setConcurrencyLimit(event.target.value) }} /></label>
                <button className="primary" type="button" disabled={busy || selected.approval_status === 'pending'} onClick={requestConcurrencyChange}>{isConcurrencyReduction ? '申请降低上限' : '申请提高上限'}</button>
              </div>
              {concurrencyFetchError && <p role="alert" className="pool-error">并发限制状态读取失败：{concurrencyFetchError}</p>}
              {concurrencyActionError && <p role="alert" className="pool-error">申请失败：{concurrencyActionError}</p>}
              {concurrencyActionNotice && selected.approval_status === 'pending' && <p role="status" className="pool-note">{concurrencyActionNotice}</p>}
              {selected.approval_status === 'pending' && <p className="pool-note">当前已有待确认操作，请先确认或取消后再提交并发上限调整。</p>}
            </section>}

            {selected.approval_status === 'pending' && (
              <div className="approval">
                <b>此操作需要你的确认</b><p>系统不会自行执行会修改状态的操作。</p>
                <button className="primary" disabled={busy} onClick={() => decide(true)}>{busy ? '执行并刷新中…' : '确认并执行'}</button>{' '}
                <button className="danger" disabled={busy} onClick={() => decide(false)}>取消</button>
              </div>
            )}

            <section className="chat">
              <h4>事件问答</h4>
              {(selected.messages || []).length === 0 && <p className="hint">可询问“服务状态如何？”、“有类似历史事件吗？”或要求解释处置建议。</p>}
              {selected.messages?.map((item) => (
                <article key={item.id} className={`message ${item.role}`}>
                  <b>{item.role === 'assistant' ? '事件助手' : '你'}</b>
                  <p>{item.content || (item.id.startsWith('stream-assistant-') ? '正在准备回答…' : '')}</p>
                  {item.tool_calls?.map((tool, index) => <details key={index}><summary>调用工具：{tool.tool}</summary><pre>{JSON.stringify(tool.result, null, 2)}</pre></details>)}
                </article>
              ))}
              {chatStatus && <p className="hint" role="status">{chatStatus}</p>}
              <form className="composer" onSubmit={sendMessage}>
                <textarea rows={2} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="追问原因或补充现场信息…" />
                <button className="primary" disabled={busy}>{busy ? '处理中…' : '发送'}</button>
              </form>
            </section>

            <h4>执行结果</h4>
            <pre>{JSON.stringify(selected.execution_result, null, 2)}</pre>

            <h4>审计记录</h4>
            <pre>{JSON.stringify(selected.audits, null, 2)}</pre>
          </div>
        )}
      </div>
    </div>
    {showCreate && <div className="modal-backdrop"><form className="modal" onSubmit={createIncident}>
      <button type="button" className="close" onClick={() => setShowCreate(false)}>×</button>
      <p className="eyebrow">新建事件</p><h2>提交事件分析</h2>
      <label>事件标题<input name="title" required placeholder="支付接口 P99 延迟升高" /></label>
      <label>受影响服务<input name="service_id" required defaultValue="payment-api" /></label>
      <div className="form-row"><label>严重级别<select name="severity" defaultValue="warning"><option value="warning">warning</option><option value="critical">critical</option></select></label><label>环境<select name="environment" defaultValue="prod"><option value="prod">prod</option><option value="staging">staging</option></select></label></div>
      <label>告警上下文<textarea name="description" required rows={4} placeholder="粘贴指标、日志或受影响范围…" /></label>
      <button className="primary wide" disabled={busy}>{busy ? '正在分析…' : '开始分析'}</button>
    </form></div>}
    {showDeadLetters && <div className="modal-backdrop"><section className="dead-letter-modal">
      <button type="button" className="close" onClick={() => setShowDeadLetters(false)}>×</button>
      <div className="dead-letter-title"><div><p className="eyebrow">失败消息</p><h2>死信消息</h2></div><button className="secondary" onClick={loadDeadLetters}>刷新</button></div>
      {deadLetters.length === 0 && <p className="hint">当前没有死信消息。</p>}
      <div className="dead-letter-list">{deadLetters.map(item => {
        const active = ['pending', 'publish_failed'].includes(item.status)
        return <article className="dead-letter-card" key={item.id}>
          <div className="dead-letter-card-head"><div><b>{deadLetterStageLabels[item.stage] || item.stage}</b><span className={`dead-letter-status ${item.status}`}>{deadLetterStatusLabels[item.status] || item.status}</span></div><time>{item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : '时间未知'}</time></div>
          <p><strong>关联事件：</strong>{item.incident_id ? <button className="link-button" onClick={() => openRelatedIncident(item.incident_id)}>{item.incident_id}</button> : '无'}</p>
          <p><strong>失败原因：</strong>{item.error}</p>
          <p><strong>Kafka：</strong>{item.kafka_topic}{item.kafka_partition != null ? ` / 分区 ${item.kafka_partition} / 位点 ${item.kafka_offset}` : ''}</p>
          {item.publish_error && <p className="dead-letter-error"><strong>投递错误：</strong>{item.publish_error}</p>}
          <details><summary>查看原始消息</summary><pre>{JSON.stringify(item.payload, null, 2)}</pre></details>
          {active && <div className="dead-letter-actions"><button className="secondary" disabled={deadLetterBusy === item.id} onClick={() => handleDeadLetter(item, 'ignore')}>忽略</button><button className="primary" disabled={deadLetterBusy === item.id} onClick={() => handleDeadLetter(item, 'replay')}>{deadLetterBusy === item.id ? '处理中…' : '本人确认后重新投递'}</button></div>}
        </article>
      })}</div>
    </section></div>}
    </main>
  )
}
