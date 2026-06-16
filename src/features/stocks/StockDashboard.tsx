import { useEffect, useMemo, useState } from 'react'
import { stockRepository } from './repository'
import type {
  FineStock,
  HoldingStock,
  OverviewStats,
  RealtimeDecision,
  RoughStock,
  SignalEvent,
  TaskRecord,
  TradeAction,
  TradeRecord,
} from './types'
import './stocks.css'

type StockRow = RoughStock | FineStock | RealtimeDecision | HoldingStock | TradeRecord | TaskRecord | SignalEvent
type TabId = 'signals' | 'live' | 'holdings' | 'rough' | 'fine' | 'history' | 'trades' | 'tasks'

interface Column<T> {
  header: string
  cell: (row: T) => React.ReactNode
  align?: 'right'
}

function formatPrice(value: number | null) {
  return value === null ? '-' : value.toFixed(2)
}

function ColorNumber({ value, suffix = '' }: { value: number; suffix?: string }) {
  const className = value > 0 ? 'stock-up' : value < 0 ? 'stock-down' : 'stock-flat'
  const prefix = value > 0 ? '+' : ''
  return <span className={className}>{prefix}{value.toFixed(2)}{suffix}</span>
}

function StatusTag({ status }: { status: string }) {
  const tone = status.includes('止损') ? 'risk' : status.includes('止盈') ? 'profit' : status.includes('可买') ? 'buy' : status.includes('成功') ? 'ok' : 'neutral'
  return <span className={`stock-status stock-status-${tone}`}>{status}</span>
}

