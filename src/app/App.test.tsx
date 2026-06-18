import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App from '../App'
import { AppProvider } from './AppContext'
import { LocalRepository, type MenuRepository } from '../data/repository'
import { demoChefs, demoRecipes } from '../data/demoData'

describe('chef menu application', () => {
  it('switches chefs and shows two daily recommendations', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)
    expect(await screen.findByText('今天推荐这两道')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /加入今日菜单/ })).toHaveLength(2)
    await userEvent.click(screen.getAllByRole('button', { name: /金大厨/ })[0])
    expect(screen.getByText(/金大厨今日掌勺/)).toBeInTheDocument()
  })

  it('searches recipes and blocks visitor ordering', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)
    await userEvent.click((await screen.findAllByRole('link', { name: '自己点菜' }))[0])
    await userEvent.type(screen.getByRole('searchbox'), '五花肉')
    expect(await screen.findByText('辣椒炒肉')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /加入今日菜单/ }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('看菜可以，点菜不行')
  })

  it('loads cloud chefs with UUID ids without creating an empty menu', async () => {
    const cloudChefs = demoChefs.map((chef, index) => ({
      ...chef,
      id: `00000000-0000-0000-0000-00000000000${index + 1}`,
    }))
    const cloudRecipes = demoRecipes.map((recipe) => ({
      ...recipe,
      chefId: recipe.chefId === 'chen' ? cloudChefs[0].id : cloudChefs[1].id,
    }))
    const saveMenu = vi.fn()
    const repository: MenuRepository = {
      getChefs: async () => cloudChefs,
      getRecipes: async () => cloudRecipes,
      getHistory: async () => [],
      getMenu: async () => null,
      saveMenu,
      saveRecipe: async () => cloudRecipes[0],
      updateRecipe: async () => cloudRecipes[0],
      deleteRecipe: async () => undefined,
      completeMenu: async () => {
        throw new Error('not used')
      },
    }

    localStorage.setItem('chef-menu:selected-chef', 'chen')
    render(<AppProvider repository={repository}><App /></AppProvider>)

    expect(await screen.findByText('今天推荐这两道')).toBeInTheDocument()
    expect(saveMenu).not.toHaveBeenCalled()
  })

  it('opens the stock strategy assistant from the header action button', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)

    await userEvent.click(await screen.findByRole('link', { name: '股票助手' }))

    expect(await screen.findByRole('heading', { name: '股票策略助手' }, { timeout: 7000 })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '自动模拟盘' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: '任务中心' }))
    expect(screen.getByRole('button', { name: '停止每分钟刷新' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '更多数据' }))
    expect(screen.getByRole('tab', { name: '最近盘后精选' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '今日精选' })).not.toBeInTheDocument()
  }, 10000)

  it('asks visitors to log in before adding a stock holding', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)

    await userEvent.click(await screen.findByRole('link', { name: '股票助手' }))
    await userEvent.click(await screen.findByRole('tab', { name: '持仓执行' }))
    await userEvent.click(screen.getByRole('button', { name: '新增持仓' }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('asks visitors to log in before requesting a cloud stock task', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)

    await userEvent.click(await screen.findByRole('link', { name: '股票助手' }))
    await userEvent.click(await screen.findByRole('tab', { name: '任务中心' }, { timeout: 7000 }))
    await userEvent.click(screen.getByRole('button', { name: '运行实时决策' }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })
})
