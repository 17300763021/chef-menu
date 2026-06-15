import type { Chef, CookingRecord, DailyMenu, Recipe, RecipeDraft } from '../domain/types'
import { demoChefs, demoRecipes } from './demoData'

export interface HistoryEntry {
  menu: DailyMenu
  record?: CookingRecord
}

export interface SaveMenuInput {
  menuDate: string
  chefId: string
  recipeIds: string[]
  quote: string
  note: string
}

export interface CompleteMenuInput {
  rating: number
  reflection: string
  photoUrls: string[]
}

export interface MenuRepository {
  getChefs(): Promise<Chef[]>
  getRecipes(): Promise<Recipe[]>
  saveRecipe(draft: RecipeDraft): Promise<Recipe>
  getMenu(date: string, chefId: string): Promise<DailyMenu | null>
  saveMenu(input: SaveMenuInput): Promise<DailyMenu>
  completeMenu(menuId: string, input: CompleteMenuInput): Promise<CookingRecord>
  getHistory(): Promise<HistoryEntry[]>
}

const MENUS_KEY = 'chef-menu:menus'
const RECORDS_KEY = 'chef-menu:records'
const RECIPES_KEY = 'chef-menu:recipes'

function load<T>(key: string, fallback: T): T {
  try {
    const value = localStorage.getItem(key)
    return value ? JSON.parse(value) as T : fallback
  } catch {
    return fallback
  }
}

function save<T>(key: string, value: T) {
  localStorage.setItem(key, JSON.stringify(value))
}

export class LocalRepository implements MenuRepository {
  async getChefs() {
    return demoChefs
  }

  async getRecipes() {
    return load<Recipe[]>(RECIPES_KEY, demoRecipes)
  }

  async saveRecipe(draft: RecipeDraft) {
    const recipes = await this.getRecipes()
    const recipe: Recipe = {
      ...draft,
      id: crypto.randomUUID(),
      published: draft.published ?? true,
    }
    save(RECIPES_KEY, [...recipes, recipe])
    return recipe
  }

  async getMenu(date: string, chefId: string) {
    return load<DailyMenu[]>(MENUS_KEY, []).find(
      (menu) => menu.menuDate === date && menu.chefId === chefId,
    ) ?? null
  }

  async saveMenu(input: SaveMenuInput) {
    const menus = load<DailyMenu[]>(MENUS_KEY, [])
    const existing = menus.find(
      (menu) => menu.menuDate === input.menuDate && menu.chefId === input.chefId,
    )
    const menu: DailyMenu = existing
      ? { ...existing, ...input }
      : { ...input, id: crypto.randomUUID(), status: 'planned' }
    save(MENUS_KEY, [...menus.filter((item) => item.id !== menu.id), menu])
    return menu
  }

  async completeMenu(menuId: string, input: CompleteMenuInput) {
    const menus = load<DailyMenu[]>(MENUS_KEY, [])
    save(MENUS_KEY, menus.map((menu) => (
      menu.id === menuId ? { ...menu, status: 'completed' } : menu
    )))
    const records = load<CookingRecord[]>(RECORDS_KEY, [])
    const record: CookingRecord = {
      ...input,
      id: crypto.randomUUID(),
      menuId,
      completedAt: new Date().toISOString(),
    }
    save(RECORDS_KEY, [...records.filter((item) => item.menuId !== menuId), record])
    return record
  }

  async getHistory() {
    const menus = load<DailyMenu[]>(MENUS_KEY, [])
    const records = load<CookingRecord[]>(RECORDS_KEY, [])
    return menus
      .filter((menu) => menu.status === 'completed')
      .map((menu) => ({
        menu,
        record: records.find((record) => record.menuId === menu.id),
      }))
      .sort((a, b) => b.menu.menuDate.localeCompare(a.menu.menuDate))
  }
}

export const localRepository = new LocalRepository()