function DataTable<T extends StockRow>({
  columns,
  data,
  onRowClick,
}: {
  columns: Column<T>[]
  data: T[]
  onRowClick?: (row: T) => void
}) {
  return (
    <div className="stock-table-wrap">
      <table className="stock-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.header} className={column.align === 'right' ? 'right' : undefined}>{column.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, index) => (
            <tr key={`${'code' in row ? row.code : 'id' in row ? row.id : index}-${index}`} onClick={() => onRowClick?.(row)}>
              {columns.map((column) => (
                <td key={column.header} className={column.align === 'right' ? 'right' : undefined}>{column.cell(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length === 0 && <div className="stock-empty">暂无数据</div>}
    </div>
  )
}

export default function StockDashboard() {
  const [activeTab, setActiveTab] = useState<TabId>('signals')
  const [loading, setLoading] = useState(true)
  const [selectedStock, setSelectedStock] = useState<StockRow | null>(null)
  const [tradeModal, setTradeModal] = useState<{ action: TradeAction; stock: HoldingStock } | null>(null)
  const [showAddHoldingModal, setShowAddHoldingModal] = useState(false)
  const [savedMessage, setSavedMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [autoRefresh, setAutoRefresh] = useState(false)

  const [overview, setOverview] = useState<OverviewStats | null>(null)
  const [roughStocks, setRoughStocks] = useState<RoughStock[]>([])
  const [fineStocks, setFineStocks] = useState<FineStock[]>([])
  const [realtime, setRealtime] = useState<RealtimeDecision[]>([])
  const [holdings, setHoldings] = useState<HoldingStock[]>([])
  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [signals, setSignals] = useState<SignalEvent[]>([])
  const [historicalFineStocks, setHistoricalFineStocks] = useState<FineStock[]>([])

  async function loadStockData() {
    const [stats, rough, fine, live, currentHoldings, tradeRecords, taskRecords, signalEvents, historicalPicks] = await Promise.all([
      stockRepository.getOverview(),
      stockRepository.getRoughStocks(),
      stockRepository.getFineStocks(),
      stockRepository.getRealtimeDecisions(),
      stockRepository.getHoldings(),
      stockRepository.getTradeRecords(),
      stockRepository.getTasks(),
      stockRepository.getSignalEvents(),
      stockRepository.getHistoricalFineStocks(),
    ])
    setOverview(stats)
    setRoughStocks(rough)
    setFineStocks(fine)
    setRealtime(live)
    setHoldings(currentHoldings)
    setTrades(tradeRecords)
    setTasks(taskRecords)
    setSignals(signalEvents)
    setHistoricalFineStocks(historicalPicks)
  }

  useEffect(() => {
    let mounted = true
    async function load() {
      setLoading(true)
      const [stats, rough, fine, live, currentHoldings, tradeRecords, taskRecords, signalEvents, historicalPicks] = await Promise.all([
        stockRepository.getOverview(),
        stockRepository.getRoughStocks(),
        stockRepository.getFineStocks(),
        stockRepository.getRealtimeDecisions(),
        stockRepository.getHoldings(),
        stockRepository.getTradeRecords(),
        stockRepository.getTasks(),
        stockRepository.getSignalEvents(),
        stockRepository.getHistoricalFineStocks(),
      ])
      if (!mounted) return
      setOverview(stats)
      setRoughStocks(rough)
      setFineStocks(fine)
      setRealtime(live)
      setHoldings(currentHoldings)
      setTrades(tradeRecords)
      setTasks(taskRecords)
      setSignals(signalEvents)
      setHistoricalFineStocks(historicalPicks)
      setLoading(false)
    }
    void load()
    return () => {
      mounted = false
    }
  }, [])

  useEffect(() => {
    if (!autoRefresh) return undefined
    const timer = window.setInterval(() => {
      void loadStockData()
    }, 60000)
    return () => window.clearInterval(timer)
  }, [autoRefresh])

  const tabs = useMemo(() => [
    { id: 'signals' as const, label: '信号中心' },
    { id: 'live' as const, label: '盘中实时决策' },
    { id: 'holdings' as const, label: '当前持仓' },
    { id: 'rough' as const, label: '今日海选' },
    { id: 'fine' as const, label: '今日精选' },
    { id: 'history' as const, label: '历史精选' },
    { id: 'trades' as const, label: '清仓复盘' },
    { id: 'tasks' as const, label: '任务中心' },
  ], [])

  const roughColumns: Column<RoughStock>[] = [
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '排名分', cell: (row) => row.score, align: 'right' },
    { header: '昨收', cell: (row) => formatPrice(row.prevClose), align: 'right' },
    { header: '信号', cell: (row) => row.signal },
    { header: '动作', cell: (row) => row.action },
    { header: '支撑', cell: (row) => formatPrice(row.supportLevel), align: 'right' },
    { header: '压力', cell: (row) => formatPrice(row.resistanceLevel), align: 'right' },
    { header: '止损', cell: (row) => formatPrice(row.stopLoss), align: 'right' },
    { header: '主要风险', cell: (row) => row.risk },
  ]

  const fineColumns: Column<FineStock>[] = [
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '策略等级', cell: (row) => row.strategyLevel },
    { header: '排名分', cell: (row) => row.score, align: 'right' },
    { header: '信号', cell: (row) => row.signal },
    { header: '动作', cell: (row) => row.action },
    { header: '复核', cell: (row) => row.reviewStatus },
  ]

  const realtimeColumns: Column<RealtimeDecision>[] = [
    { header: '更新时间', cell: (row) => row.updateTime },
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '状态', cell: (row) => <StatusTag status={row.status} /> },
    { header: '当前价', cell: (row) => formatPrice(row.currentPrice), align: 'right' },
    { header: '涨跌幅', cell: (row) => <ColorNumber value={row.changeRate} suffix="%" />, align: 'right' },
    { header: '建议买入', cell: (row) => formatPrice(row.suggestBuyPrice), align: 'right' },
    { header: '建议卖出', cell: (row) => formatPrice(row.suggestSellPrice), align: 'right' },
    { header: '止损位', cell: (row) => formatPrice(row.stopLoss), align: 'right' },
    { header: '最终动作', cell: (row) => row.finalAction },
  ]

  const holdingColumns: Column<HoldingStock>[] = [
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '成本价', cell: (row) => formatPrice(row.costPrice), align: 'right' },
    { header: '当前价', cell: (row) => formatPrice(row.currentPrice), align: 'right' },
    { header: '股数', cell: (row) => row.shares, align: 'right' },
    { header: '市值', cell: (row) => row.marketValue.toFixed(0), align: 'right' },
    { header: '浮盈亏', cell: (row) => <ColorNumber value={row.floatingPnl} />, align: 'right' },
    { header: '盈亏率', cell: (row) => <ColorNumber value={row.pnlRate} suffix="%" />, align: 'right' },
    { header: '当前建议', cell: (row) => row.currentSuggestion },
    {
      header: '操作',
      cell: (row) => (
        <div className="stock-row-actions">
          {(['加仓', '减仓', '清仓'] as TradeAction[]).map((action) => (
            <button key={action} type="button" onClick={(event) => {
              event.stopPropagation()
              setTradeModal({ action, stock: row })
            }}>{action}</button>
          ))}
        </div>
      ),
    },
  ]

  const tradeColumns: Column<TradeRecord>[] = [
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '买入日', cell: (row) => row.buyDate },
    { header: '卖出日', cell: (row) => row.sellDate },
    { header: '成本', cell: (row) => formatPrice(row.costPrice), align: 'right' },
    { header: '卖出价', cell: (row) => formatPrice(row.sellPrice), align: 'right' },
    { header: '盈亏', cell: (row) => <ColorNumber value={row.pnlAmount} />, align: 'right' },
    { header: '盈亏率', cell: (row) => <ColorNumber value={row.pnlRate} suffix="%" />, align: 'right' },
    { header: '卖出说明', cell: (row) => row.sellMemo },
  ]

  const taskColumns: Column<TaskRecord>[] = [
    { header: '任务类型', cell: (row) => row.type },
    { header: '状态', cell: (row) => <StatusTag status={row.status} /> },
    { header: '开始时间', cell: (row) => row.startTime },
    { header: '结束时间', cell: (row) => row.endTime },
    { header: '导入条数', cell: (row) => row.importedCount, align: 'right' },
  ]

  const signalColumns: Column<SignalEvent>[] = [
    { header: '触发时间', cell: (row) => row.signalTime },
    { header: '类型', cell: (row) => <StatusTag status={row.signalType} /> },
    { header: '状态', cell: (row) => row.status },
    { header: '来源', cell: (row) => row.sourceType },
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '触发价', cell: (row) => formatPrice(row.triggerPrice), align: 'right' },
    { header: '涨跌幅', cell: (row) => <ColorNumber value={row.changeRate} suffix="%" />, align: 'right' },
    { header: '买入计划', cell: (row) => row.buyPriceText },
    { header: '卖出计划', cell: (row) => row.sellPriceText },
    { header: '动作', cell: (row) => signalActions(row) },
  ]

  const historyColumns: Column<FineStock>[] = [
    { header: '入选日期', cell: (row) => row.date },
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '策略等级', cell: (row) => row.strategyLevel },
    { header: '排名分', cell: (row) => row.score, align: 'right' },
    { header: '昨收', cell: (row) => formatPrice(row.prevClose), align: 'right' },
    { header: '信号', cell: (row) => row.signal },
    { header: '入选理由', cell: (row) => row.reason },
    { header: '风险', cell: (row) => row.risk },
  ]

  function numberPrompt(message: string, fallback: number) {
    const value = window.prompt(message, String(fallback))
    if (value === null) return null
    const parsed = Number(value)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }

  function todayInputValue() {
    return new Date().toISOString().slice(0, 10)
  }

  async function handleSignalBuy(signal: SignalEvent) {
    const shares = numberPrompt('确认线下买入股数', 100)
    if (!shares) return
    const price = numberPrompt('确认线下买入价格', signal.triggerPrice || signal.currentPrice)
    if (!price) return
    setErrorMessage('')
    try {
      const holding = await stockRepository.confirmSignalBuy({
        signal,
        shares,
        price,
        buyDate: todayInputValue(),
        memo: signal.reason || signal.finalAction,
      })
      setHoldings((items) => [holding, ...items])
      setSignals((items) => items.map((item) => item.id === signal.id ? { ...item, status: '已买入' } : item))
      setSavedMessage(`${signal.name} 已记录为持仓。`)
    } catch (reason) {
      setErrorMessage(reason instanceof Error ? reason.message : '信号买入记录失败')
    }
  }

  async function handleSignalSell(signal: SignalEvent) {
    const holding = holdings.find((item) => item.code === signal.code)
    if (!holding) {
      setErrorMessage('没有找到对应持仓，请先刷新或手动检查当前持仓。')
      return
    }
    const price = numberPrompt('确认线下卖出价格', signal.triggerPrice || holding.currentPrice)
    if (!price) return
    setErrorMessage('')
    try {
      const result = await stockRepository.saveTrade({
        action: '清仓',
        holding,
        price,
        shares: holding.shares,
        tradeDate: todayInputValue(),
        memo: signal.finalAction || signal.sellPriceText,
      })
      await stockRepository.markSignalEvent(signal.id, '已卖出')
      setHoldings((items) => items.filter((item) => item.code !== signal.code))
      if (result.tradeRecord) setTrades((items) => [result.tradeRecord as TradeRecord, ...items])
      setSignals((items) => items.map((item) => item.id === signal.id ? { ...item, status: '已卖出' } : item))
      setSavedMessage(`${signal.name} 已记录清仓复盘。`)
    } catch (reason) {
      setErrorMessage(reason instanceof Error ? reason.message : '信号卖出记录失败')
    }
  }

  async function handleSignalT(signal: SignalEvent) {
    const holding = holdings.find((item) => item.code === signal.code)
    if (!holding) {
      setErrorMessage('做 T 需要已有持仓。')
      return
    }
    const action = signal.signalType === '做T卖' ? '做T卖' : '做T买'
    const shares = numberPrompt(`确认${action}股数`, 100)
    if (!shares) return
    const price = numberPrompt(`确认${action}价格`, signal.triggerPrice || holding.currentPrice)
    if (!price) return
    setErrorMessage('')
    try {
      const result = await stockRepository.recordTTrade({
        signal,
        holding,
        action,
        price,
        shares,
        tradeDate: todayInputValue(),
        memo: signal.finalAction || action,
      })
      setHoldings((items) => {
        if (!result.holding) return items.filter((item) => item.code !== holding.code)
        return items.map((item) => item.code === holding.code ? result.holding as HoldingStock : item)
      })
      if (result.tradeRecord) setTrades((items) => [result.tradeRecord as TradeRecord, ...items])
      setSignals((items) => items.map((item) => item.id === signal.id ? { ...item, status: '已记录T' } : item))
      setSavedMessage(`${signal.name} 已记录${action}。`)
    } catch (reason) {
      setErrorMessage(reason instanceof Error ? reason.message : '做 T 记录失败')
    }
  }

  async function handleIgnoreSignal(signal: SignalEvent) {
    setErrorMessage('')
    try {
      await stockRepository.markSignalEvent(signal.id, '已忽略')
      setSignals((items) => items.map((item) => item.id === signal.id ? { ...item, status: '已忽略' } : item))
      setSavedMessage('信号已忽略。')
    } catch (reason) {
      setErrorMessage(reason instanceof Error ? reason.message : '信号忽略失败')
    }
  }

  function signalActions(signal: SignalEvent) {
    if (signal.status !== '新信号') return <span>-</span>
    return (
      <div className="stock-row-actions">
        {signal.signalType === '买入' && <button type="button" onClick={(event) => { event.stopPropagation(); void handleSignalBuy(signal) }}>买入</button>}
        {(['卖出', '减仓', '止损', '止盈'] as string[]).includes(signal.signalType) && <button type="button" onClick={(event) => { event.stopPropagation(); void handleSignalSell(signal) }}>清仓</button>}
        {(['做T买', '做T卖'] as string[]).includes(signal.signalType) && <button type="button" onClick={(event) => { event.stopPropagation(); void handleSignalT(signal) }}>记录T</button>}
        <button type="button" onClick={(event) => { event.stopPropagation(); void handleIgnoreSignal(signal) }}>忽略</button>
      </div>
    )
  }

  async function submitTrade(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!tradeModal) return
    setErrorMessage('')
    const form = new FormData(event.currentTarget)
    try {
      const result = await stockRepository.saveTrade({
        action: tradeModal.action,
        holding: tradeModal.stock,
        price: Number(form.get('price') || 0),
        shares: Number(form.get('shares') || 0),
        tradeDate: String(form.get('tradeDate') || todayInputValue()),
        memo: String(form.get('memo') || '').trim(),
      })
      setHoldings((items) => {
        if (!result.holding) return items.filter((item) => item.code !== tradeModal.stock.code)
        return items.map((item) => item.code === tradeModal.stock.code ? result.holding as HoldingStock : item)
      })
      if (result.tradeRecord) {
        setTrades((items) => [result.tradeRecord as TradeRecord, ...items])
        setActiveTab('trades')
      }
      setTradeModal(null)
      setSavedMessage(`${tradeModal.action}记录已保存。`)
    } catch (reason) {
      setErrorMessage(reason instanceof Error ? reason.message : '保存失败')
    }
  }

  async function submitHolding(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setErrorMessage('')
    try {
      const costPrice = Number(form.get('costPrice') || 0)
      const shares = Number(form.get('shares') || 0)
      const currentPrice = Number(form.get('currentPrice') || costPrice)
      const newHolding = await stockRepository.addHolding({
        code: String(form.get('code') || '').trim(),
        name: String(form.get('name') || '').trim(),
        costPrice,
        shares,
        currentPrice,
        buyDate: String(form.get('buyDate') || todayInputValue()),
        currentSuggestion: String(form.get('currentSuggestion') || '手动新增，等待策略同步').trim(),
      })

      setHoldings((items) => [newHolding, ...items])
      setShowAddHoldingModal(false)
      setSavedMessage('持仓已保存。')
    } catch (reason) {
      setErrorMessage(reason instanceof Error ? reason.message : '保存失败')
    }
  }

  function explainLocalScript(action: 'night' | 'live' | 'sync') {
    setErrorMessage('')
    const jobTypes = {
      night: 'night_scan' as const,
      live: 'live_decision' as const,
      sync: 'sync_latest' as const,
    }
    const labels = {
      night: '夜间筛选',
      live: '实时决策',
      sync: '同步数据库',
    }
    stockRepository.requestJob(jobTypes[action])
      .then(() => {
        setSavedMessage(`已提交${labels[action]}任务。GitHub Actions 会在几分钟内执行，稍后刷新即可查看结果。`)
      })
      .catch((reason) => {
        setErrorMessage(reason instanceof Error ? reason.message : '任务提交失败')
      })
  }

  async function toggleAutoRefresh() {
    const next = !autoRefresh
    setAutoRefresh(next)
    if (next) {
      await loadStockData()
      setSavedMessage('已开启每分钟刷新：网页会每 60 秒重新读取数据库数据。')
    } else {
      setSavedMessage('已关闭每分钟刷新。')
    }
  }

  if (loading) {
    return <section className="stock-dashboard stock-loading">正在同步策略工作台数据...</section>
  }

  return (
    <section className="stock-dashboard">
      <header className="stock-hero">
        <div>
          <span className="eyebrow">STOCK STRATEGY DESK</span>
          <h1>股票策略助手</h1>
          <p>“市场短期是投票机，长期是称重机。” 今日只做记录、复核和纪律提醒。</p>
        </div>
        <div className="stock-risk-note">不接证券账户，不自动下单。所有买卖仅作为个人记录和复盘依据。</div>
      </header>

      <div className="stock-stats">
        {[
          ['今日海选', overview?.roughCount],
          ['今日精选', overview?.fineCount],
          ['当前持仓', holdings.length],
          ['可买入', overview?.buyableCount],
          ['风控提醒', overview?.alertCount],
          ['新信号', signals.filter((item) => item.status === '新信号').length],
        ].map(([label, value]) => (
          <article key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </article>
        ))}
      </div>

      <div className="stock-workbench">
        <div className="stock-toolbar">
          <div>
            <b>最后更新</b>
            <span>{overview?.lastUpdateTime}</span>
          </div>
          <input aria-label="搜索股票" placeholder="搜索代码 / 名称" />
        </div>

        <div className="stock-tabs" role="tablist" aria-label="股票工作台视图">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              className={activeTab === tab.id ? 'active' : ''}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="stock-panel">
          {activeTab === 'live' && <DataTable columns={realtimeColumns} data={realtime} onRowClick={setSelectedStock} />}
          {activeTab === 'signals' && (
            <div className="stock-section-stack">
              <div className="stock-panel-heading">
                <div>
                  <h2>信号中心</h2>
                  <p>自动盯盘产生的买入、卖出、止损、止盈和做 T 提醒；按钮只记录你的线下操作。</p>
                </div>
              </div>
              <DataTable columns={signalColumns} data={signals} onRowClick={setSelectedStock} />
            </div>
          )}
          {activeTab === 'holdings' && (
            <div className="stock-section-stack">
              <div className="stock-panel-heading">
                <div>
                  <h2>当前持仓</h2>
                  <p>手动记录线下买入后的持仓，也可以先写自己的跟踪建议。</p>
                </div>
                <button type="button" onClick={() => setShowAddHoldingModal(true)}>新增持仓</button>
              </div>
              <DataTable columns={holdingColumns} data={holdings} onRowClick={setSelectedStock} />
            </div>
          )}
          {activeTab === 'rough' && <DataTable columns={roughColumns} data={roughStocks} onRowClick={setSelectedStock} />}
          {activeTab === 'fine' && <DataTable columns={fineColumns} data={fineStocks} onRowClick={setSelectedStock} />}
          {activeTab === 'history' && <DataTable columns={historyColumns} data={historicalFineStocks} onRowClick={setSelectedStock} />}
          {activeTab === 'trades' && <DataTable columns={tradeColumns} data={trades} onRowClick={setSelectedStock} />}
          {activeTab === 'tasks' && (
            <div className="stock-task-panel">
              <div className="stock-task-actions">
                <button type="button" onClick={() => explainLocalScript('night')}>运行夜间筛选</button>
                <button type="button" onClick={() => explainLocalScript('live')}>运行实时决策</button>
                <button type="button" onClick={() => explainLocalScript('sync')}>同步到数据库</button>
                <button type="button" onClick={() => void toggleAutoRefresh()}>{autoRefresh ? '停止每分钟刷新' : '每分钟刷新'}</button>
              </div>
              <DataTable columns={taskColumns} data={tasks} />
            </div>
          )}
        </div>
      </div>

      {savedMessage && <div className="stock-toast">{savedMessage}</div>}
      {errorMessage && <div className="stock-toast stock-toast-error">{errorMessage}</div>}

      {tradeModal && (
        <div className="stock-modal-backdrop" role="dialog" aria-modal="true" aria-label={`记录${tradeModal.action}`}>
          <form className="stock-modal" onSubmit={submitTrade}>
            <h2>记录{tradeModal.action}</h2>
            <p>{tradeModal.stock.name} {tradeModal.stock.code}</p>
            <label>交易价格<input name="price" type="number" step="0.01" defaultValue={tradeModal.stock.currentPrice} required /></label>
            <label>交易股数<input name="shares" type="number" step="100" max={tradeModal.action === '加仓' ? undefined : tradeModal.stock.shares} defaultValue={tradeModal.action === '清仓' ? tradeModal.stock.shares : 100} required /></label>
            <label>交易日期<input name="tradeDate" type="date" defaultValue={todayInputValue()} required /></label>
            <label>买卖说明<textarea name="memo" placeholder="记录本次操作依据、纪律或复盘备注" /></label>
            <div className="stock-modal-actions">
              <button type="button" onClick={() => setTradeModal(null)}>取消</button>
              <button type="submit">保存记录</button>
            </div>
          </form>
        </div>
      )}

      {showAddHoldingModal && (
        <div className="stock-modal-backdrop" role="dialog" aria-modal="true" aria-label="新增持仓">
          <form className="stock-modal" onSubmit={submitHolding}>
            <h2>新增持仓</h2>
            <p>先手动记录，后续接数据库后会长期保存。</p>
            <label>股票代码<input name="code" required /></label>
            <label>股票名称<input name="name" required /></label>
            <label>成本价<input name="costPrice" type="number" step="0.01" defaultValue="0" required /></label>
            <label>当前价<input name="currentPrice" type="number" step="0.01" placeholder="不填默认等于成本价" /></label>
            <label>持仓股数<input name="shares" type="number" step="100" defaultValue="100" required /></label>
            <label>买入日期<input name="buyDate" type="date" defaultValue={todayInputValue()} required /></label>
            <label>当前建议<textarea name="currentSuggestion" placeholder="例如：等待回踩，不追高" /></label>
            <div className="stock-modal-actions">
              <button type="button" onClick={() => setShowAddHoldingModal(false)}>取消</button>
              <button type="submit">保存持仓</button>
            </div>
          </form>
        </div>
      )}

      {selectedStock && (
        <aside className="stock-drawer">
          <button type="button" className="stock-drawer-close" onClick={() => setSelectedStock(null)}>×</button>
          <h2>{'name' in selectedStock ? selectedStock.name : '任务详情'}</h2>
          {'code' in selectedStock && <p className="stock-code">{selectedStock.code}</p>}
          <dl>
            {'date' in selectedStock && <><dt>日期</dt><dd>{selectedStock.date}</dd></>}
            {'finalAction' in selectedStock && <><dt>最终动作</dt><dd>{selectedStock.finalAction}</dd></>}
            {'signalType' in selectedStock && <><dt>信号类型</dt><dd>{selectedStock.signalType}</dd></>}
            {'buyPriceText' in selectedStock && <><dt>买入计划</dt><dd>{selectedStock.buyPriceText}</dd></>}
            {'sellPriceText' in selectedStock && <><dt>卖出计划</dt><dd>{selectedStock.sellPriceText}</dd></>}
            {'reason' in selectedStock && <><dt>入选理由</dt><dd>{selectedStock.reason}</dd></>}
            {'sellMemo' in selectedStock && <><dt>卖出说明</dt><dd>{selectedStock.sellMemo}</dd></>}
            {'type' in selectedStock && <><dt>任务类型</dt><dd>{selectedStock.type}</dd></>}
          </dl>
        </aside>
      )}
    </section>
  )
}
