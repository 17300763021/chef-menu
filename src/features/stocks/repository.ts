import type { SupabaseClient } from '@supabase/supabase-js'
import { supabase } from '../../lib/supabase'
import { stocksApi } from './mockApi'
import type {
  AddHoldingInput,
  BacktestEquityPoint,
  BacktestRun,
  BacktestTrade,
  ConfirmSignalBuyInput,
  FineStock,
  HoldingStock,
  ModelDecision,
  ModelOrder,
  ModelPortfolioSnapshot,
  ModelPosition,
  ModelPrediction,
  MissedRunner,
  OverviewStats,
  PaperTradeOrder,
  PortfolioSnapshot,
  RecordTTradeInput,
  RealtimeDecision,
  RoughStock,
  SaveTradeInput,
  SaveTradeResult,
  SignalEvent,
  SignalExecutionStatus,
  StockJobType,
  TaskRecord,
  TradeRecord,
} from './types'

type StockSupabaseClient = SupabaseClient | null
type Row = Record<string, unknown>

function text(row: Row, key: string, fallback = '') {
  return String(row[key] ?? fallback)
}

function numberValue(row: Row, key: string, fallback = 0) {
  return Number(row[key] ?? fallback)
}

function jsonValue(row: Row, key: string) {
  const value = row[key]
  if (!value) return null
  if (typeof value === 'string') {
    try {
      return JSON.parse(value) as unknown
    } catch {
      return value
    }
  }
  return value
}

function optionalNumber(row: Row, key: string) {
  const value = row[key]
  return value === null || value === undefined ? null : Number(value)
}

