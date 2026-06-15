export const RECIPE_CATEGORIES = [
  '猪肉',
  '牛羊肉',
  '鸡鸭',
  '鱼虾海鲜',
  '蛋类',
  '豆制品',
  '蔬菜菌菇',
  '主食',
] as const

export type RecipeCategory = (typeof RECIPE_CATEGORIES)[number]
export type ChefTheme = 'yellow' | 'pink'

export interface Chef {
  id: string
  name: string
  slug: string
  avatarUrl: string
  theme: ChefTheme
  bio: string
  specialties: string[]
}

export interface Ingredient {
  name: string
  amount: string
}

export interface Recipe {
  id: string
  chefId: string
  name: string
  aliases: string[]
  category: RecipeCategory
  coverUrl: string
  ingredients: Ingredient[]
  steps: string[]
  keywords: string[]
  spicyLevel: number
  difficulty: number
  minutes: number
  tutorialPlatform: string
  tutorialAuthor: string
  tutorialUrl: string
  tutorialNote: string
  published: boolean
}

export interface DailyMenu {
  id: string
  menuDate: string
  chefId: string
  recipeIds: string[]
  status: 'planned' | 'completed'
  quote: string
  note: string
}

export interface CookingRecord {
  id: string
  menuId: string
  rating: number
  reflection: string
  photoUrls: string[]
  completedAt: string
}

export interface RecipeDraft extends Omit<Recipe, 'id' | 'published'> {
  published?: boolean
}
