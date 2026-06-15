/* eslint-disable react-refresh/only-export-components */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import { dailyQuote, recommendRecipes, shanghaiDateKey } from '../domain/menu'
import type { Chef, DailyMenu, Recipe } from '../domain/types'
import {
  type CompleteMenuInput,
  type HistoryEntry,
  type MenuRepository,
} from '../data/repository'
import { appRepository } from '../data/supabaseRepository'

interface AppState {
  chefs: Chef[]
  recipes: Recipe[]
  selectedChefId: string
  selectedChef?: Chef
  todayMenu: DailyMenu | null
  recommendationIds: string[]
  history: HistoryEntry[]
  loading: boolean
  setSelectedChefId: (chefId: string) => void
  addToMenu: (recipeId: string) => Promise<void>
  removeFromMenu: (recipeId: string) => Promise<void>
  replaceRecommendations: () => Promise<void>
  completeTodayMenu: (input: CompleteMenuInput) => Promise<void>
  refresh: () => Promise<void>
}

const AppContext = createContext<AppState | null>(null)

export function AppProvider({
  children,
  repository = appRepository,
}: {
  children: ReactNode
  repository?: MenuRepository
}) {
  const [chefs, setChefs] = useState<Chef[]>([])
  const [recipes, setRecipes] = useState<Recipe[]>([])
  const [selectedChefId, setSelectedChefId] = useState(
    () => localStorage.getItem('chef-menu:selected-chef') ?? 'chen',
  )
  const [todayMenu, setTodayMenu] = useState<DailyMenu | null>(null)
  const [recommendationIds, setRecommendationIds] = useState<string[]>([])
  const [history, setHistory] = useState<HistoryEntry[]>([])
  const [loading, setLoading] = useState(true)
  const today = shanghaiDateKey()

  const loadMenu = useCallback(async (chefId: string) => {
    const existing = await repository.getMenu(today, chefId)
    if (existing) {
      setTodayMenu(existing)
      return
    }
    const created = await repository.saveMenu({
      menuDate: today,
      chefId,
      recipeIds: [],
      quote: dailyQuote(),
      note: '',
    })
    setTodayMenu(created)
  }, [repository, today])

  const loadRecommendations = useCallback((allRecipes: Recipe[], chefId: string) => {
    const key = `chef-menu:recommendations:${today}:${chefId}`
    const saved = localStorage.getItem(key)
    if (saved) {
      setRecommendationIds(JSON.parse(saved) as string[])
      return
    }
    const ids = recommendRecipes(allRecipes, chefId, new Date()).map((recipe) => recipe.id)
    localStorage.setItem(key, JSON.stringify(ids))
    setRecommendationIds(ids)
  }, [today])

  const refresh = useCallback(async () => {
    setLoading(true)
    const [nextChefs, nextRecipes, nextHistory] = await Promise.all([
      repository.getChefs(),
      repository.getRecipes(),
      repository.getHistory(),
    ])
    setChefs(nextChefs)
    setRecipes(nextRecipes)
    setHistory(nextHistory)
    await loadMenu(selectedChefId)
    loadRecommendations(nextRecipes, selectedChefId)
    setLoading(false)
  }, [loadMenu, loadRecommendations, repository, selectedChefId])

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(timer)
  }, [refresh])

  useEffect(() => {
    localStorage.setItem('chef-menu:selected-chef', selectedChefId)
  }, [selectedChefId])

  const updateItems = async (recipeIds: string[]) => {
    const menu = await repository.saveMenu({
      menuDate: today,
      chefId: selectedChefId,
      recipeIds,
      quote: todayMenu?.quote ?? dailyQuote(),
      note: todayMenu?.note ?? '',
    })
    setTodayMenu(menu)
  }

  const addToMenu = async (recipeId: string) => {
    const ids = todayMenu?.recipeIds ?? []
    if (!ids.includes(recipeId)) await updateItems([...ids, recipeId])
  }

  const removeFromMenu = async (recipeId: string) => {
    await updateItems((todayMenu?.recipeIds ?? []).filter((id) => id !== recipeId))
  }

  const replaceRecommendations = async () => {
    const recommended = recommendRecipes(recipes, selectedChefId, new Date(), recommendationIds)
    const fallback = recommended.length === 2
      ? recommended
      : recommendRecipes(recipes, selectedChefId, new Date())
    const ids = fallback.map((recipe) => recipe.id)
    localStorage.setItem(`chef-menu:recommendations:${today}:${selectedChefId}`, JSON.stringify(ids))
    setRecommendationIds(ids)
  }

  const completeTodayMenu = async (input: CompleteMenuInput) => {
    if (!todayMenu) return
    await repository.completeMenu(todayMenu.id, input)
    await refresh()
  }

  const value: AppState = {
    chefs,
    recipes,
    selectedChefId,
    selectedChef: chefs.find((chef) => chef.id === selectedChefId),
    todayMenu,
    recommendationIds,
    history,
    loading,
    setSelectedChefId,
    addToMenu,
    removeFromMenu,
    replaceRecommendations,
    completeTodayMenu,
    refresh,
  }

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>
}

export function useApp() {
  const context = useContext(AppContext)
  if (!context) throw new Error('useApp must be used within AppProvider')
  return context
}
