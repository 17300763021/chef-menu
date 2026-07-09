import type {
  FineStock,
  HoldingStock,
  MarketRegime,
  OverviewStats,
  RealtimeDecision,
  RoughStock,
  TaskRecord,
  TradeRecord,
} from './types'

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

const today = '2026-06-16'

const marketRegime: MarketRegime = {
  id: 'mock-regime',
  regimeDate: today,
  regime: '震荡市',
  csi300Close: 3950,
  csi300Ma20: 3920,
  csi300Ma60: 3880,
  marketTurnoverYi: 9500,
  limitUpCount: 65,
  limitDownCount: 12,
  breakRatePct: 22.5,
  advanceDeclineRatio: 1.3,
  positionCapPct: 40,
  regimeNote: '模拟数据：大盘震荡，方向不明，模拟仓位上限40%',
  breadthSource: 'mock',
}

const roughStocks: RoughStock[] = [
  {
    code: '600988',
    name: '赤峰黄金',
    date: today,
    score: 88,
    prevClose: 54,
    signal: '回踩20日线低吸买点',
    action: '盘中确认后小仓',
    supportLevel: 52.8,
    resistanceLevel: 57.2,
    stopLoss: 51.6,
    reason: '趋势保持，量能温和，靠近支撑区',
    risk: '黄金板块波动放大，追高风险较高',
    factorTrend: 82,
    factorMomentum: 76,
    factorVolume: 63,
    factorFlow: 72,
    factorQuality: 58,
    sectorRank: 3,
  },
  {
    code: '603993',
    name: '洛阳钼业',
    date: today,
    score: 84,
    prevClose: 19.69,
    signal: '趋势尚可，但买点不标准',
    action: '观察，不追买',
    supportLevel: 18.9,
    resistanceLevel: 20.8,
    stopLoss: 18.3,
    reason: '中期均线多头，板块强度仍在',
    risk: '距离压力位空间不足',
    factorTrend: 78,
    factorMomentum: 68,
    factorVolume: 59,
    factorFlow: 61,
    factorQuality: 64,
    sectorRank: 7,
  },
  {
    code: '002600',
    name: '领益智造',
    date: today,
    score: 79,
    prevClose: 14.69,
    signal: '站回20日线修复买点',
    action: '等待放量确认',
    supportLevel: 14.2,
    resistanceLevel: 15.4,
    stopLoss: 13.8,
    reason: '修复形态出现，短线资金回流',
    risk: '消费电子板块轮动较快',
    factorTrend: 70,
    factorMomentum: 73,
    factorVolume: 66,
    factorFlow: 57,
    factorQuality: 60,
    sectorRank: 9,
  },
]

const fineStocks: FineStock[] = [
  {
    ...roughStocks[0],
    strategyLevel: '重点池',
    reviewStatus: '通过，盘中确认',
  },
  {
    ...roughStocks[2],
    strategyLevel: '观察转强',
    reviewStatus: '需放量',
  },
]

const realtimeDecisions: RealtimeDecision[] = [
  {
    code: '600988',
    name: '赤峰黄金',
    date: today,
    updateTime: '10:31:24',
    operationType: '候选买入',
    currentPrice: 54.86,
    changeRate: 1.59,
    canBuy: true,
    suggestBuyPrice: 54.2,
    suggestSellPrice: 57.1,
    stopLoss: 51.6,
    targetPrice1: 56.4,
    finalAction: '回落到买入区间再小仓，不追高',
    status: '可买入',
  },
  {
    code: '600633',
    name: '浙数文化',
    date: today,
    updateTime: '10:31:24',
    operationType: '持仓风控',
    currentPrice: 13.52,
    changeRate: -3.08,
    canBuy: false,
    suggestBuyPrice: null,
    suggestSellPrice: 13.48,
    stopLoss: 13.5,
    targetPrice1: null,
    finalAction: '跌破防守位，优先减仓观察',
    sellReason: '贴近止损位，短线走弱',
    status: '止损/风控',
  },
  {
    code: '603806',
    name: '瑞斯康达',
    date: today,
    updateTime: '10:31:24',
    operationType: '持仓跟踪',
    currentPrice: 17.58,
    changeRate: 6.74,
    canBuy: false,
    suggestBuyPrice: null,
    suggestSellPrice: 17.6,
    stopLoss: 15.9,
    targetPrice1: 17.4,
    finalAction: '触及第一止盈，按计划记录减仓',
    sellReason: '达到第一止盈价',
    status: '止盈',
  },
]

const holdings: HoldingStock[] = [
  {
    code: '600988',
    name: '赤峰黄金',
    costPrice: 54,
    shares: 100,
    currentPrice: 54.86,
    marketValue: 5486,
    floatingPnl: 86,
    pnlRate: 1.59,
    buyDate: '2026-06-12',
    holdingDays: 4,
    currentSuggestion: '继续跟踪，低吸不追高',
  },
  {
    code: '600633',
    name: '浙数文化',
    costPrice: 13.95,
    shares: 100,
    currentPrice: 13.52,
    marketValue: 1352,
    floatingPnl: -43,
    pnlRate: -3.08,
    buyDate: '2026-06-13',
    holdingDays: 3,
    currentSuggestion: '贴近止损，准备减仓',
  },
]

const trades: TradeRecord[] = [
  {
    code: '601318',
    name: '中国平安',
    buyDate: '2026-05-20',
    sellDate: '2026-05-29',
    costPrice: 45,
    sellPrice: 48.5,
    shares: 200,
    pnlAmount: 700,
    pnlRate: 7.78,
    buyMemo: '突破平台后回踩确认',
    sellMemo: '达到第一止盈位，按计划清仓',
    isCleared: true,
  },
]

const tasks: TaskRecord[] = [
  {
    id: 'scan-20260616',
    type: '夜间海选同步',
    startTime: '2026-06-16 16:05:00',
    endTime: '2026-06-16 16:07:18',
    status: '成功',
    importedCount: 142,
  },
  {
    id: 'live-20260616',
    type: '盘中实时决策',
    startTime: '2026-06-16 10:31:00',
    endTime: '2026-06-16 10:31:24',
    status: '成功',
    importedCount: 18,
  },
]

export const stocksApi = {
  async getOverview(): Promise<OverviewStats> {
    await delay(80)
    return {
      roughCount: roughStocks.length,
      fineCount: fineStocks.length,
      holdingCount: holdings.length,
      buyableCount: realtimeDecisions.filter((item) => item.status === '可买入').length,
      alertCount: realtimeDecisions.filter((item) => item.status === '止损/风控' || item.status === '止盈').length,
      lastUpdateTime: '2026-06-16 10:31:24',
    }
  },
  async getRoughStocks(): Promise<RoughStock[]> {
    await delay(80)
    return roughStocks
  },
  async getFineStocks(): Promise<FineStock[]> {
    await delay(80)
    return fineStocks
  },
  async getRealtimeDecisions(): Promise<RealtimeDecision[]> {
    await delay(80)
    return realtimeDecisions
  },
  async getHoldings(): Promise<HoldingStock[]> {
    await delay(80)
    return holdings
  },
  async getTradeRecords(): Promise<TradeRecord[]> {
    await delay(80)
    return trades
  },
  async getTasks(): Promise<TaskRecord[]> {
    await delay(80)
    return tasks
  },
  async getMarketRegime(): Promise<MarketRegime> {
    await delay(80)
    return marketRegime
  },
  async submitTradeForm(): Promise<{ success: true }> {
    await delay(120)
    return { success: true }
  },
}
