import { useEffect, useMemo, useState } from 'react'
import { useApp } from '../../app/AppContext'
import { signIn } from '../auth'
import { buildAccountSummary, dailyHoldingPnlDetails, formatDailyHoldingPnlQuoteWarning, recommendSignalBuy } from './account'
import { stockRepository } from './repository'
import type { PositionAllocation } from './account'
import type {
  BacktestRun,
  BacktestEquityPoint,
  BacktestTrade,
  FineStock,
  HoldingStock,
  MissedRunner,
  OverviewStats,
  PaperTradeOrder,
  PortfolioSnapshot,
  RealtimeDecision,
  RoughStock,
  SignalEvent,
  TaskRecord,
  TradeAction,
  TradeRecord,
} from './types'
import './stocks.css'

type StockRow = RoughStock | FineStock | RealtimeDecision | HoldingStock | TradeRecord | TaskRecord | SignalEvent | PositionAllocation | PaperTradeOrder | PortfolioSnapshot | BacktestRun | BacktestTrade | MissedRunner | BacktestEquityPoint
type TabId = 'auto' | 'holdings' | 'backtest' | 'tasks' | 'account' | 'signals' | 'live' | 'rough' | 'fine' | 'history' | 'trades'

interface Column<T> {
  header: string
  cell: (row: T) => React.ReactNode
  align?: 'right'
}

function formatPrice(value: number | null) {
  return value === null ? '-' : value.toFixed(2)
}