function formatDateTime(value: unknown) {
  const textValue = String(value ?? '').trim()
  if (!textValue || textValue === '--') return textValue || '--'
  if (!textValue.includes('T')) return textValue

  const date = new Date(textValue)
  if (Number.isNaN(date.getTime())) return textValue

  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`
}

function translateStockText(value: unknown) {
  let textValue = String(value ?? '').trim()
  if (!textValue) return ''

  const replacements: Array<[RegExp, string]> = [
    [/Auto paper trading engine/g, '自动模拟交易引擎'],
    [/Auto paper buy:/g, '自动模拟买入：'],
    [/Auto paper sell:/g, '自动模拟卖出：'],
    [/Auto paper partial sell:/g, '自动模拟减仓：'],
    [/Auto paper closed:/g, '自动模拟清仓：'],
    [/stop loss touched/g, '触发止损'],
    [/take profit touched/g, '触发止盈'],
    [/target 1 touched/g, '触发第一止盈位'],
    [/target touched/g, '触发目标价'],
    [/max holding days/g, '达到最长持仓天数'],
    [/risk control/g, '风控触发'],
    [/insufficient cash/g, '现金不足'],
    [/not bought/g, '未买入'],
    [/already holding/g, '已有持仓'],
    [/max holdings/g, '达到持仓数量上限'],
  ]

  for (const [pattern, replacement] of replacements) {
    textValue = textValue.replace(pattern, replacement)
  }
  return textValue.replace(/：\s+/g, '：')
}

const signalExecutionStatusText: Record<SignalExecutionStatus, string> = {
  not_executed: '策略建议，未执行',
  auto_executed: '已自动模拟执行',
  manual_executed: '已手动记录执行',
  ignored: '已忽略',
  blocked: '执行受阻',
  failed: '执行失败',
}

function signalExecutionStatus(row: Row): SignalExecutionStatus {
  const value = text(row, 'execution_status', 'not_executed')
  if (
    value === 'auto_executed'
    || value === 'manual_executed'
    || value === 'ignored'
    || value === 'blocked'
    || value === 'failed'
  ) {
    return value
  }
  return 'not_executed'
}

function calculateHolding(input: AddHoldingInput): HoldingStock {
  const marketValue = input.currentPrice * input.shares
  const floatingPnl = (input.currentPrice - input.costPrice) * input.shares
  const pnlRate = input.costPrice > 0 ? ((input.currentPrice - input.costPrice) / input.costPrice) * 100 : 0
  return {
    code: input.code,
    name: input.name,
    costPrice: input.costPrice,
    shares: input.shares,
    currentPrice: input.currentPrice,
    marketValue,
    floatingPnl,
    pnlRate,
    buyDate: input.buyDate,
    holdingDays: 0,
    currentSuggestion: input.currentSuggestion,
    buyMemo: input.buyMemo,
  }
}

function recalculateHolding(holding: HoldingStock, shares: number, costPrice: number, currentPrice = costPrice): HoldingStock {
  const marketValue = currentPrice * shares
  const floatingPnl = (currentPrice - costPrice) * shares
  const pnlRate = costPrice > 0 ? ((currentPrice - costPrice) / costPrice) * 100 : 0
  return {
    ...holding,
    shares,
    costPrice,
    currentPrice,
    marketValue,
    floatingPnl,
    pnlRate,
  }
}

function tradeRecordFromInput(input: SaveTradeInput): TradeRecord {
  const pnlAmount = (input.price - input.holding.costPrice) * input.shares
  const pnlRate = input.holding.costPrice > 0 ? ((input.price - input.holding.costPrice) / input.holding.costPrice) * 100 : 0
  return {
    code: input.holding.code,
    name: input.holding.name,
    buyDate: input.holding.buyDate,
    sellDate: input.tradeDate,
    costPrice: input.holding.costPrice,
    sellPrice: input.price,
    shares: input.shares,
    pnlAmount,
    pnlRate,
    buyMemo: '',
    sellMemo: input.memo,
    isCleared: input.action === '清仓' || input.shares >= input.holding.shares,
  }
}

async function withTimeout<T>(request: PromiseLike<T>, milliseconds = 5000): Promise<T> {
  let timer = 0
  try {
    return await Promise.race([
      Promise.resolve(request),
      new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => reject(new Error('Stock repository request timed out')), milliseconds)
      }),
    ])
  } finally {
    window.clearTimeout(timer)
  }
}

function mapRoughStock(row: Row): RoughStock {
  return {
    code: text(row, 'code'),
    name: text(row, 'name'),
    date: text(row, 'scan_date'),
    score: numberValue(row, 'score'),
    prevClose: numberValue(row, 'prev_close'),
    signal: text(row, 'signal'),
    action: text(row, 'action'),
    supportLevel: numberValue(row, 'support_level'),
    resistanceLevel: numberValue(row, 'resistance_level'),
    stopLoss: numberValue(row, 'stop_loss'),
    reason: text(row, 'reason'),
    risk: text(row, 'risk'),
  }
}

function mapFineStock(row: Row): FineStock {
  return {
    ...mapRoughStock(row),
    strategyLevel: text(row, 'strategy_level'),
    reviewStatus: text(row, 'review_status'),
  }
}

function mapRealtimeDecision(row: Row): RealtimeDecision {
  return {
    code: text(row, 'code'),
    name: text(row, 'name'),
    date: text(row, 'decision_date'),
    updateTime: formatDateTime(row.update_time),
    operationType: text(row, 'operation_type'),
    currentPrice: numberValue(row, 'current_price'),
    changeRate: numberValue(row, 'change_rate'),
    canBuy: Boolean(row.can_buy),
    suggestBuyPrice: optionalNumber(row, 'suggest_buy_price'),
    suggestSellPrice: optionalNumber(row, 'suggest_sell_price'),
    stopLoss: numberValue(row, 'stop_loss'),
    targetPrice1: optionalNumber(row, 'target_price_1'),
    finalAction: text(row, 'final_action'),
    noBuyReason: text(row, 'no_buy_reason'),
    sellReason: text(row, 'sell_reason'),
    status: text(row, 'status', '不买/无动作') as RealtimeDecision['status'],
  }
}

function mapHolding(row: Row): HoldingStock {
  return {
    id: text(row, 'id'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    costPrice: numberValue(row, 'cost_price'),
    shares: numberValue(row, 'shares'),
    currentPrice: numberValue(row, 'current_price', numberValue(row, 'cost_price')),
    marketValue: numberValue(row, 'market_value'),
    floatingPnl: numberValue(row, 'floating_pnl'),
    pnlRate: numberValue(row, 'pnl_rate'),
    buyDate: text(row, 'buy_date'),
    holdingDays: numberValue(row, 'holding_days'),
    currentSuggestion: translateStockText(row.current_suggestion),
    buyMemo: translateStockText(row.buy_memo),
  }
}

function mapTrade(row: Row): TradeRecord {
  return {
    code: text(row, 'code'),
    name: text(row, 'name'),
    buyDate: text(row, 'buy_date'),
    sellDate: text(row, 'sell_date'),
    costPrice: numberValue(row, 'cost_price'),
    sellPrice: numberValue(row, 'sell_price'),
    shares: numberValue(row, 'shares'),
    pnlAmount: numberValue(row, 'pnl_amount'),
    pnlRate: numberValue(row, 'pnl_rate'),
    buyMemo: translateStockText(row.buy_memo),
    sellMemo: translateStockText(row.sell_memo),
    isCleared: Boolean(row.is_cleared),
  }
}

const taskTypeLabels: Record<string, string> = {
  full: '线上全链路任务',
  night_scan: '线上股票池筛选',
  live_decision: '线上实时决策',
  live_session: '线上盘中轮询',
  paper_trade: '线上模拟交易执行',
  backtest: '线上策略回测',
  sync_latest: '线上结果入库',
  pending: '线上任务请求处理',
  '本地CSV同步': '策略结果入库',
}

function formatTaskType(value: unknown): string {
  const raw = String(value ?? '').trim()
  if (!raw) return ''
  if (taskTypeLabels[raw]) return taskTypeLabels[raw]
  const githubPrefix = 'GitHub Actions: '
  if (raw.startsWith(githubPrefix)) {
    const mode = raw.slice(githubPrefix.length)
    return taskTypeLabels[mode] ?? `线上任务：${mode}`
  }
  const webPrefix = 'Web request: '
  if (raw.startsWith(webPrefix)) {
    const mode = raw.slice(webPrefix.length)
    return `页面触发：${taskTypeLabels[mode] ?? mode}`
  }
  return raw
}

function mapTask(row: Row): TaskRecord {
  return {
    id: text(row, 'id'),
    type: formatTaskType(row.job_type),
    startTime: formatDateTime(row.started_at),
    endTime: formatDateTime(row.finished_at || '--'),
    status: text(row, 'status', '成功') as TaskRecord['status'],
    importedCount: numberValue(row, 'imported_count'),
    errorMsg: text(row, 'error_message'),
  }
}

function mapPaperTradeOrder(row: Row): PaperTradeOrder {
  return {
    id: text(row, 'id'),
    orderTime: formatDateTime(row.order_time),
    orderDate: text(row, 'order_date'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    side: text(row, 'side', 'buy') as PaperTradeOrder['side'],
    reason: translateStockText(row.reason),
    price: numberValue(row, 'price'),
    shares: numberValue(row, 'shares'),
    amount: numberValue(row, 'amount'),
    feeAmount: numberValue(row, 'fee_amount'),
    slippageAmount: numberValue(row, 'slippage_amount'),
    cashBefore: numberValue(row, 'cash_before'),
    cashAfter: numberValue(row, 'cash_after'),
    realizedPnl: numberValue(row, 'realized_pnl'),
    status: text(row, 'status'),
    sourceSignalId: text(row, 'source_signal_id'),
    failureReason: translateStockText(row.failure_reason),
  }
}

function mapPortfolioSnapshot(row: Row): PortfolioSnapshot {
  return {
    id: text(row, 'id'),
    snapshotTime: formatDateTime(row.snapshot_time),
    snapshotDate: text(row, 'snapshot_date'),
    cash: numberValue(row, 'cash'),
    holdingMarketValue: numberValue(row, 'holding_market_value'),
    totalAssets: numberValue(row, 'total_assets'),
    realizedPnl: numberValue(row, 'realized_pnl'),
    floatingPnl: numberValue(row, 'floating_pnl'),
    totalPnl: numberValue(row, 'total_pnl'),
    totalReturnRate: numberValue(row, 'total_return_rate'),
    positionCount: numberValue(row, 'position_count'),
    tradeCount: numberValue(row, 'trade_count'),
    note: text(row, 'note'),
  }
}

function mapModelPrediction(row: Row): ModelPrediction {
  return {
    id: text(row, 'id'),
    predictionDate: text(row, 'prediction_date'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    modelName: text(row, 'model_name'),
    modelVersion: text(row, 'model_version'),
    featureSet: text(row, 'feature_set'),
    score: numberValue(row, 'score'),
    rank: numberValue(row, 'rank'),
    predictedReturn: numberValue(row, 'predicted_return'),
    confidence: numberValue(row, 'confidence'),
    closePrice: numberValue(row, 'close_price'),
    featureWindowStart: text(row, 'feature_window_start'),
    featureWindowEnd: text(row, 'feature_window_end'),
  }
}

function mapModelDecision(row: Row): ModelDecision {
  return {
    id: text(row, 'id'),
    decisionTime: formatDateTime(row.decision_time),
    decisionDate: text(row, 'decision_date'),
    strategyAccount: text(row, 'strategy_account'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    modelName: text(row, 'model_name'),
    modelVersion: text(row, 'model_version'),
    action: text(row, 'action'),
    reason: text(row, 'reason'),
    riskGateStatus: text(row, 'risk_gate_status'),
    riskGateReason: text(row, 'risk_gate_reason'),
    targetWeight: numberValue(row, 'target_weight'),
    plannedShares: numberValue(row, 'planned_shares'),
    status: text(row, 'status'),
  }
}

function mapModelPosition(row: Row): ModelPosition {
  return {
    id: text(row, 'id'),
    strategyAccount: text(row, 'strategy_account'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    costPrice: numberValue(row, 'cost_price'),
    shares: numberValue(row, 'shares'),
    currentPrice: numberValue(row, 'current_price'),
    marketValue: numberValue(row, 'market_value'),
    floatingPnl: numberValue(row, 'floating_pnl'),
    pnlRate: numberValue(row, 'pnl_rate'),
    buyDate: text(row, 'buy_date'),
    currentSuggestion: text(row, 'current_suggestion'),
    status: text(row, 'status'),
    modelName: text(row, 'model_name'),
    modelVersion: text(row, 'model_version'),
  }
}

function mapModelOrder(row: Row): ModelOrder {
  return {
    ...mapPaperTradeOrder(row),
    strategyAccount: text(row, 'strategy_account'),
    modelName: text(row, 'model_name'),
    modelVersion: text(row, 'model_version'),
  }
}

function mapModelPortfolioSnapshot(row: Row): ModelPortfolioSnapshot {
  return {
    ...mapPortfolioSnapshot(row),
    strategyAccount: text(row, 'strategy_account'),
    maxDrawdownRate: numberValue(row, 'max_drawdown_rate'),
    consecutiveLosses: numberValue(row, 'consecutive_losses'),
    modelName: text(row, 'model_name'),
    modelVersion: text(row, 'model_version'),
  }
}

function mapBacktestRun(row: Row): BacktestRun {
  return {
    id: text(row, 'id'),
    runTime: formatDateTime(row.run_time),
    strategyName: text(row, 'strategy_name'),
    benchmarkName: text(row, 'benchmark_name'),
    startDate: text(row, 'start_date'),
    endDate: text(row, 'end_date'),
    initialCash: numberValue(row, 'initial_cash'),
    finalValue: numberValue(row, 'final_value'),
    totalReturnRate: numberValue(row, 'total_return_rate'),
    annualReturnRate: numberValue(row, 'annual_return_rate'),
    benchmarkReturnRate: numberValue(row, 'benchmark_return_rate'),
    benchmarkCsi300ReturnRate: numberValue(row, 'benchmark_csi300_return_rate'),
    benchmarkCsi500ReturnRate: numberValue(row, 'benchmark_csi500_return_rate'),
    excessReturnRate: numberValue(row, 'excess_return_rate'),
    equityReconciled: Boolean(row.equity_reconciled ?? false),
    maxDrawdownRate: numberValue(row, 'max_drawdown_rate'),
    sharpeRatio: numberValue(row, 'sharpe_ratio'),
    calmarRatio: numberValue(row, 'calmar_ratio'),
    winRate: numberValue(row, 'win_rate'),
    profitLossRatio: numberValue(row, 'profit_loss_ratio'),
    turnoverRate: numberValue(row, 'turnover_rate'),
    consecutiveLosses: numberValue(row, 'consecutive_losses'),
    largestSingleLoss: numberValue(row, 'largest_single_loss'),
    sampleSplitSummary: jsonValue(row, 'sample_split_summary'),
    parameterSensitivitySummary: jsonValue(row, 'parameter_sensitivity_summary'),
    tradeCount: numberValue(row, 'trade_count'),
    avgHoldingDays: numberValue(row, 'avg_holding_days'),
    missedRunnerCount: numberValue(row, 'missed_runner_count'),
    note: text(row, 'note'),
  }
}

function mapBacktestEquityPoint(row: Row): BacktestEquityPoint {
  return {
    id: text(row, 'id'),
    runId: text(row, 'run_id'),
    curveDate: text(row, 'curve_date'),
    equityValue: numberValue(row, 'equity_value'),
    dailyReturnRate: numberValue(row, 'daily_return_rate'),
    drawdownRate: numberValue(row, 'drawdown_rate'),
    benchmarkValue: numberValue(row, 'benchmark_value'),
    benchmarkReturnRate: numberValue(row, 'benchmark_return_rate'),
  }
}

function mapBacktestTrade(row: Row): BacktestTrade {
  return {
    id: text(row, 'id'),
    runId: text(row, 'run_id'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    entryDate: text(row, 'entry_date'),
    exitDate: text(row, 'exit_date'),
    entryPrice: numberValue(row, 'entry_price'),
    exitPrice: numberValue(row, 'exit_price'),
    shares: numberValue(row, 'shares'),
    pnlAmount: numberValue(row, 'pnl_amount'),
    pnlRate: numberValue(row, 'pnl_rate'),
    feeAmount: numberValue(row, 'fee_amount'),
    slippageAmount: numberValue(row, 'slippage_amount'),
    holdingDays: numberValue(row, 'holding_days'),
    exitReason: text(row, 'exit_reason'),
  }
}

function mapMissedRunner(row: Row): MissedRunner {
  return {
    id: text(row, 'id'),
    runId: text(row, 'run_id'),
    pickDate: text(row, 'pick_date'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    pickPrice: numberValue(row, 'pick_price'),
    maxPrice: numberValue(row, 'max_price'),
    maxReturnRate: numberValue(row, 'max_return_rate'),
    daysToHigh: numberValue(row, 'days_to_high'),
    reason: text(row, 'reason'),
  }
}

function mapSignalEvent(row: Row): SignalEvent {
  const executionStatus = signalExecutionStatus(row)
  return {
    id: text(row, 'id'),
    signalTime: formatDateTime(row.signal_time),
    signalDate: text(row, 'signal_date'),
    code: text(row, 'code'),
    name: text(row, 'name'),
    sourceType: text(row, 'source_type'),
    signalType: text(row, 'signal_type', '观察') as SignalEvent['signalType'],
    status: text(row, 'status', '新信号') as SignalEvent['status'],
    triggerPrice: numberValue(row, 'trigger_price'),
    currentPrice: numberValue(row, 'current_price'),
    changeRate: numberValue(row, 'change_rate'),
    buyPriceText: text(row, 'buy_price_text'),
    sellPriceText: text(row, 'sell_price_text'),
    stopLoss: numberValue(row, 'stop_loss'),
    targetPrice1: optionalNumber(row, 'target_price_1'),
    finalAction: text(row, 'final_action'),
    reason: text(row, 'reason'),
    risk: text(row, 'risk'),
    executionStatus,
    executionStatusText: signalExecutionStatusText[executionStatus],
    executionOrderId: text(row, 'execution_order_id'),
    executionReason: translateStockText(row.execution_reason),
    executionHandledAt: formatDateTime(row.execution_handled_at),
    createdAt: formatDateTime(row.created_at),
  }
}

export interface StockRepository {
  getOverview(): Promise<OverviewStats>
  getRoughStocks(): Promise<RoughStock[]>
  getFineStocks(): Promise<FineStock[]>
  getRealtimeDecisions(): Promise<RealtimeDecision[]>
  getHoldings(): Promise<HoldingStock[]>
  getTradeRecords(): Promise<TradeRecord[]>
  getTasks(): Promise<TaskRecord[]>
  getPaperTradeOrders(): Promise<PaperTradeOrder[]>
  getPortfolioSnapshots(): Promise<PortfolioSnapshot[]>
  getBacktestRuns(): Promise<BacktestRun[]>
  getBacktestTrades(): Promise<BacktestTrade[]>
  getMissedRunners(): Promise<MissedRunner[]>
  getBacktestEquityCurve(): Promise<BacktestEquityPoint[]>
  getSignalEvents(): Promise<SignalEvent[]>
  getModelPredictions(): Promise<ModelPrediction[]>
  getModelDecisions(): Promise<ModelDecision[]>
  getModelPositions(): Promise<ModelPosition[]>
  getModelOrders(): Promise<ModelOrder[]>
  getModelPortfolioSnapshots(): Promise<ModelPortfolioSnapshot[]>
  getHistoricalFineStocks(): Promise<FineStock[]>
  addHolding(input: AddHoldingInput): Promise<HoldingStock>
  saveTrade(input: SaveTradeInput): Promise<SaveTradeResult>
  confirmSignalBuy(input: ConfirmSignalBuyInput): Promise<HoldingStock>
  markSignalEvent(id: string, status: SignalEvent['status'], executionReason?: string): Promise<void>
  recordTTrade(input: RecordTTradeInput): Promise<SaveTradeResult>
  requestJob(jobType: StockJobType): Promise<void>
}

export function createStockRepository(client: StockSupabaseClient = supabase): StockRepository {
  let localHoldings: HoldingStock[] | null = null

  async function localHoldingList() {
    if (!localHoldings) localHoldings = await stocksApi.getHoldings()
    return [...localHoldings]
  }

  async function selectRows(table: string, orderColumn: string, filters: Record<string, string> = {}) {
    if (!client) return null
    try {
      let query = client.from(table).select('*')
      for (const [column, value] of Object.entries(filters)) {
        query = query.eq(column, value)
      }
      const { data, error } = await withTimeout(query.order(orderColumn, { ascending: false }))
      if (error || !data) return null
      return data as Row[]
    } catch {
      return null
    }
  }

  return {
    async getOverview() {
      if (!client) return stocksApi.getOverview()
      const [rough, fine, live, holdings] = await Promise.all([
        this.getRoughStocks(),
        this.getFineStocks(),
        this.getRealtimeDecisions(),
        this.getHoldings(),
      ])
      return {
        roughCount: rough.length,
        fineCount: fine.length,
        holdingCount: holdings.length,
        buyableCount: live.filter((item) => item.canBuy).length,
        alertCount: live.filter((item) => item.status === '止损/风控' || item.status === '止盈').length,
        lastUpdateTime: live[0]?.updateTime || new Date().toLocaleString(),
      }
    },
    async getRoughStocks() {
      const rows = await selectRows('stock_scan_results', 'scan_date')
      if (!client) return stocksApi.getRoughStocks()
      return (rows ?? []).map(mapRoughStock)
    },
    async getFineStocks() {
      const rows = await selectRows('stock_strong_picks', 'scan_date')
      if (!client) return stocksApi.getFineStocks()
      return (rows ?? []).map(mapFineStock)
    },
    async getRealtimeDecisions() {
      const rows = await selectRows('stock_live_decisions', 'updated_at')
      if (!client) return stocksApi.getRealtimeDecisions()
      return (rows ?? []).map(mapRealtimeDecision)
    },
    async getHoldings() {
      const rows = await selectRows('stock_positions', 'created_at', { status: 'open' })
      if (rows) return rows.map(mapHolding)
      if (client) return []
      return localHoldingList()
    },
    async getTradeRecords() {
      const rows = await selectRows('stock_trade_history', 'sell_date')
      if (!client) return stocksApi.getTradeRecords()
      return (rows ?? []).map(mapTrade)
    },
    async getTasks() {
      const rows = await selectRows('stock_job_runs', 'started_at')
      if (!client) return stocksApi.getTasks()
      return (rows ?? []).map(mapTask)
    },
    async getPaperTradeOrders() {
      const rows = await selectRows('stock_auto_trade_orders', 'order_time')
      return (rows ?? []).map(mapPaperTradeOrder)
    },
    async getPortfolioSnapshots() {
      const rows = await selectRows('stock_portfolio_snapshots', 'snapshot_time')
      return (rows ?? []).map(mapPortfolioSnapshot)
    },
    async getBacktestRuns() {
      const rows = await selectRows('stock_backtest_runs', 'run_time')
      return (rows ?? []).map(mapBacktestRun)
    },
    async getBacktestTrades() {
      const rows = await selectRows('stock_backtest_trades', 'entry_date')
      return (rows ?? []).map(mapBacktestTrade)
    },
    async getMissedRunners() {
      const rows = await selectRows('stock_missed_runners', 'max_return_rate')
      return (rows ?? []).map(mapMissedRunner)
    },
    async getBacktestEquityCurve() {
      const rows = await selectRows('stock_backtest_equity_curve', 'curve_date')
      return (rows ?? []).map(mapBacktestEquityPoint)
    },
    async getSignalEvents() {
      const rows = await selectRows('stock_signal_events', 'signal_time')
      if (!client) return []
      return (rows ?? []).map(mapSignalEvent)
    },
    async getModelPredictions() {
      const rows = await selectRows('stock_model_predictions', 'prediction_date')
      return (rows ?? []).map(mapModelPrediction)
    },
    async getModelDecisions() {
      const rows = await selectRows('stock_model_decisions', 'decision_time')
      return (rows ?? []).map(mapModelDecision)
    },
    async getModelPositions() {
      const rows = await selectRows('stock_model_positions', 'updated_at', { status: 'open' })
      return (rows ?? []).map(mapModelPosition)
    },
    async getModelOrders() {
      const rows = await selectRows('stock_model_orders', 'order_time')
      return (rows ?? []).map(mapModelOrder)
    },
    async getModelPortfolioSnapshots() {
      const rows = await selectRows('stock_model_portfolio_snapshots', 'snapshot_time')
      return (rows ?? []).map(mapModelPortfolioSnapshot)
    },
    async getHistoricalFineStocks() {
      const rows = await selectRows('stock_strong_picks', 'scan_date')
      if (!client) return stocksApi.getFineStocks()
      return (rows ?? []).map(mapFineStock)
    },
    async addHolding(input) {
      const holding = calculateHolding(input)
      if (client) {
        try {
          const { data, error } = await withTimeout(client.from('stock_positions').insert({
            code: holding.code,
            name: holding.name,
            cost_price: holding.costPrice,
            shares: holding.shares,
            current_price: holding.currentPrice,
            market_value: holding.marketValue,
            floating_pnl: holding.floatingPnl,
            pnl_rate: holding.pnlRate,
            buy_date: holding.buyDate,
            holding_days: holding.holdingDays,
            current_suggestion: holding.currentSuggestion,
            buy_memo: input.buyMemo ?? '',
            status: 'open',
          }).select().single())
          if (!error && data) return mapHolding(data as Row)
        } catch {
          throw new Error('持仓保存失败：请先用管理员账号登录后厨重地，并确认股票表已创建。')
        }
      }
      localHoldings = [holding, ...(await localHoldingList())]
      return holding
    },
    async saveTrade(input) {
      const holding = input.holding
      if (input.action === '加仓') {
        const totalCost = holding.costPrice * holding.shares + input.price * input.shares
        const nextShares = holding.shares + input.shares
        const nextHolding = recalculateHolding(holding, nextShares, totalCost / nextShares, input.price)
        if (client) {
          try {
            const { data, error } = await withTimeout(client
              .from('stock_positions')
              .update({
                cost_price: nextHolding.costPrice,
                shares: nextHolding.shares,
                current_price: nextHolding.currentPrice,
                market_value: nextHolding.marketValue,
                floating_pnl: nextHolding.floatingPnl,
                pnl_rate: nextHolding.pnlRate,
                current_suggestion: input.memo || nextHolding.currentSuggestion,
                updated_at: new Date().toISOString(),
              })
              .eq('code', holding.code)
              .eq('status', 'open')
              .select()
              .single())
            if (error) throw error
            return { holding: mapHolding(data as Row) }
          } catch {
            throw new Error('加仓保存失败：请先用管理员账号登录后厨重地，并确认股票表已创建。')
          }
        }
        localHoldings = (await localHoldingList()).map((item) => item.code === holding.code ? nextHolding : item)
        return { holding: nextHolding }
      }

      const tradeRecord = tradeRecordFromInput(input)
      const nextShares = Math.max(holding.shares - input.shares, 0)
      const nextHolding = nextShares > 0
        ? recalculateHolding(holding, nextShares, holding.costPrice, input.price)
        : null

      if (client) {
        try {
          const { error: tradeError } = await withTimeout(client.from('stock_trade_history').insert({
            code: tradeRecord.code,
            name: tradeRecord.name,
            buy_date: tradeRecord.buyDate,
            sell_date: tradeRecord.sellDate,
            cost_price: tradeRecord.costPrice,
            sell_price: tradeRecord.sellPrice,
            shares: tradeRecord.shares,
            pnl_amount: tradeRecord.pnlAmount,
            pnl_rate: tradeRecord.pnlRate,
            buy_memo: tradeRecord.buyMemo,
            sell_memo: tradeRecord.sellMemo,
            is_cleared: tradeRecord.isCleared,
          }))
          if (tradeError) throw tradeError

          const updatePayload = nextHolding ? {
            shares: nextHolding.shares,
            current_price: nextHolding.currentPrice,
            market_value: nextHolding.marketValue,
            floating_pnl: nextHolding.floatingPnl,
            pnl_rate: nextHolding.pnlRate,
            current_suggestion: input.memo || nextHolding.currentSuggestion,
            updated_at: new Date().toISOString(),
          } : {
            shares: 0,
            current_price: input.price,
            market_value: 0,
            floating_pnl: tradeRecord.pnlAmount,
            pnl_rate: tradeRecord.pnlRate,
            current_suggestion: input.memo,
            status: 'closed',
            updated_at: new Date().toISOString(),
          }

          const { data, error } = await withTimeout(client
            .from('stock_positions')
            .update(updatePayload)
            .eq('code', holding.code)
            .eq('status', 'open')
            .select()
            .maybeSingle())
          if (error) throw error
          return { holding: data && nextHolding ? mapHolding(data as Row) : null, tradeRecord }
        } catch {
          throw new Error('卖出记录保存失败：请先用管理员账号登录后厨重地，并确认股票表已创建。')
        }
      }

      localHoldings = (await localHoldingList())
        .map((item) => item.code === holding.code ? nextHolding : item)
        .filter((item): item is HoldingStock => Boolean(item))
      return { holding: nextHolding, tradeRecord }
    },
    async confirmSignalBuy(input) {
      const holding = await this.addHolding({
        code: input.signal.code,
        name: input.signal.name,
        costPrice: input.price,
        shares: input.shares,
        currentPrice: input.price,
        buyDate: input.buyDate,
        currentSuggestion: input.signal.finalAction || input.signal.reason,
        buyMemo: input.memo,
      })
      await this.markSignalEvent(input.signal.id, '已买入', '手动记录买入')
      return holding
    },
    async markSignalEvent(id, status, executionReason) {
      if (!client) return
      try {
        const executionStatus: SignalExecutionStatus = status === '已忽略' ? 'ignored' : 'manual_executed'
        const fallbackReason: Record<SignalEvent['status'], string> = {
          新信号: '',
          已买入: '手动记录买入',
          已卖出: '手动记录卖出',
          已忽略: '用户忽略信号',
          已记录T: '手动记录做T',
        }
        const result = await withTimeout(client
          .from('stock_signal_events')
          .update({
            status,
            handled_at: new Date().toISOString(),
            execution_status: executionStatus,
            execution_reason: executionReason ?? fallbackReason[status],
            execution_handled_at: new Date().toISOString(),
          })
          .eq('id', id))
        const error = (result as { error?: unknown }).error
        if (error) throw error
      } catch {
        throw new Error('信号状态更新失败：请确认 stock_signal_events 表已创建，并已登录管理员账号。')
      }
    },
    async recordTTrade(input) {
      const result = await this.saveTrade({
        action: input.action === '做T买' ? '加仓' : '减仓',
        holding: input.holding,
        price: input.price,
        shares: input.shares,
        tradeDate: input.tradeDate,
        memo: input.memo || input.action,
      })
      await this.markSignalEvent(input.signal.id, '已记录T', input.action === '做T买' ? '手动记录做T买入' : '手动记录做T卖出')
      return result
    },
    async requestJob(jobType) {
      if (!client) {
        throw new Error('当前没有配置 Supabase，不能提交线上任务请求。')
      }
      try {
        const { error } = await withTimeout(client.from('stock_job_requests').insert({
          job_type: jobType,
          status: 'pending',
          requested_at: new Date().toISOString(),
        }))
        if (error) throw error
      } catch {
        throw new Error('任务请求提交失败：请确认已登录管理员账号，并已创建 stock_job_requests 表。')
      }
    },
  }
}

export const stockRepository = createStockRepository()
