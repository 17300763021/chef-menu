import type { Chef, CookingRecord, DailyMenu, Recipe, RecipeDraft } from '../domain/types'
import { supabase } from '../lib/supabase'
import { demoChefs } from './demoData'
import {
  localRepository,
  type CompleteMenuInput,
  type HistoryEntry,
  type MenuRepository,
  type SaveMenuInput,
} from './repository'

async function withTimeout<T>(request: PromiseLike<T>, milliseconds = 3500): Promise<T> {
  let timer = 0
  try {
    return await Promise.race([
      Promise.resolve(request),
      new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => reject(new Error('Supabase request timed out')), milliseconds)
      }),
    ])
  } finally {
    window.clearTimeout(timer)
  }
}

function mapChef(row: Record<string, unknown>): Chef {
  return {
    id: String(row.id),
    name: String(row.name),
    slug: String(row.slug),
    avatarUrl: String(row.avatar_url ?? ''),
    theme: row.theme === 'pink' ? 'pink' : 'yellow',
    bio: String(row.bio ?? ''),
    specialties: (row.specialties as string[]) ?? [],
  }
}

function mapRecipe(row: Record<string, unknown>): Recipe {
  return {
    id: String(row.id),
    chefId: String(row.chef_id ?? ''),
    name: String(row.name),
    aliases: (row.aliases as string[]) ?? [],
    category: row.category as Recipe['category'],
    coverUrl: String(row.cover_url ?? '🍲'),
    ingredients: (row.ingredients as Recipe['ingredients']) ?? [],
    steps: (row.steps as string[]) ?? [],
    keywords: (row.keywords as string[]) ?? [],
    spicyLevel: Number(row.spicy_level ?? 0),
    difficulty: Number(row.difficulty ?? 1),
    minutes: Number(row.minutes ?? 20),
    tutorialPlatform: String(row.tutorial_platform ?? ''),
    tutorialAuthor: String(row.tutorial_author ?? ''),
    tutorialUrl: String(row.tutorial_url ?? ''),
    tutorialNote: String(row.tutorial_note ?? ''),
    published: Boolean(row.is_published),
  }
}

export class SupabaseRepository implements MenuRepository {
  private remoteReady = false

  async getChefs() {
    if (!supabase) return demoChefs
    try {
      const { data, error } = await withTimeout(supabase.from('chefs').select('*').order('name'))
      if (error || !data?.length) return demoChefs
      return data.map(mapChef)
    } catch {
      return demoChefs
    }
  }

  async getRecipes() {
    if (!supabase) return localRepository.getRecipes()
    try {
      const { data, error } = await withTimeout(
        supabase.from('recipes').select('*').eq('is_published', true).order('name'),
      )
      if (error || !data?.length) {
        this.remoteReady = false
        return localRepository.getRecipes()
      }
      this.remoteReady = true
      return data.map(mapRecipe)
    } catch {
      this.remoteReady = false
      return localRepository.getRecipes()
    }
  }

  async saveRecipe(draft: RecipeDraft) {
    if (!supabase) return localRepository.saveRecipe(draft)
    const { data, error } = await supabase.from('recipes').insert({
      chef_id: draft.chefId,
      name: draft.name,
      aliases: draft.aliases,
      category: draft.category,
      cover_url: draft.coverUrl,
      ingredients: draft.ingredients,
      steps: draft.steps,
      keywords: draft.keywords,
      spicy_level: draft.spicyLevel,
      difficulty: draft.difficulty,
      minutes: draft.minutes,
      tutorial_platform: draft.tutorialPlatform,
      tutorial_author: draft.tutorialAuthor,
      tutorial_url: draft.tutorialUrl,
      tutorial_note: draft.tutorialNote,
      is_published: draft.published ?? true,
    }).select().single()
    if (error) {
      throw new Error(`菜谱保存失败：${error.message}。请检查 recipes 表的管理员 RLS Policy。`)
    }
    this.remoteReady = true
    return mapRecipe(data)
  }

