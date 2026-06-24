import { describe, expect, it } from 'vitest'
import {
  DEFAULT_ACCOUNT_CONFIG,
  buildAccountSummary,
  realizedPnlForDate,
  recommendSignalBuy,
} from './account'
import type { HoldingStock, SignalEvent, TradeRecord } from './types'

function holding(overrides: Partial<HoldingStock>): HoldingStock {
  return {
    code: '000001',
    name: '平安银行',
    costPrice: 10,
    shares: 1000,
    currentPrice: 11,
    marketValue: 11000,
    floatingPnl: 1000,
    pnlRate: 10,
    buyDate: '2026-06-16',
    holdingDays: 1,
    currentSuggestion: '持有观察',
    ...overrides,
  }
}

function trade(overrides: Partial<TradeRecord>): TradeRecord {
  return {
    code: '000002',
    name: '万科A',
    buyDate: '2026-06-10',
    sellDate: '2026-06-16',
    costPrice: 10,
    sellPrice: 11,
    shares: 1000,
    pnlAmount: 1000,
    pnlRate: 10,
    buyMemo: '',
    sellMemo: '止盈',
    isCleared: true,
    ...overrides,
  }
}

function signal(overrides: Partial<SignalEvent>): SignalEvent {
  return {
    id: 's1',
    signalTime: '2026-06-16T10:00:00Z',
    signalDate: '2026-06-16',
    code: '000003',
    name: '测试股',
    sourceType: '精选',
    signalType: '买入',
    status: '新信号',
    triggerPrice: 10,
    currentPrice: 10,
    changeRate: 1.2,
    buyPriceText: '10.00',
    sellPriceText: '跌破 9.5 止损',
    stopLoss: 9.5,
    targetPrice1: 11,
    finalAction: '可以买小仓',
    reason: '回踩确认',
    risk: '',
    createdAt: '2026-06-16T10:00:00Z',
    ...overrides,
  }
}

describe('stock account model', () => {
  it('summarizes a one million account with cash, holdings, realized pnl and total pnl', () => {
    const summary = buildAccountSummary([
      holding({ code: '000001', name: '平安银行', costPrice: 10, shares: 10000, currentPrice: 11, marketValue: 110000, floatingPnl: 10000 }),
      holding({ code: '000002', name: '万科A', costPrice: 8, shares: 5000, currentPrice: 7, marketValue: 35000, floatingPnl: -5000 }),
    ], [
      trade({ pnlAmount: 12000 }),
      trade({ pnlAmount: -2000 }),
    ])

    expect(summary.initialCapital).toBe(1_000_000)
    expect(summary.openCostBasis).toBe(140000)
    expect(summary.holdingMarketValue).toBe(145000)
    expect(summary.realizedPnl).toBe(10000)
    expect(summary.floatingPnl).toBe(5000)
    expect(summary.totalPnl).toBe(15000)
    expect(summary.cash).toBe(870000)
    expect(summary.totalAssets).toBe(1015000)
    expect(summary.totalReturnRate).toBe(1.5)
    expect(summary.positions[0]).toMatchObject({
      code: '000001',
      allocationRate: 10.84,
      pnlContributionRate: 0.99,
      overSinglePositionLimit: false,
    })
  })

  it('calculates realized pnl for a single trade date', () => {
    const todayPnl = realizedPnlForDate([
      trade({ sellDate: '2026-06-24', pnlAmount: -20000 }),
      trade({ sellDate: '2026-06-24', pnlAmount: 5000 }),
      trade({ sellDate: '2026-06-23', pnlAmount: 12000 }),
    ], '2026-06-24')

    expect(todayPnl).toBe(-15000)
  })

  it('recommends conservative buy sizing from signal quality, cash, position cap and stop risk', () => {
    const summary = buildAccountSummary([], [])
    const normal = recommendSignalBuy(signal({ triggerPrice: 10, stopLoss: 9.5, finalAction: '可以买小仓' }), summary)
    const strong = recommendSignalBuy(signal({ triggerPrice: 20, stopLoss: 19.6, finalAction: '高质量趋势，可以小仓' }), summary)

    expect(normal.targetAmount).toBe(80000)
    expect(normal.maxShares).toBe(8000)
    expect(normal.reason).toContain('首仓 8%')
    expect(strong.targetAmount).toBe(100000)
    expect(strong.maxShares).toBe(5000)
    expect(strong.reason).toContain('高质量首仓 10%')
  })

  it('reserves cash and blocks new buys when the account already has six holdings', () => {
    const holdings = Array.from({ length: DEFAULT_ACCOUNT_CONFIG.maxHoldings }, (_, index) => holding({
      code: `${index}`.padStart(6, '0'),
      marketValue: 100000,
      costPrice: 10,
      shares: 10000,
      currentPrice: 10,
      floatingPnl: 0,
    }))
    const summary = buildAccountSummary(holdings, [])
    const recommendation = recommendSignalBuy(signal({ triggerPrice: 10, stopLoss: 9.5 }), summary)

    expect(summary.positionCountWarning).toBe('已达到建议持仓上限 6 只，新开仓应先卖弱留强。')
    expect(recommendation.maxShares).toBe(0)
    expect(recommendation.reason).toContain('已达到建议持仓上限')
  })
})
