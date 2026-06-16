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

export type StockJobType = 'night_scan' | 'live_decision' | 'sync_latest'

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

export interface TaskRecord {
  id: string
  type: string
  startTime: string
  endTime: string
  status: TaskStatus
  importedCount: number
  errorMsg?: string
}
