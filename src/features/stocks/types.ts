export type DecisionStatus = '可买入' | '止损/风控' | '止盈' | '持有观察' | '不买/无动作'
export type TaskStatus = '成功' | '失败' | '运行中'

export interface OverviewStats {
  roughCount: number
  fineCount: number
  holdingCount: number
  buyableCount: number
  alertCount: number
  lastUpdateTime: string
}

export interface BaseStock {
  code: string
  name: string
  date: string
}

export interface RoughStock extends BaseStock {
  score: number
  prevClose: number
  signal: string
  action: string
  supportLevel: number
  resistanceLevel: number
  stopLoss: number
  reason: string
  risk: string
}

export interface FineStock extends RoughStock {
  strategyLevel: string
  reviewStatus: string
}

export interface RealtimeDecision extends BaseStock {
  updateTime: string
  operationType: string
  currentPrice: number
  changeRate: number
  canBuy: boolean
  suggestBuyPrice: number | null
  suggestSellPrice: number | null
  stopLoss: number
  targetPrice1: number | null
  finalAction: string
  noBuyReason?: string
  sellReason?: string
  status: DecisionStatus
}

export interface HoldingStock {
  id?: string
  code: string
  name: string
  costPrice: number
  shares: number
  currentPrice: number
  marketValue: number
  floatingPnl: number
  pnlRate: number
  buyDate: string
  holdingDays: number
  currentSuggestion: string
}

export interface AddHoldingInput {
  code: string
  name: string
  costPrice: number
  shares: number
  currentPrice: number
  buyDate: string
  currentSuggestion: string
  buyMemo?: string
}

export type TradeAction = '加仓' | '减仓' | '清仓'
export type SignalEventType = '买入' | '卖出' | '减仓' | '止损' | '止盈' | '做T买' | '做T卖' | '观察'
export type SignalEventStatus = '新信号' | '已买入' | '已卖出' | '已忽略' | '已记录T'

export interface SaveTradeInput {
  action: TradeAction
  holding: HoldingStock
  price: number
  shares: number
  tradeDate: string
  memo: string
}

export interface SaveTradeResult {
  holding: HoldingStock | null
  tradeRecord?: TradeRecord
}

export type StockJobType = 'night_scan' | 'live_decision' | 'paper_trade' | 'backtest' | 'sync_latest'

export interface BacktestRun {
  id: string
  runTime: string
  strategyName: string
  startDate: string
  endDate: string
  initialCash: number
  finalValue: number
  totalReturnRate: number
  maxDrawdownRate: number
  winRate: number
  profitLossRatio: number
  tradeCount: number
  avgHoldingDays: number
  missedRunnerCount: number
  note: string
}

export interface BacktestTrade {
  id: string
  runId: string
  code: string
  name: string
  entryDate: string
  exitDate: string
  entryPrice: number
  exitPrice: number
  shares: number
  pnlAmount: number
  pnlRate: number
  holdingDays: number
  exitReason: string
}

export interface MissedRunner {
  id: string
  runId: string
  pickDate: string
  code: string
  name: string
  pickPrice: number
  maxPrice: number
  maxReturnRate: number
  daysToHigh: number
  reason: string
}

export interface SignalEvent {
  id: string
  signalTime: string
  signalDate: string
  code: string
  name: string
  sourceType: string
  signalType: SignalEventType
  status: SignalEventStatus
  triggerPrice: number
  currentPrice: number
  changeRate: number
  buyPriceText: string
  sellPriceText: string
  stopLoss: number
  targetPrice1: number | null
  finalAction: string
  reason: string
  risk: string
  createdAt: string
}

export interface ConfirmSignalBuyInput {
  signal: SignalEvent
  shares: number
  price: number
  buyDate: string
  memo: string
}

export interface RecordTTradeInput {
  signal: SignalEvent
  holding: HoldingStock
  action: '做T买' | '做T卖'
  price: number
  shares: number
  tradeDate: string
  memo: string
}

export interface TradeRecord {
  code: string
  name: string
  buyDate: string
  sellDate: string
  costPrice: number
  sellPrice: number
  shares: number
  pnlAmount: number
  pnlRate: number
  buyMemo: string
  sellMemo: string
  isCleared: boolean
}

export interface PaperTradeOrder {
  id: string
  orderTime: string
  orderDate: string
  code: string
  name: string
  side: 'buy' | 'sell'
  reason: string
  price: number
  shares: number
  amount: number
  cashBefore: number
  cashAfter: number
  realizedPnl: number
  status: string
}

export interface PortfolioSnapshot {
  id: string
  snapshotTime: string
  snapshotDate: string
  cash: number
  holdingMarketValue: number
  totalAssets: number
  realizedPnl: number
  floatingPnl: number
  totalPnl: number
  totalReturnRate: number
  positionCount: number
  tradeCount: number
  note: string
}

export interface TaskRecord {
  id: string
  type: string
  startTime: string
  endTime: string
  status: TaskStatus
  importedCount: number
  errorMsg?: string
}
