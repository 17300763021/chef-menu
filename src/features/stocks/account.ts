import type { HoldingStock, SignalEvent, TradeRecord } from './types'

export interface AccountConfig {
  initialCapital: number
  maxHoldings: number
  normalInitialRate: number
  strongInitialRate: number
  maxSinglePositionRate: number
  tTradeRate: number
  riskRate: number
  weakRiskRate: number
  cashReserveRate: number
}

export interface PositionAllocation {
  code: string
  name: string
  costAmount: number
  marketValue: number
  floatingPnl: number
  pnlRate: number
  allocationRate: number
  pnlContributionRate: number
  overSinglePositionLimit: boolean
}

export interface AccountSummary {
  initialCapital: number
  cashReserve: number
  cash: number
  totalAssets: number
  openCostBasis: number
  holdingMarketValue: number
  realizedPnl: number
  floatingPnl: number
  totalPnl: number
  totalReturnRate: number
  positionCount: number
  positionCountWarning: string
  positions: PositionAllocation[]
}

export interface BuyRecommendation {
  targetAmount: number
  maxShares: number
  estimatedAmount: number
  riskBudget: number
  reason: string
}

export const DEFAULT_ACCOUNT_CONFIG: AccountConfig = {
  initialCapital: 1_000_000,
  maxHoldings: 6,
  normalInitialRate: 0.08,
  strongInitialRate: 0.10,
  maxSinglePositionRate: 0.15,
  tTradeRate: 0.05,
  riskRate: 0.01,
  weakRiskRate: 0.005,
  cashReserveRate: 0.25,
}

function round2(value: number) {
  return Math.round(value * 100) / 100
}

function roundLot(shares: number) {
  return Math.max(0, Math.floor(shares / 100) * 100)
}

export function buildAccountSummary(
  holdings: HoldingStock[],
  trades: TradeRecord[],
  config = DEFAULT_ACCOUNT_CONFIG,
): AccountSummary {
  const openCostBasis = holdings.reduce((sum, item) => sum + item.costPrice * item.shares, 0)
  const holdingMarketValue = holdings.reduce((sum, item) => sum + item.marketValue, 0)
  const floatingPnl = holdings.reduce((sum, item) => sum + item.floatingPnl, 0)
  const realizedPnl = trades.reduce((sum, item) => sum + item.pnlAmount, 0)
  const totalPnl = realizedPnl + floatingPnl
  const totalAssets = config.initialCapital + totalPnl
  const cash = totalAssets - holdingMarketValue
  const maxSinglePositionAmount = totalAssets * config.maxSinglePositionRate
  const positions = holdings.map((item) => {
    const costAmount = item.costPrice * item.shares
    return {
      code: item.code,
      name: item.name,
      costAmount,
      marketValue: item.marketValue,
      floatingPnl: item.floatingPnl,
      pnlRate: item.pnlRate,
      allocationRate: totalAssets > 0 ? round2(item.marketValue / totalAssets * 100) : 0,
      pnlContributionRate: totalAssets > 0 ? round2(item.floatingPnl / totalAssets * 100) : 0,
      overSinglePositionLimit: item.marketValue > maxSinglePositionAmount,
    }
  })

  return {
    initialCapital: config.initialCapital,
    cashReserve: config.initialCapital * config.cashReserveRate,
    cash,
    totalAssets,
    openCostBasis,
    holdingMarketValue,
    realizedPnl,
    floatingPnl,
    totalPnl,
    totalReturnRate: round2(totalPnl / config.initialCapital * 100),
    positionCount: holdings.length,
    positionCountWarning: holdings.length >= config.maxHoldings
      ? `已达到建议持仓上限 ${config.maxHoldings} 只，新开仓应先卖弱留强。`
      : '',
    positions,
  }
}

export function realizedPnlForDate(trades: TradeRecord[], tradeDate: string) {
  return trades
    .filter((item) => item.sellDate === tradeDate)
    .reduce((sum, item) => sum + item.pnlAmount, 0)
}

export function recommendSignalBuy(
  signal: SignalEvent,
  account: AccountSummary,
  config = DEFAULT_ACCOUNT_CONFIG,
): BuyRecommendation {
  const price = signal.triggerPrice || signal.currentPrice
  if (!price || price <= 0) {
    return { targetAmount: 0, maxShares: 0, estimatedAmount: 0, riskBudget: 0, reason: '缺少有效触发价格，不能计算仓位。' }
  }
  if (account.positionCount >= config.maxHoldings) {
    return { targetAmount: 0, maxShares: 0, estimatedAmount: 0, riskBudget: 0, reason: `已达到建议持仓上限 ${config.maxHoldings} 只，先卖弱留强。` }
  }

  const strong = /高质量|重点|强|趋势/.test(`${signal.sourceType} ${signal.finalAction} ${signal.reason}`)
  const targetRate = strong ? config.strongInitialRate : config.normalInitialRate
  const targetAmount = account.initialCapital * targetRate
  const reserveProtectedCash = Math.max(0, account.cash - account.cashReserve)
  const maxPositionAmount = account.totalAssets * config.maxSinglePositionRate
  const riskBudget = account.initialCapital * config.riskRate
  const riskPerShare = signal.stopLoss > 0 && price > signal.stopLoss ? price - signal.stopLoss : price * 0.06
  const riskAmountCap = riskBudget / riskPerShare * price
  const allowedAmount = Math.max(0, Math.min(targetAmount, reserveProtectedCash, maxPositionAmount, riskAmountCap))
  const maxShares = roundLot(allowedAmount / price)
  const reason = [
    strong ? '高质量首仓 10%' : '首仓 8%',
    '单票最高 15%',
    '单笔风险 1%',
    '保留 25% 现金',
  ].join('；')

  return {
    targetAmount: round2(targetAmount),
    maxShares,
    estimatedAmount: round2(maxShares * price),
    riskBudget: round2(riskBudget),
    reason,
  }
}
