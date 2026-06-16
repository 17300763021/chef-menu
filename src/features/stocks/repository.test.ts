import { describe, expect, it } from 'vitest'
import { createStockRepository } from './repository'
import type { SignalEvent } from './types'

describe('stock repository', () => {
  it('does not show mock stock prices when a cloud repository is configured but empty', async () => {
    const emptyClient = {
      from: () => ({
        select: () => ({
          order: () => Promise.resolve({ data: [], error: null }),
        }),
      }),
    } as unknown as Parameters<typeof createStockRepository>[0]
    const repository = createStockRepository(emptyClient)

    await expect(repository.getRealtimeDecisions()).resolves.toEqual([])
    await expect(repository.getRoughStocks()).resolves.toEqual([])
    await expect(repository.getFineStocks()).resolves.toEqual([])
    await expect(repository.getOverview()).resolves.toMatchObject({
      roughCount: 0,
      fineCount: 0,
      buyableCount: 0,
    })
  })

  it('falls back to local holdings and stores manually added holdings', async () => {
    const repository = createStockRepository(null)

    const before = await repository.getHoldings()
    await repository.addHolding({
      code: '000001',
      name: '平安银行',
      costPrice: 10.5,
      shares: 100,
      currentPrice: 10.8,
      buyDate: '2026-06-16',
      currentSuggestion: '等待回踩，不追高',
    })

    const after = await repository.getHoldings()
    expect(after).toHaveLength(before.length + 1)
    expect(after[0]).toMatchObject({
      code: '000001',
      name: '平安银行',
      currentSuggestion: '等待回踩，不追高',
    })
    expect(after[0].marketValue).toBe(1080)
  })

  it('loads signal events and historical strong picks from the cloud repository', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_signal_events: [{
        id: 'signal-1',
        signal_time: '2026-06-16T10:15:00Z',
        signal_date: '2026-06-16',
        code: '000001',
        name: '平安银行',
        source_type: '精选',
        signal_type: '买入',
        status: '新信号',
        trigger_price: 10.8,
        current_price: 10.8,
        change_rate: 2.1,
        buy_price_text: '10.70 ~ 10.90',
        sell_price_text: '跌破 10.20 止损',
        stop_loss: 10.2,
        target_price_1: 11.4,
        final_action: '可以买小仓',
        reason: '分时站上均价线',
        risk: '',
        created_at: '2026-06-16T10:15:00Z',
      }],
      stock_strong_picks: [{
        id: 'pick-1',
        scan_date: '2026-06-15',
        code: '000001',
        name: '平安银行',
        strategy_level: '重点池',
        review_status: '通过',
        score: 88,
        prev_close: 10.5,
        signal: '回踩20日线低吸买点',
        action: '可进入第二天确认',
        support_level: 10.1,
        resistance_level: 11.2,
        stop_loss: 9.8,
        reason: '股价在向上的20日线上方',
        risk: '',
      }],
    }
    const client = {
      from: (table: string) => ({
        select: () => ({
          order: () => Promise.resolve({ data: rowsByTable[table] ?? [], error: null }),
        }),
      }),
    } as unknown as Parameters<typeof createStockRepository>[0]
    const repository = createStockRepository(client)

    await expect(repository.getSignalEvents()).resolves.toMatchObject([{
      id: 'signal-1',
      code: '000001',
      signalType: '买入',
      buyPriceText: '10.70 ~ 10.90',
      stopLoss: 10.2,
    }])
    await expect(repository.getHistoricalFineStocks()).resolves.toMatchObject([{
      code: '000001',
      date: '2026-06-15',
      strategyLevel: '重点池',
      score: 88,
    }])
  })

  it('confirms a buy signal by creating a holding and marking the signal handled', async () => {
    const calls: Array<{ table: string; action: string; payload?: unknown }> = []
    const signalRow = {
      id: 'signal-1',
      signal_time: '2026-06-16T10:15:00Z',
      signal_date: '2026-06-16',
      code: '000001',
      name: '平安银行',
      source_type: '精选',
      signal_type: '买入',
      status: '新信号',
      trigger_price: 10.8,
      current_price: 10.8,
      change_rate: 2.1,
      buy_price_text: '10.70 ~ 10.90',
      sell_price_text: '跌破 10.20 止损',
      stop_loss: 10.2,
      target_price_1: 11.4,
      final_action: '可以买小仓',
      reason: '分时站上均价线',
      risk: '',
      created_at: '2026-06-16T10:15:00Z',
    }
    const client = {
      from: (table: string) => ({
        insert: (payload: unknown) => {
          calls.push({ table, action: 'insert', payload })
          return {
            select: () => ({
              single: () => Promise.resolve({ data: { id: 'holding-1', ...(payload as object) }, error: null }),
            }),
          }
        },
        update: (payload: unknown) => {
          calls.push({ table, action: 'update', payload })
          return {
            eq: () => ({
              select: () => ({
                single: () => Promise.resolve({ data: { ...signalRow, ...(payload as object) }, error: null }),
              }),
            }),
          }
        },
      }),
    } as unknown as Parameters<typeof createStockRepository>[0]
    const repository = createStockRepository(client)

    const signal: SignalEvent = {
      id: 'signal-1',
      signalTime: '2026-06-16T10:15:00Z',
      signalDate: '2026-06-16',
      code: '000001',
      name: '平安银行',
      sourceType: '精选',
      signalType: '买入',
      status: '新信号',
      triggerPrice: 10.8,
      currentPrice: 10.8,
      changeRate: 2.1,
      buyPriceText: '10.70 ~ 10.90',
      sellPriceText: '跌破 10.20 止损',
      stopLoss: 10.2,
      targetPrice1: 11.4,
      finalAction: '可以买小仓',
      reason: '分时站上均价线',
      risk: '',
      createdAt: '2026-06-16T10:15:00Z',
    }

    await repository.confirmSignalBuy({
      signal,
      shares: 100,
      price: 10.8,
      buyDate: '2026-06-16',
      memo: '按信号小仓试买',
    })

    expect(calls).toEqual(expect.arrayContaining([
      expect.objectContaining({ table: 'stock_positions', action: 'insert' }),
      expect.objectContaining({ table: 'stock_signal_events', action: 'update' }),
    ]))
  })
})
