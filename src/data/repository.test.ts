import { beforeEach, describe, expect, it } from 'vitest'
import { demoChefs, demoRecipes } from './demoData'
import { LocalRepository } from './repository'

describe('LocalRepository', () => {
  beforeEach(() => localStorage.clear())

  it('provides seeded chefs and recipes', async () => {
    const repository = new LocalRepository()
    expect(await repository.getChefs()).toEqual(demoChefs)
    expect((await repository.getRecipes()).length).toBe(demoRecipes.length)
  })

  it('persists a daily menu and its items', async () => {
    const repository = new LocalRepository()
    const menu = await repository.saveMenu({
      menuDate: '2026-06-15',
      chefId: 'chen',
      recipeIds: ['recipe-1', 'recipe-2'],
      quote: '吃饭',
      note: '',
    })
    const loaded = await repository.getMenu('2026-06-15', 'chen')
    expect(loaded).toEqual(menu)
  })

  it('stores a completed cooking record in history', async () => {
    const repository = new LocalRepository()
    const menu = await repository.saveMenu({
      menuDate: '2026-06-15',
      chefId: 'jin',
      recipeIds: ['recipe-1'],
      quote: '吃饭',
      note: '',
    })
    await repository.completeMenu(menu.id, {
      rating: 5,
      reflection: '很好吃',
      photoUrls: ['data:image/webp;base64,test'],
    })
    const history = await repository.getHistory()
    expect(history[0].menu.status).toBe('completed')
    expect(history[0].record?.reflection).toBe('很好吃')
  })
})
