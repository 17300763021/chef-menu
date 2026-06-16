import type { SupabaseClient } from '@supabase/supabase-js'
import { supabase } from '../../lib/supabase'
import { stocksApi } from './mockApi'
import type {
  AddHoldingInput,
  FineStock,
  HoldingStock,
  OverviewStats,
  RealtimeDecision,
  RoughStock,
  SaveTradeInput,
  SaveTradeResult,
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

function optionalNumber(row: Row, key: string) {
  const value = row[key]
  return value === null || value === undefined ? null : Number(value)
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

async function withTimeout<T>(request: PromiseLike<T>, milliseconds = 800): Promise<T> {
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
    updateTime: text(row, 'update_time'),
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
    currentSuggestion: text(row, 'current_suggestion'),
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
    buyMemo: text(row, 'buy_memo'),
    sellMemo: text(row, 'sell_memo'),
    isCleared: Boolean(row.is_cleared),
  }
}

function mapTask(row: Row): TaskRecord {
  return {
    id: text(row, 'id'),
    type: text(row, 'job_type'),
    startTime: text(row, 'started_at'),
    endTime: text(row, 'finished_at', '--'),
    status: text(row, 'status', '成功') as TaskRecord['status'],
    importedCount: numberValue(row, 'imported_count'),
    errorMsg: text(row, 'error_message'),
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
  addHolding(input: AddHoldingInput): Promise<HoldingStock>
  saveTrade(input: SaveTradeInput): Promise<SaveTradeResult>
  requestJob(jobType: StockJobType): Promise<void>
}

export function createStockRepository(client: StockSupabaseClient = supabase): StockRepository {
  let localHoldings: HoldingStock[] | null = null

  async function localHoldingList() {
    if (!localHoldings) localHoldings = await stocksApi.getHoldings()
    return [...localHoldings]
  }

  async function selectRows(table: string, orderColumn: string) {
    if (!client) return null
    try {
      const { data, error } = await withTimeout(
        client.from(table).select('*').order(orderColumn, { ascending: false }),
      )
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
      if (!rough.length && !fine.length && !live.length) return stocksApi.getOverview()
      return {
        roughCount: rough.length,
        fineCount: fine.length,
        holdingCount: holdings.length,
        buyableCount: live.filter((item) => item.status === '可买入').length,
        alertCount: live.filter((item) => item.status === '止损/风控' || item.status === '止盈').length,
        lastUpdateTime: live[0]?.updateTime || new Date().toLocaleString(),
      }
    },
    async getRoughStocks() {
      const rows = await selectRows('stock_scan_results', 'scan_date')
      return rows?.map(mapRoughStock) ?? stocksApi.getRoughStocks()
    },
    async getFineStocks() {
      const rows = await selectRows('stock_strong_picks', 'scan_date')
      return rows?.map(mapFineStock) ?? stocksApi.getFineStocks()
    },
    async getRealtimeDecisions() {
      const rows = await selectRows('stock_live_decisions', 'updated_at')
      return rows?.map(mapRealtimeDecision) ?? stocksApi.getRealtimeDecisions()
    },
    async getHoldings() {
      const rows = await selectRows('stock_positions', 'created_at')
      if (rows) return rows.map(mapHolding)
      return localHoldingList()
    },
    async getTradeRecords() {
      const rows = await selectRows('stock_trade_history', 'sell_date')
      return rows?.map(mapTrade) ?? stocksApi.getTradeRecords()
    },
    async getTasks() {
      const rows = await selectRows('stock_job_runs', 'started_at')
      return rows?.map(mapTask) ?? stocksApi.getTasks()
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