function formatMoney(value: number) {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
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

function shanghaiDateValue() {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}`
}

function isHoldingStock(row: StockRow): row is HoldingStock {
  return 'marketValue' in row && 'currentSuggestion' in row
}

function PriceLevelChart({ levels }: { levels: Array<{ label: string; value: number; tone?: string }> }) {
  const validLevels = levels.filter((item) => Number.isFinite(item.value) && item.value > 0)
  if (validLevels.length === 0) return null
  const min = Math.min(...validLevels.map((item) => item.value))
  const max = Math.max(...validLevels.map((item) => item.value))
  const span = Math.max(max - min, max * 0.02)
  return (
    <div className="stock-level-chart">
      <h3>关键价位</h3>
      <div className="stock-level-track">
        {validLevels.map((item) => (
          <span
            key={item.label}
            className={item.tone}
            style={{ left: `${Math.min(96, Math.max(4, ((item.value - min) / span) * 92 + 4))}%` }}
            title={`${item.label} ${formatPrice(item.value)}`}
          />
        ))}
      </div>
      <div className="stock-level-list">
        {validLevels.map((item) => (
          <p key={item.label}><b>{item.label}</b><span>{formatPrice(item.value)}</span></p>
        ))}
      </div>
    </div>
  )
}

function DataTable<T extends StockRow>({
  columns,
  data,
  onRowClick,
  pageSize = 8,
}: {
  columns: Column<T>[]
  data: T[]
  onRowClick?: (row: T) => void
  pageSize?: number
}) {
  const [page, setPage] = useState(0)
  const pageCount = Math.max(1, Math.ceil(data.length / pageSize))
  const safePage = Math.min(page, pageCount - 1)
  const visibleData = data.slice(safePage * pageSize, safePage * pageSize + pageSize)

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
          {visibleData.map((row, index) => (
            <tr key={`${'code' in row ? row.code : 'id' in row ? row.id : index}-${index}`} onClick={() => onRowClick?.(row)}>
              {columns.map((column) => (
                <td key={column.header} className={column.align === 'right' ? 'right' : undefined}>{column.cell(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length > pageSize && (
        <div className="stock-pagination">
          <span>{safePage + 1} / {pageCount} · 共 {data.length} 条</span>
          <div>
            <button type="button" disabled={safePage === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</button>
            <button type="button" disabled={safePage >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}>下一页</button>
          </div>
        </div>
      )}
      {data.length === 0 && <div className="stock-empty">暂无数据</div>}
    </div>
  )
}

export default function StockDashboard() {
  const { adminEmail, refreshAdminUser, adminSignOut } = useApp()
  const [activeTab, setActiveTab] = useState<TabId>('auto')
  const [showMoreTabs, setShowMoreTabs] = useState(false)
  const [loading, setLoading] = useState(true)
  const [selectedStock, setSelectedStock] = useState<StockRow | null>(null)
  const [tradeModal, setTradeModal] = useState<{ action: TradeAction; stock: HoldingStock } | null>(null)
  const [showAddHoldingModal, setShowAddHoldingModal] = useState(false)
  const [savedMessage, setSavedMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [showTodayExecution, setShowTodayExecution] = useState(false)
  const [executionStock, setExecutionStock] = useState<{ code: string; name: string } | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [loginError, setLoginError] = useState('')

  const [overview, setOverview] = useState<OverviewStats | null>(null)
  const [roughStocks, setRoughStocks] = useState<RoughStock[]>([])
  const [fineStocks, setFineStocks] = useState<FineStock[]>([])
  const [realtime, setRealtime] = useState<RealtimeDecision[]>([])
  const [holdings, setHoldings] = useState<HoldingStock[]>([])
  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [signals, setSignals] = useState<SignalEvent[]>([])
  const [historicalFineStocks, setHistoricalFineStocks] = useState<FineStock[]>([])
  const [paperOrders, setPaperOrders] = useState<PaperTradeOrder[]>([])
  const [portfolioSnapshots, setPortfolioSnapshots] = useState<PortfolioSnapshot[]>([])
  const [backtestRuns, setBacktestRuns] = useState<BacktestRun[]>([])
  const [backtestTrades, setBacktestTrades] = useState<BacktestTrade[]>([])
  const [missedRunners, setMissedRunners] = useState<MissedRunner[]>([])
  const [backtestCurve, setBacktestCurve] = useState<BacktestEquityPoint[]>([])
  const accountSummary = useMemo(() => buildAccountSummary(holdings, trades), [holdings, trades])
  const latestSnapshot = portfolioSnapshots[0]
  const latestBacktest = backtestRuns[0]
  const today = shanghaiDateValue()
  const todayHoldingPnlDetails = useMemo(() => dailyHoldingPnlDetails(holdings, realtime), [holdings, realtime])
  const todayHoldingPnl = todayHoldingPnlDetails.total
  const todayQuoteWarning = useMemo(
    () => formatDailyHoldingPnlQuoteWarning(todayHoldingPnlDetails),
    [todayHoldingPnlDetails],
  )
  const todayOrders = useMemo(() => {
    return paperOrders
      .filter((order) => order.orderDate === today)
      .sort((a, b) => a.orderTime.localeCompare(b.orderTime))
  }, [paperOrders])
  const stockExecutionOrders = useMemo(() => {
    if (!executionStock) return []
    return paperOrders
      .filter((order) => order.code === executionStock.code)
      .sort((a, b) => b.orderTime.localeCompare(a.orderTime))
  }, [executionStock, paperOrders])
  const stockExecutionTrades = useMemo(() => {
    if (!executionStock) return []
    return trades
      .filter((trade) => trade.code === executionStock.code)
      .sort((a, b) => b.sellDate.localeCompare(a.sellDate))
  }, [executionStock, trades])
  const stockExecutionHolding = useMemo(() => {
    if (!executionStock) return null
    return holdings.find((holding) => holding.code === executionStock.code) ?? null
  }, [executionStock, holdings])

  async function loadStockData() {
    const [stats, rough, fine, live, currentHoldings, tradeRecords, taskRecords, signalEvents, historicalPicks, autoOrders, snapshots, btRuns, btTrades, missed, curve] = await Promise.all([
      stockRepository.getOverview(),
      stockRepository.getRoughStocks(),
      stockRepository.getFineStocks(),
      stockRepository.getRealtimeDecisions(),
      stockRepository.getHoldings(),
      stockRepository.getTradeRecords(),
      stockRepository.getTasks(),
      stockRepository.getSignalEvents(),
      stockRepository.getHistoricalFineStocks(),
      stockRepository.getPaperTradeOrders(),
      stockRepository.getPortfolioSnapshots(),
      stockRepository.getBacktestRuns(),
      stockRepository.getBacktestTrades(),
      stockRepository.getMissedRunners(),
      stockRepository.getBacktestEquityCurve(),
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
    setPaperOrders(autoOrders)
    setPortfolioSnapshots(snapshots)
    setBacktestRuns(btRuns)
    setBacktestTrades(btTrades)
    setMissedRunners(missed)
    setBacktestCurve(curve)
  }

  useEffect(() => {
    let mounted = true
    async function load() {
      setLoading(true)
      const [stats, rough, fine, live, currentHoldings, tradeRecords, taskRecords, signalEvents, historicalPicks, autoOrders, snapshots, btRuns, btTrades, missed, curve] = await Promise.all([
        stockRepository.getOverview(),
        stockRepository.getRoughStocks(),
        stockRepository.getFineStocks(),
        stockRepository.getRealtimeDecisions(),
        stockRepository.getHoldings(),
        stockRepository.getTradeRecords(),
        stockRepository.getTasks(),
        stockRepository.getSignalEvents(),
        stockRepository.getHistoricalFineStocks(),
        stockRepository.getPaperTradeOrders(),
        stockRepository.getPortfolioSnapshots(),
        stockRepository.getBacktestRuns(),
        stockRepository.getBacktestTrades(),
        stockRepository.getMissedRunners(),
        stockRepository.getBacktestEquityCurve(),
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
      setPaperOrders(autoOrders)
      setPortfolioSnapshots(snapshots)
      setBacktestRuns(btRuns)
      setBacktestTrades(btTrades)
      setMissedRunners(missed)
      setBacktestCurve(curve)
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
    { id: 'auto' as const, label: '自动模拟盘' },
    { id: 'holdings' as const, label: '持仓执行' },
    { id: 'backtest' as const, label: '回测中心' },
    { id: 'tasks' as const, label: '任务中心' },
  ], [])
  const secondaryTabs = useMemo(() => [
    { id: 'account' as const, label: '账户总览' },
    { id: 'signals' as const, label: '信号中心' },
    { id: 'live' as const, label: '盘中实时决策' },
    { id: 'rough' as const, label: '今日海选' },
    { id: 'fine' as const, label: '最近盘后精选' },
    { id: 'history' as const, label: '历史精选' },
    { id: 'trades' as const, label: '清仓复盘' },
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
    { header: '成本金额', cell: (row) => formatMoney(row.costPrice * row.shares), align: 'right' },
    { header: '市值', cell: (row) => row.marketValue.toFixed(0), align: 'right' },
    { header: '账户占比', cell: (row) => `${accountSummary.positions.find((item) => item.code === row.code)?.allocationRate ?? 0}%`, align: 'right' },
    { header: '浮盈亏', cell: (row) => <ColorNumber value={row.floatingPnl} />, align: 'right' },
    { header: '盈亏率', cell: (row) => <ColorNumber value={row.pnlRate} suffix="%" />, align: 'right' },
    { header: '当前建议', cell: (row) => row.currentSuggestion },
    {
      header: '操作',
      cell: (row) => (
        <div className="stock-row-actions">
          <button type="button" onClick={(event) => {
            event.stopPropagation()
            setExecutionStock({ code: row.code, name: row.name })
          }}>{'\u6267\u884c\u8bb0\u5f55'}</button>
          {(['加仓', '减仓', '清仓'] as TradeAction[]).map((action) => (
            <button key={action} type="button" onClick={(event) => {
              event.stopPropagation()
              if (!requireStockLogin()) return
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
    { header: '执行状态', cell: (row) => <StatusTag status={row.executionStatusText} /> },
    { header: '执行说明', cell: (row) => row.executionReason || (row.executionStatus === 'not_executed' ? '仅策略建议，尚未形成虚拟订单' : '-') },
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

  const allocationColumns: Column<PositionAllocation>[] = [
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '成本金额', cell: (row) => formatMoney(row.costAmount), align: 'right' },
    { header: '当前市值', cell: (row) => formatMoney(row.marketValue), align: 'right' },
    { header: '账户占比', cell: (row) => `${row.allocationRate}%`, align: 'right' },
    { header: '浮盈亏', cell: (row) => <ColorNumber value={row.floatingPnl} />, align: 'right' },
    { header: '收益贡献', cell: (row) => <ColorNumber value={row.pnlContributionRate} suffix="%" />, align: 'right' },
    { header: '仓位状态', cell: (row) => row.overSinglePositionLimit ? <StatusTag status="超单票上限" /> : '正常' },
  ]

  const paperOrderColumns: Column<PaperTradeOrder>[] = [
    { header: '时间', cell: (row) => row.orderTime },
    { header: '方向', cell: (row) => <StatusTag status={row.side === 'buy' ? '买入' : '卖出'} /> },
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '价格', cell: (row) => formatPrice(row.price), align: 'right' },
    { header: '股数', cell: (row) => row.shares, align: 'right' },
    { header: '金额', cell: (row) => formatMoney(row.amount), align: 'right' },
    { header: '费用', cell: (row) => formatMoney(row.feeAmount), align: 'right' },
    { header: '滑点', cell: (row) => formatMoney(row.slippageAmount), align: 'right' },
    { header: '实现盈亏', cell: (row) => <ColorNumber value={row.realizedPnl} />, align: 'right' },
    { header: '状态', cell: (row) => <StatusTag status={row.status || 'filled'} /> },
    { header: '触发原因', cell: (row) => row.reason },
    { header: '阻断/失败', cell: (row) => row.failureReason || '-' },
  ]

  paperOrderColumns.push({
    header: '执行记录',
    cell: (row) => (
      <button
        type="button"
        className="stock-inline-button"
        onClick={(event) => {
          event.stopPropagation()
          setExecutionStock({ code: row.code, name: row.name })
        }}
      >
        查看
      </button>
    ),
  })

  const snapshotColumns: Column<PortfolioSnapshot>[] = [
    { header: '时间', cell: (row) => row.snapshotTime },
    { header: '总资产', cell: (row) => formatMoney(row.totalAssets), align: 'right' },
    { header: '现金', cell: (row) => formatMoney(row.cash), align: 'right' },
    { header: '持仓市值', cell: (row) => formatMoney(row.holdingMarketValue), align: 'right' },
    { header: '总盈亏', cell: (row) => <ColorNumber value={row.totalPnl} />, align: 'right' },
    { header: '收益率', cell: (row) => <ColorNumber value={row.totalReturnRate} suffix="%" />, align: 'right' },
    { header: '持仓数', cell: (row) => row.positionCount, align: 'right' },
    { header: '成交数', cell: (row) => row.tradeCount, align: 'right' },
  ]

  const backtestRunColumns: Column<BacktestRun>[] = [
    { header: 'Run time', cell: (row) => row.runTime },
    { header: '策略', cell: (row) => row.strategyName },
    { header: '对比基准', cell: (row) => row.benchmarkName },
    { header: '回测区间', cell: (row) => `${row.startDate} ~ ${row.endDate}` },
    { header: '策略收益', cell: (row) => <ColorNumber value={row.totalReturnRate} suffix="%" />, align: 'right' },
    { header: '年化', cell: (row) => <ColorNumber value={row.annualReturnRate} suffix={'%'} />, align: 'right' },
    { header: '基准收益', cell: (row) => <ColorNumber value={row.benchmarkReturnRate} suffix="%" />, align: 'right' },
    { header: 'CSI300', cell: (row) => <ColorNumber value={row.benchmarkCsi300ReturnRate} suffix="%" />, align: 'right' },
    { header: 'CSI500', cell: (row) => <ColorNumber value={row.benchmarkCsi500ReturnRate} suffix="%" />, align: 'right' },
    { header: '超额收益', cell: (row) => <ColorNumber value={row.excessReturnRate} suffix="%" />, align: 'right' },
    { header: '对账', cell: (row) => row.equityReconciled ? 'OK' : 'Check' },
    { header: '最大回撤', cell: (row) => `${row.maxDrawdownRate.toFixed(2)}%`, align: 'right' },
    { header: 'Sharpe', cell: (row) => row.sharpeRatio.toFixed(2), align: 'right' },
    { header: 'Calmar', cell: (row) => row.calmarRatio.toFixed(2), align: 'right' },
    { header: '胜率', cell: (row) => `${row.winRate.toFixed(2)}%`, align: 'right' },
    { header: '盈亏比', cell: (row) => row.profitLossRatio.toFixed(2), align: 'right' },
    { header: '交易次数', cell: (row) => row.tradeCount, align: 'right' },
    { header: '换手', cell: (row) => row.turnoverRate.toFixed(2) + '%', align: 'right' },
    { header: '连续亏损', cell: (row) => row.consecutiveLosses, align: 'right' },
    { header: '最大单亏', cell: (row) => <ColorNumber value={row.largestSingleLoss} />, align: 'right' },
    { header: '平均持仓天数', cell: (row) => row.avgHoldingDays.toFixed(1), align: 'right' },
    { header: '错过大涨数', cell: (row) => row.missedRunnerCount, align: 'right' },
  ]

  const backtestTradeColumns: Column<BacktestTrade>[] = [
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '买入日', cell: (row) => row.entryDate },
    { header: '卖出日', cell: (row) => row.exitDate },
    { header: '买入价', cell: (row) => formatPrice(row.entryPrice), align: 'right' },
    { header: '卖出价', cell: (row) => formatPrice(row.exitPrice), align: 'right' },
    { header: '盈亏', cell: (row) => <ColorNumber value={row.pnlAmount} />, align: 'right' },
    { header: '费用', cell: (row) => formatMoney(row.feeAmount), align: 'right' },
    { header: '滑点', cell: (row) => formatMoney(row.slippageAmount), align: 'right' },
    { header: '收益率', cell: (row) => <ColorNumber value={row.pnlRate} suffix="%" />, align: 'right' },
    { header: '持仓天数', cell: (row) => row.holdingDays, align: 'right' },
    { header: '退出原因', cell: (row) => row.exitReason },
  ]

  const missedRunnerColumns: Column<MissedRunner>[] = [
    { header: '入选日', cell: (row) => row.pickDate },
    { header: '代码', cell: (row) => row.code },
    { header: '名称', cell: (row) => row.name },
    { header: '入选价', cell: (row) => formatPrice(row.pickPrice), align: 'right' },
    { header: '最高价', cell: (row) => formatPrice(row.maxPrice), align: 'right' },
    { header: '最大涨幅', cell: (row) => <ColorNumber value={row.maxReturnRate} suffix="%" />, align: 'right' },
    { header: '几天后见高点', cell: (row) => row.daysToHigh, align: 'right' },
    { header: '原因', cell: (row) => row.reason },
  ]

  const backtestCurveColumns: Column<BacktestEquityPoint>[] = [
    { header: '日期', cell: (row) => row.curveDate },
    { header: '策略权益', cell: (row) => formatMoney(row.equityValue), align: 'right' },
    { header: '单日收益', cell: (row) => <ColorNumber value={row.dailyReturnRate} suffix="%" />, align: 'right' },
    { header: '回撤', cell: (row) => `${row.drawdownRate.toFixed(2)}%`, align: 'right' },
    { header: '基准权益', cell: (row) => formatMoney(row.benchmarkValue), align: 'right' },
    { header: '基准收益', cell: (row) => <ColorNumber value={row.benchmarkReturnRate} suffix="%" />, align: 'right' },
  ]

  function numberPrompt(message: string, fallback: number) {
    const value = window.prompt(message, String(fallback))
    if (value === null) return null
    const parsed = Number(value)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }

  function todayInputValue() {
    return shanghaiDateValue()
  }

  function requireStockLogin() {
    if (adminEmail) return true
    setLoginOpen(true)
    setErrorMessage('请先登录后查看或记录股票数据。')
    return false
  }

  async function handleSignalBuy(signal: SignalEvent) {
    if (!requireStockLogin()) return
    const recommendation = recommendSignalBuy(signal, accountSummary)
    if (recommendation.maxShares <= 0) {
      setErrorMessage(recommendation.reason)
      return
    }
    const shares = numberPrompt(`确认线下买入股数。策略建议：${recommendation.maxShares} 股，约 ${formatMoney(recommendation.estimatedAmount)} 元；${recommendation.reason}`, recommendation.maxShares || 100)
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
    if (!requireStockLogin()) return
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
    if (!requireStockLogin()) return
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
    if (!requireStockLogin()) return
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
    if (!requireStockLogin()) return
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
    if (!requireStockLogin()) return
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

  function explainLocalScript(action: 'night' | 'live' | 'paper' | 'backtest' | 'sync') {
    if (!requireStockLogin()) return
    setErrorMessage('')
    const jobTypes = {
      night: 'night_scan' as const,
      live: 'live_decision' as const,
      paper: 'paper_trade' as const,
      backtest: 'backtest' as const,
      sync: 'sync_latest' as const,
    }
    const labels = {
      backtest: '回测中心',
      paper: '自动模拟盘',
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

  async function submitStockLogin(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setLoginError('')
    try {
      await signIn(String(form.get('email') || ''), String(form.get('password') || ''))
      await refreshAdminUser()
      await loadStockData()
      setLoginOpen(false)
      setSavedMessage('登录成功，数据已刷新。')
    } catch (reason) {
      setLoginError(reason instanceof Error ? reason.message : '登录失败')
    }
  }

  if (loading) {
    return <section className="stock-dashboard stock-loading">正在同步策略工作台数据...</section>
  }

  return (
    <section className="stock-dashboard">
      <header className="stock-hero">
        <div>
          <span className="eyebrow">A 股策略工作台</span>
          <h1>股票策略助手</h1>
          <p>盘中信号、仓位跟踪、自动模拟盘与收益复盘。</p>
        </div>
        <div className="stock-login-box">
          {adminEmail ? (
            <>
              <span>{adminEmail}</span>
              <button type="button" onClick={() => void adminSignOut().then(loadStockData)}>退出</button>
            </>
          ) : (
            <>
              <span>登录后读取策略数据</span>
              <button type="button" onClick={() => setLoginOpen(true)}>登录</button>
            </>
          )}
        </div>
      </header>

      <div className="stock-stats">
        {[
          ['总资产', formatMoney(accountSummary.totalAssets)],
          ['今日盈亏', `${todayHoldingPnl >= 0 ? '+' : ''}${formatMoney(todayHoldingPnl)}`],
          ['总盈亏', `${accountSummary.totalPnl >= 0 ? '+' : ''}${formatMoney(accountSummary.totalPnl)}`],
          ['总收益率', `${accountSummary.totalReturnRate >= 0 ? '+' : ''}${accountSummary.totalReturnRate.toFixed(2)}%`],
          ['当前持仓', holdings.length],
        ].map(([label, value]) => (
          <article key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </article>
        ))}
      </div>

      <div className="stock-account-overview stock-account-overview-secondary">
        <section>
          <span>初始本金</span>
          <strong>¥{formatMoney(accountSummary.initialCapital)}</strong>
        </section>
        <section>
          <span>当前持仓市值</span>
          <strong>¥{formatMoney(accountSummary.holdingMarketValue)}</strong>
        </section>
        <section>
          <span>当前浮盈亏</span>
          <strong><ColorNumber value={accountSummary.floatingPnl} /></strong>
        </section>
        <section>
          <span>已清仓盈亏</span>
          <strong><ColorNumber value={accountSummary.realizedPnl} /></strong>
        </section>
        <section>
          <span>总收益率</span>
          <strong><ColorNumber value={accountSummary.totalReturnRate} suffix="%" /></strong>
        </section>
        <section>
          <span>风控规则</span>
          <b>最多 6 只；单票 15%；留现金 25%</b>
        </section>
      </div>
      {accountSummary.positionCountWarning && <div className="stock-account-warning">{accountSummary.positionCountWarning}</div>}
      {todayQuoteWarning && <div className="stock-account-warning">{todayQuoteWarning}</div>}

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
          <button
            type="button"
            className="stock-more-tab"
            aria-expanded={showMoreTabs}
            onClick={() => setShowMoreTabs((value) => !value)}
          >
            更多数据
          </button>
        </div>
        {showMoreTabs && (
          <div className="stock-secondary-tabs" role="tablist" aria-label="股票辅助数据视图">
            {secondaryTabs.map((tab) => (
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
        )}

        <div className="stock-panel">
          {activeTab === 'account' && (
            <div className="stock-section-stack">
              <div className="stock-panel-heading">
                <div>
                  <h2>账户总览</h2>
                  <p>默认 100 万本金，按 4-6 只股票、单票最高 15%、至少保留 25% 现金做仓位控制。</p>
                </div>
              </div>
              <div className="stock-chart-grid">
                <section className="stock-mini-chart">
                  <h3>持仓占比</h3>
                  {accountSummary.positions.length === 0 && <div className="stock-empty">暂无持仓</div>}
                  {accountSummary.positions.map((item) => (
                    <div className="stock-bar-row" key={item.code}>
                      <span>{item.name}</span>
                      <div><i style={{ width: `${Math.min(item.allocationRate, 100)}%` }} /></div>
                      <b>{item.allocationRate}%</b>
                    </div>
                  ))}
                </section>
                <section className="stock-mini-chart">
                  <h3>盈亏贡献</h3>
                  {accountSummary.positions.length === 0 && <div className="stock-empty">暂无持仓</div>}
                  {accountSummary.positions.map((item) => (
                    <div className="stock-bar-row stock-pnl-row" key={item.code}>
                      <span>{item.name}</span>
                      <div><i className={item.floatingPnl >= 0 ? 'gain' : 'loss'} style={{ width: `${Math.min(Math.abs(item.pnlContributionRate) * 12, 100)}%` }} /></div>
                      <b><ColorNumber value={item.floatingPnl} /></b>
                    </div>
                  ))}
                </section>
              </div>
              <DataTable columns={allocationColumns} data={accountSummary.positions} />
            </div>
          )}
          {activeTab === 'auto' && (
            <div className="stock-section-stack">
              <div className="stock-panel-heading">
                <div>
                  <h2>自动模拟盘</h2>
                  <p>100 万虚拟资金按策略自动演算买卖，沉淀账户曲线和成交记录。</p>
                </div>
              </div>
              <button type="button" onClick={() => setShowTodayExecution((value) => !value)}>
                {showTodayExecution ? '隐藏今日自动执行记录' : '查看今日自动执行记录'}
              </button>
              {showTodayExecution && (
                <section className="stock-execution-timeline">
                  <h3>今日自动执行记录</h3>
                  {todayOrders.length === 0 && <div className="stock-empty">今天还没有自动买卖记录</div>}
                  {todayOrders.map((order) => (
                    <article key={order.id}>
                      <time>{order.orderTime}</time>
                      <div>
                        <b>{order.side === 'buy' ? '虚拟买入' : '虚拟卖出'}：{order.name} {order.code}</b>
                        <p>{formatPrice(order.price)} 元 / {order.shares} 股 / {formatMoney(order.amount)} 元；原因：{order.reason}</p>
                      </div>
                      <strong><ColorNumber value={order.realizedPnl} /></strong>
                    </article>
                  ))}
                </section>
              )}
              {latestSnapshot && (
                <div className="stock-account-overview">
                  <section>
                    <span>总资产</span>
                    <strong>{formatMoney(latestSnapshot.totalAssets)}</strong>
                  </section>
                  <section>
                    <span>现金</span>
                    <strong>{formatMoney(latestSnapshot.cash)}</strong>
                  </section>
                  <section>
                    <span>总盈亏</span>
                    <strong><ColorNumber value={latestSnapshot.totalPnl} /></strong>
                  </section>
                  <section>
                    <span>收益率</span>
                    <strong><ColorNumber value={latestSnapshot.totalReturnRate} suffix="%" /></strong>
                  </section>
                </div>
              )}
              <section>
                <h3>账户快照</h3>
                <DataTable columns={snapshotColumns} data={portfolioSnapshots} onRowClick={setSelectedStock} />
              </section>
              <section>
                <h3>自动交易流水</h3>
                <DataTable columns={paperOrderColumns} data={paperOrders} onRowClick={setSelectedStock} />
              </section>
            </div>
          )}
          {activeTab === 'backtest' && (
            <div className="stock-section-stack">
              <div className="stock-panel-heading">
                <div>
                  <h2>回测中心</h2>
                  <p>用历史行情验证当前策略，看策略过去是否赚钱、是否跑赢基准，以及错过了哪些大涨股。</p>
                </div>
                <button type="button" onClick={() => explainLocalScript('backtest')}>运行回测</button>
              </div>
              {latestBacktest && (
                <div className="stock-account-overview">
                  <section>
                    <span>策略收益</span>
                    <strong><ColorNumber value={latestBacktest.totalReturnRate} suffix="%" /></strong>
                  </section>
                  <section>
                    <span>超额收益</span>
                    <strong><ColorNumber value={latestBacktest.excessReturnRate} suffix="%" /></strong>
                  </section>
                  <section>
                    <span>最大回撤</span>
                    <strong>{latestBacktest.maxDrawdownRate.toFixed(2)}%</strong>
                  </section>
                  <section>
                    <span>胜率</span>
                    <strong>{latestBacktest.winRate.toFixed(2)}%</strong>
                  </section>
                  <section>
                    <span>盈亏比</span>
                    <strong>{latestBacktest.profitLossRatio.toFixed(2)}</strong>
                  </section>
                  <section>
                    <span>交易次数</span>
                    <strong>{latestBacktest.tradeCount}</strong>
                  </section>
                  <section>
                    <span>平均持仓天数</span>
                    <strong>{latestBacktest.avgHoldingDays.toFixed(1)}</strong>
                  </section>
                </div>
              )}
              <section>
                <h3>回测记录</h3>
                <DataTable columns={backtestRunColumns} data={backtestRuns} />
              </section>
              <section>
                <h3>权益曲线</h3>
                <DataTable columns={backtestCurveColumns} data={backtestCurve} />
              </section>
              <section>
                <h3>回测交易样本</h3>
                <DataTable columns={backtestTradeColumns} data={backtestTrades} onRowClick={setSelectedStock} />
              </section>
              <section>
                <h3>错过的大涨股</h3>
                <DataTable columns={missedRunnerColumns} data={missedRunners} onRowClick={setSelectedStock} />
              </section>
            </div>
          )}
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
                <button type="button" onClick={() => {
                  if (requireStockLogin()) setShowAddHoldingModal(true)
                }}>新增持仓</button>
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
                <button type="button" onClick={() => explainLocalScript('backtest')}>运行回测</button>
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

      {executionStock && (
        <div className="stock-modal-backdrop" role="dialog" aria-modal="true" aria-label={`${executionStock.name} 执行记录`}>
          <section className="stock-modal stock-execution-modal">
            <div className="stock-timeline-heading">
              <div>
                <h2>{executionStock.name} 执行记录</h2>
                <p>{executionStock.code}</p>
              </div>
              <button type="button" onClick={() => setExecutionStock(null)}>关闭</button>
            </div>
            <div className="stock-execution-timeline">
              {!stockExecutionHolding && stockExecutionOrders.length === 0 && stockExecutionTrades.length === 0 && (
                <div className="stock-empty">这只股票还没有买入、卖出或自动执行记录</div>
              )}
              {stockExecutionHolding && (
                <article>
                  <time>{stockExecutionHolding.buyDate}</time>
                  <div>
                    <b>当前持仓买入</b>
                    <p>买入价 {formatPrice(stockExecutionHolding.costPrice)} 元 / {stockExecutionHolding.shares} 股；理由：{stockExecutionHolding.buyMemo || stockExecutionHolding.currentSuggestion || '无'}</p>
                  </div>
                  <strong><ColorNumber value={stockExecutionHolding.floatingPnl} /></strong>
                </article>
              )}
              {stockExecutionTrades.map((trade, index) => (
                <article key={`manual-${trade.code}-${trade.sellDate}-${index}`}>
                  <time>{trade.sellDate}</time>
                  <div>
                    <b>{trade.isCleared ? '手动清仓' : '手动减仓'}</b>
                    <p>买入：{trade.buyDate} / {formatPrice(trade.costPrice)} 元；卖出：{formatPrice(trade.sellPrice)} 元 / {trade.shares} 股；理由：{trade.sellMemo || trade.buyMemo || '无'}</p>
                  </div>
                  <strong><ColorNumber value={trade.pnlAmount} /></strong>
                </article>
              ))}
              {stockExecutionOrders.map((order) => (
                <article key={order.id}>
                  <time>{order.orderTime}</time>
                  <div>
                    <b>{order.side === 'buy' ? '自动模拟买入' : '自动模拟卖出'}</b>
                    <p>{formatPrice(order.price)} 元 / {order.shares} 股 / {formatMoney(order.amount)} 元；原因：{order.reason || '无'}</p>
                  </div>
                  <strong><ColorNumber value={order.realizedPnl} /></strong>
                </article>
              ))}
            </div>
          </section>
        </div>
      )}

      {loginOpen && (
        <div className="stock-modal-backdrop" role="dialog" aria-modal="true" aria-label="股票助手登录">
          <form className="stock-modal stock-login-modal" onSubmit={submitStockLogin}>
            <h2>登录股票助手</h2>
            <label>邮箱<input name="email" type="email" autoComplete="email" required /></label>
            <label>密码<input name="password" type="password" autoComplete="current-password" required /></label>
            {loginError && <p className="stock-form-error">{loginError}</p>}
            <div className="stock-modal-actions">
              <button type="button" onClick={() => setLoginOpen(false)}>取消</button>
              <button type="submit">登录</button>
            </div>
          </form>
        </div>
      )}

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
          {isHoldingStock(selectedStock) && (
            <PriceLevelChart levels={[
              { label: '成本', value: selectedStock.costPrice },
              { label: '当前', value: selectedStock.currentPrice, tone: selectedStock.currentPrice >= selectedStock.costPrice ? 'gain' : 'loss' },
            ]} />
          )}
          {'signalType' in selectedStock && (
            <PriceLevelChart levels={[
              { label: '触发', value: selectedStock.triggerPrice },
              { label: '当前', value: selectedStock.currentPrice },
              { label: '止损', value: selectedStock.stopLoss, tone: 'loss' },
              { label: '目标', value: selectedStock.targetPrice1 ?? 0, tone: 'gain' },
            ]} />
          )}
          <dl>
            {'date' in selectedStock && <><dt>日期</dt><dd>{selectedStock.date}</dd></>}
            {isHoldingStock(selectedStock) && <><dt>成本金额</dt><dd>¥{formatMoney(selectedStock.costPrice * selectedStock.shares)}</dd></>}
            {'marketValue' in selectedStock && <><dt>当前市值</dt><dd>¥{formatMoney(selectedStock.marketValue)}</dd></>}
            {'marketValue' in selectedStock && <><dt>账户占比</dt><dd>{accountSummary.positions.find((item) => item.code === selectedStock.code)?.allocationRate ?? 0}%</dd></>}
            {'floatingPnl' in selectedStock && <><dt>浮盈亏</dt><dd><ColorNumber value={selectedStock.floatingPnl} /></dd></>}
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
