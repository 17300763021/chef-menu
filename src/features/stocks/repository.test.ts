import { describe, expect, it } from 'vitest'
import { createStockRepository } from './repository'

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
})
