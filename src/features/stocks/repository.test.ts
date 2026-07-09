import { describe, expect, it } from 'vitest'
import { createStockRepository } from './repository'
import type { SignalEvent } from './types'

describe('stock repository', () => {
  it('loads only open holdings from the cloud repository', async () => {
    const calls: Array<{ table: string; filters: Array<[string, string]> }> = []
    const rowsByTable: Record<string, unknown[]> = {
      stock_positions: [{
        id: 'holding-open',
        code: '000001',
        name: '平安银行',
        cost_price: 10,
        shares: 100,
        current_price: 11,
        market_value: 1100,
        floating_pnl: 100,
        pnl_rate: 10,
        buy_date: '2026-06-16',
        holding_days: 8,
        current_suggestion: '继续持有',
        status: 'open',
      }],
    }
    const client = {
      from: (table: string) => {
        const filters: Array<[string, string]> = []
        calls.push({ table, filters })
        return {
          select: () => ({
            eq: (column: string, value: string) => {
              filters.push([column, value])
              return {
                order: () => Promise.resolve({ data: rowsByTable[table] ?? [], error: null }),
              }
            },
            order: () => Promise.resolve({ data: rowsByTable[table] ?? [], error: null }),
          }),
        }
      },
    } as unknown as Parameters<typeof createStockRepository>[0]
    const repository = createStockRepository(client)

    await expect(repository.getHoldings()).resolves.toHaveLength(1)
    expect(calls.find((call) => call.table === 'stock_positions')?.filters).toContainEqual(['status', 'open'])
  })

  it('formats timestamp fields for display', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_job_runs: [{
        id: 'job-1',
        job_type: 'GitHub Actions: live_decision',
        started_at: '2026-06-24T05:55:28.529979+00:00',
        finished_at: '2026-06-24T05:56:01.000000+00:00',
        status: 'success',
        imported_count: 2,
        error_message: '',
      }],
      stock_auto_trade_orders: [{
        id: 'order-1',
        order_time: '2026-06-24T05:55:28.529979+00:00',
        order_date: '2026-06-24',
        code: '000001',
        name: '平安银行',
        side: 'sell',
        reason: '止盈',
        price: 11,
        shares: 100,
        amount: 1100,
        fee_amount: 6,
        slippage_amount: 1.1,
        cash_before: 0,
        cash_after: 1100,
        realized_pnl: 100,
        status: 'filled',
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

    await expect(repository.getTasks()).resolves.toMatchObject([{
      startTime: '2026-06-24 13:55:28',
      endTime: '2026-06-24 13:56:01',
    }])
    await expect(repository.getPaperTradeOrders()).resolves.toMatchObject([{
      orderTime: '2026-06-24 13:55:28',
      orderDate: '2026-06-24',
      feeAmount: 6,
      slippageAmount: 1.1,
    }])
  })

  it('maps stock task job types to user-facing names', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_job_runs: [
        {
          id: 'job-full',
          job_type: 'GitHub Actions: full',
          started_at: '2026-06-26T05:18:20Z',
          status: 'success',
          imported_count: 4,
          error_message: '',
        },
        {
          id: 'job-sync',
          job_type: '本地CSV同步',
          started_at: '2026-06-26T05:32:39Z',
          status: 'success',
          imported_count: 122,
          error_message: '',
        },
        {
          id: 'job-web',
          job_type: 'Web request: live_decision',
          started_at: '2026-06-26T05:40:00Z',
          status: 'success',
          imported_count: 2,
          error_message: '',
        },
      ],
    }
    const client = {
      from: (table: string) => ({
        select: () => ({
          order: () => Promise.resolve({ data: rowsByTable[table] ?? [], error: null }),
        }),
      }),
    } as unknown as Parameters<typeof createStockRepository>[0]
    const repository = createStockRepository(client)

    await expect(repository.getTasks()).resolves.toMatchObject([
      { type: '线上全链路任务' },
      { type: '策略结果入库' },
      { type: '页面触发：线上实时决策' },
    ])
  })

  it('maps multi-factor stock scan fields from Supabase rows', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_scan_results: [{
        scan_date: '2026-07-09',
        code: '600001',
        name: 'Alpha',
        score: 80,
        prev_close: 10,
        signal: '多因子测试',
        action: '观察',
        support_level: 9.5,
        resistance_level: 11,
        stop_loss: 9,
        reason: 'fixture',
        risk: '',
        factor_trend: 71,
        factor_momentum: 82,
        factor_volume: 63,
        factor_flow: 77,
        factor_quality: 55,
        sector_rank: 4,
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

    await expect(repository.getRoughStocks()).resolves.toMatchObject([{
      factorTrend: 71,
      factorMomentum: 82,
      factorVolume: 63,
      factorFlow: 77,
      factorQuality: 55,
      sectorRank: 4,
    }])
  })

  it('loads the latest market regime from Supabase rows', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_market_regime: [{
        id: 'regime-1',
        regime_date: '2026-07-09',
        regime: '熊市',
        csi300_close: 4755.534,
        market_turnover_yi: 9819.46,
        limit_up_count: 8,
        limit_down_count: 1,
        break_rate_pct: 50,
        advance_decline_ratio: 3.4444,
        position_cap_pct: 20,
        details: {
          csi300_ma20: 4880.862,
          csi300_ma60: 4846.421,
          regime_note: '指数弱于中期均线，模拟仓位上限20%',
          breadth_source: 'stock_daily_history_cache',
        },
        created_at: '2026-07-09T09:00:00Z',
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

    await expect(repository.getMarketRegime()).resolves.toMatchObject({
      regime: '熊市',
      regimeDate: '2026-07-09',
      positionCapPct: 20,
      csi300Ma20: 4880.862,
      csi300Ma60: 4846.421,
      regimeNote: '指数弱于中期均线，模拟仓位上限20%',
      breadthSource: 'stock_daily_history_cache',
    })
  })

  it('translates automatic trading reasons for display', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_auto_trade_orders: [{
        id: 'order-1',
        order_time: '2026-06-24T05:55:28.529979+00:00',
        order_date: '2026-06-24',
        code: '000001',
        name: '平安银行',
        side: 'sell',
        reason: 'Auto paper sell: stop loss touched',
        price: 9,
        shares: 100,
        amount: 900,
        cash_before: 0,
        cash_after: 900,
        realized_pnl: -100,
        status: 'filled',
      }],
      stock_trade_history: [{
        code: '000001',
        name: '平安银行',
        buy_date: '2026-06-20',
        sell_date: '2026-06-24',
        cost_price: 10,
        sell_price: 9,
        shares: 100,
        pnl_amount: -100,
        pnl_rate: -10,
        buy_memo: 'Auto paper trading engine',
        sell_memo: 'Auto paper sell: stop loss touched',
        is_cleared: true,
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

    await expect(repository.getPaperTradeOrders()).resolves.toMatchObject([{
      reason: '自动模拟卖出：触发止损',
    }])
    await expect(repository.getTradeRecords()).resolves.toMatchObject([{
      buyMemo: '自动模拟交易引擎',
      sellMemo: '自动模拟卖出：触发止损',
    }])
  })

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

  it('maps strategy suggestions with no order as not executed', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_signal_events: [{
        id: 'signal-2r',
        signal_time: '2026-06-25T02:30:00Z',
        signal_date: '2026-06-25',
        code: '000001',
        name: '平安银行',
        source_type: '持仓',
        signal_type: '止盈',
        status: '新信号',
        execution_status: 'not_executed',
        execution_order_id: null,
        execution_reason: '',
        execution_handled_at: null,
        trigger_price: 12,
        current_price: 12,
        change_rate: 3,
        buy_price_text: '不加仓',
        sell_price_text: '2R 达成，建议止盈',
        stop_loss: 10,
        target_price_1: 11,
        final_action: '2R止盈建议',
        reason: '达到第二止盈目标',
        risk: '',
        created_at: '2026-06-25T02:30:00Z',
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
      code: '000001',
      executionStatus: 'not_executed',
      executionStatusText: '策略建议，未执行',
      executionReason: '',
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
      executionStatus: 'not_executed',
      executionStatusText: '策略建议，未执行',
      executionOrderId: '',
      executionReason: '',
      executionHandledAt: '--',
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
      expect.objectContaining({
        table: 'stock_signal_events',
        action: 'update',
        payload: expect.objectContaining({
          status: '已买入',
          execution_status: 'manual_executed',
          execution_reason: '手动记录买入',
        }),
      }),
    ]))
  })

  it('loads backtest runs, trades, and missed runners from the cloud repository', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_backtest_runs: [{
        id: 'run-1',
        run_time: '2026-06-17T08:00:00Z',
        strategy_name: 'strong_pick_v1',
        benchmark_name: 'pick_equal_weight',
        start_date: '2026-05-01',
        end_date: '2026-06-16',
        initial_cash: 1000000,
        final_value: 1080000,
        total_return_rate: 8,
        annual_return_rate: 18,
        benchmark_return_rate: 5,
        benchmark_csi300_return_rate: 4.2,
        benchmark_csi500_return_rate: 6.1,
        excess_return_rate: 3,
        equity_reconciled: true,
        max_drawdown_rate: 3.2,
        sharpe_ratio: 1.4,
        calmar_ratio: 5.6,
        win_rate: 55.5,
        profit_loss_ratio: 1.8,
        turnover_rate: 32,
        consecutive_losses: 3,
        largest_single_loss: -12000,
        sample_split_summary: { in_sample: { trade_count: 10, total_return_rate: 4 } },
        parameter_sensitivity_summary: [{ case_name: 'tp_0.08_sl_0.04_hold_5', baseline_total_return_rate: 8 }],
        trade_count: 18,
        avg_holding_days: 6.4,
        missed_runner_count: 3,
        note: 'ok',
      }],
      stock_backtest_trades: [{
        id: 'trade-1',
        run_id: 'run-1',
        code: '000001',
        name: '平安银行',
        entry_date: '2026-05-06',
        exit_date: '2026-05-14',
        entry_price: 10,
        exit_price: 11,
        shares: 1000,
        pnl_amount: 1000,
        pnl_rate: 10,
        fee_amount: 12,
        slippage_amount: 2,
        holding_days: 8,
        exit_reason: 'take_profit',
      }],
      stock_missed_runners: [{
        id: 'miss-1',
        run_id: 'run-1',
        pick_date: '2026-05-06',
        code: '000002',
        name: '万科A',
        pick_price: 8,
        max_price: 10,
        max_return_rate: 25,
        days_to_high: 5,
        reason: 'not bought',
      }],
      stock_backtest_equity_curve: [{
        id: 'curve-1',
        run_id: 'run-1',
        curve_date: '2026-05-14',
        equity_value: 1001000,
        daily_return_rate: 0.1,
        drawdown_rate: 0,
        benchmark_value: 1000500,
        benchmark_return_rate: 0.05,
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

    await expect(repository.getBacktestRuns()).resolves.toMatchObject([{
      id: 'run-1',
      strategyName: 'strong_pick_v1',
      totalReturnRate: 8,
      annualReturnRate: 18,
      benchmarkReturnRate: 5,
      benchmarkCsi300ReturnRate: 4.2,
      benchmarkCsi500ReturnRate: 6.1,
      excessReturnRate: 3,
      equityReconciled: true,
      sharpeRatio: 1.4,
      calmarRatio: 5.6,
      turnoverRate: 32,
      consecutiveLosses: 3,
      largestSingleLoss: -12000,
      sampleSplitSummary: { in_sample: { trade_count: 10, total_return_rate: 4 } },
      parameterSensitivitySummary: [{ case_name: 'tp_0.08_sl_0.04_hold_5', baseline_total_return_rate: 8 }],
      tradeCount: 18,
    }])
    await expect(repository.getBacktestTrades()).resolves.toMatchObject([{
      id: 'trade-1',
      code: '000001',
      pnlRate: 10,
      feeAmount: 12,
      slippageAmount: 2,
      holdingDays: 8,
    }])
    await expect(repository.getMissedRunners()).resolves.toMatchObject([{
      id: 'miss-1',
      code: '000002',
      maxReturnRate: 25,
      daysToHigh: 5,
    }])
    await expect(repository.getBacktestEquityCurve()).resolves.toMatchObject([{
      id: 'curve-1',
      runId: 'run-1',
      equityValue: 1001000,
      benchmarkValue: 1000500,
      drawdownRate: 0,
    }])
  })

  it('loads model predictions, decisions, positions, orders, and snapshots', async () => {
    const rowsByTable: Record<string, unknown[]> = {
      stock_model_predictions: [{
        id: 'pred-1',
        prediction_date: '2026-07-06',
        code: '000001',
        name: '骞冲畨閾惰',
        model_name: 'qlib_lgbm_baseline',
        model_version: 'v1',
        feature_set: 'alpha158_lite',
        score: 72.5,
        rank: 1,
        predicted_return: 2.8,
        confidence: 0.68,
        close_price: 10.8,
        feature_window_start: '2026-04-01',
        feature_window_end: '2026-07-06',
      }],
      stock_model_decisions: [{
        id: 'decision-1',
        decision_time: '2026-07-06T07:00:00Z',
        decision_date: '2026-07-06',
        strategy_account: 'model_qlib_lgbm_v1',
        code: '000001',
        name: '骞冲畨閾惰',
        model_name: 'qlib_lgbm_baseline',
        model_version: 'v1',
        action: 'buy',
        reason: 'top-ranked positive model prediction',
        risk_gate_status: 'passed',
        risk_gate_reason: '',
        target_weight: 0.08,
        planned_shares: 1000,
        status: 'handled',
      }],
      stock_model_positions: [{
        id: 'model-pos-1',
        strategy_account: 'model_qlib_lgbm_v1',
        code: '000001',
        name: '骞冲畨閾惰',
        cost_price: 10.8,
        shares: 1000,
        current_price: 11,
        market_value: 11000,
        floating_pnl: 200,
        pnl_rate: 1.85,
        buy_date: '2026-07-06',
        current_suggestion: 'model virtual buy',
        status: 'open',
        model_name: 'qlib_lgbm_baseline',
        model_version: 'v1',
      }],
      stock_model_orders: [{
        id: 'model-order-1',
        order_time: '2026-07-06T07:01:00Z',
        order_date: '2026-07-06',
        strategy_account: 'model_qlib_lgbm_v1',
        code: '000001',
        name: '骞冲畨閾惰',
        side: 'buy',
        reason: 'top-ranked positive model prediction',
        price: 10.81,
        shares: 1000,
        amount: 10810,
        fee_amount: 5,
        slippage_amount: 10,
        cash_before: 1000000,
        cash_after: 989185,
        realized_pnl: 0,
        status: 'filled',
        failure_reason: '',
        model_name: 'qlib_lgbm_baseline',
        model_version: 'v1',
      }],
      stock_model_portfolio_snapshots: [{
        id: 'model-snap-1',
        snapshot_time: '2026-07-06T07:02:00Z',
        snapshot_date: '2026-07-06',
        strategy_account: 'model_qlib_lgbm_v1',
        cash: 989185,
        holding_market_value: 10810,
        total_assets: 999995,
        realized_pnl: 0,
        floating_pnl: -5,
        total_pnl: -5,
        total_return_rate: -0.0005,
        max_drawdown_rate: 0.0005,
        consecutive_losses: 0,
        position_count: 1,
        trade_count: 1,
        model_name: 'qlib_lgbm_baseline',
        model_version: 'v1',
        note: 'simulation only',
      }],
    }
    const client = {
      from: (table: string) => ({
        select: () => ({
          eq: () => ({
            order: () => Promise.resolve({ data: rowsByTable[table] ?? [], error: null }),
          }),
          order: () => Promise.resolve({ data: rowsByTable[table] ?? [], error: null }),
        }),
      }),
    } as unknown as Parameters<typeof createStockRepository>[0]
    const repository = createStockRepository(client)

    await expect(repository.getModelPredictions()).resolves.toMatchObject([{
      id: 'pred-1',
      modelName: 'qlib_lgbm_baseline',
      rank: 1,
      confidence: 0.68,
    }])
    await expect(repository.getModelDecisions()).resolves.toMatchObject([{
      id: 'decision-1',
      strategyAccount: 'model_qlib_lgbm_v1',
      action: 'buy',
      riskGateStatus: 'passed',
    }])
    await expect(repository.getModelPositions()).resolves.toMatchObject([{
      id: 'model-pos-1',
      strategyAccount: 'model_qlib_lgbm_v1',
      shares: 1000,
    }])
    await expect(repository.getModelOrders()).resolves.toMatchObject([{
      id: 'model-order-1',
      strategyAccount: 'model_qlib_lgbm_v1',
      modelVersion: 'v1',
    }])
    await expect(repository.getModelPortfolioSnapshots()).resolves.toMatchObject([{
      id: 'model-snap-1',
      maxDrawdownRate: 0.0005,
      consecutiveLosses: 0,
    }])
  })
})