  async getMenu(date: string, chefId: string) {
    if (!supabase || !this.remoteReady) return localRepository.getMenu(date, chefId)
    const { data, error } = await supabase
      .from('daily_menus')
      .select('*, daily_menu_items(recipe_id)')
      .eq('menu_date', date)
      .eq('chef_id', chefId)
      .maybeSingle()
    if (error || !data) return null
    return {
      id: data.id,
      menuDate: data.menu_date,
      chefId: data.chef_id,
      recipeIds: (data.daily_menu_items ?? []).map((item: { recipe_id: string }) => item.recipe_id),
      status: data.status,
      quote: data.quote ?? '',
      note: data.note ?? '',
    } satisfies DailyMenu
  }

  async saveMenu(input: SaveMenuInput) {
    if (!supabase || !this.remoteReady) return localRepository.saveMenu(input)
    const { data: menu, error } = await supabase.from('daily_menus').upsert({
      menu_date: input.menuDate,
      chef_id: input.chefId,
      quote: input.quote,
      note: input.note,
    }, { onConflict: 'menu_date,chef_id' }).select().single()
    if (error) throw error
    await supabase.from('daily_menu_items').delete().eq('menu_id', menu.id)
    if (input.recipeIds.length) {
      const { error: itemError } = await supabase.from('daily_menu_items').insert(
        input.recipeIds.map((recipeId, index) => ({ menu_id: menu.id, recipe_id: recipeId, sort_order: index })),
      )
      if (itemError) throw itemError
    }
    return { ...input, id: menu.id, status: menu.status ?? 'planned' }
  }

  async completeMenu(menuId: string, input: CompleteMenuInput) {
    if (!supabase || !this.remoteReady) return localRepository.completeMenu(menuId, input)
    const { error: menuError } = await supabase.from('daily_menus').update({ status: 'completed' }).eq('id', menuId)
    if (menuError) throw menuError
    const { data, error } = await supabase.from('cooking_records').upsert({
      menu_id: menuId,
      rating: input.rating,
      reflection: input.reflection,
    }, { onConflict: 'menu_id' }).select().single()
    if (error) throw error
    if (input.photoUrls.length) {
      await supabase.from('record_photos').delete().eq('record_id', data.id)
      const { error: photoError } = await supabase.from('record_photos').insert(
        input.photoUrls.map((imageUrl, index) => ({ record_id: data.id, image_url: imageUrl, sort_order: index })),
      )
      if (photoError) throw photoError
    }
    return {
      id: data.id,
      menuId,
      rating: input.rating,
      reflection: input.reflection,
      photoUrls: input.photoUrls,
      completedAt: data.completed_at,
    } satisfies CookingRecord
  }

  async getHistory(): Promise<HistoryEntry[]> {
    if (!supabase || !this.remoteReady) return localRepository.getHistory()
    const { data, error } = await withTimeout(
      supabase
        .from('daily_menus')
        .select('*, daily_menu_items(recipe_id), cooking_records(*, record_photos(image_url, sort_order))')
        .eq('status', 'completed')
        .order('menu_date', { ascending: false }),
    )
    if (error || !data) return []
    return data.map((row) => {
      const record = row.cooking_records?.[0]
      return {
        menu: {
          id: row.id,
          menuDate: row.menu_date,
          chefId: row.chef_id,
          recipeIds: row.daily_menu_items.map((item: { recipe_id: string }) => item.recipe_id),
          status: row.status,
          quote: row.quote ?? '',
          note: row.note ?? '',
        },
        record: record ? {
          id: record.id,
          menuId: row.id,
          rating: record.rating,
          reflection: record.reflection ?? '',
          photoUrls: (record.record_photos ?? []).map((photo: { image_url: string }) => photo.image_url),
          completedAt: record.completed_at,
        } : undefined,
      }
    })
  }
}

export const appRepository = new SupabaseRepository()
