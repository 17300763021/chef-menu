import { describe, expect, it } from 'vitest'
import type { Recipe } from './types'
import {
  dailyQuote,
  formatShanghaiDate,
  recommendRecipes,
  searchRecipes,
} from './menu'

const recipes: Recipe[] = [
  {
    id: 'pork',
    chefId: 'chen',
    name: '辣椒炒肉',
    aliases: ['农家小炒肉'],
    category: '猪肉',
    coverUrl: '',
    ingredients: [{ name: '五花肉', amount: '250克' }, { name: '青椒', amount: '4个' }],
    steps: ['煸香五花肉', '加入青椒翻炒'],
    keywords: ['下饭', '家常'],
    spicyLevel: 2,
    difficulty: 1,
    minutes: 20,
    tutorialPlatform: '小红书',
    tutorialAuthor: '陈大厨',
    tutorialUrl: 'https://example.com/pork',
    tutorialNote: '固定教程',
    published: true,
  },
  {
    id: 'greens',
    chefId: 'jin',
    name: '蒜蓉上海青',
    aliases: [],
    category: '蔬菜菌菇',
    coverUrl: '',
    ingredients: [{ name: '上海青', amount: '300克' }],
    steps: ['大火快炒'],
    keywords: ['清淡', '快手'],
    spicyLevel: 0,
    difficulty: 1,
    minutes: 8,
    tutorialPlatform: '自制',
    tutorialAuthor: '金大厨',
    tutorialUrl: '',
    tutorialNote: '固定教程',
    published: true,
  },
  {
    id: 'fish',
    chefId: 'chen',
    name: '剁椒鱼头',
    aliases: [],
    category: '鱼虾海鲜',
    coverUrl: '',
    ingredients: [{ name: '鱼头', amount: '1个' }],
    steps: ['腌制', '蒸制'],
    keywords: ['周末硬菜'],
    spicyLevel: 3,
    difficulty: 3,
    minutes: 55,
    tutorialPlatform: '小红书',
    tutorialAuthor: '陈大厨',
    tutorialUrl: 'https://example.com/fish',
    tutorialNote: '固定教程',
    published: true,
  },
]

describe('menu domain', () => {
  it('formats Shanghai date with weekday', () => {
    expect(formatShanghaiDate(new Date('2026-06-15T01:00:00Z'))).toContain('星期一')
  })

  it('searches by recipe name, alias, keyword, or ingredient', () => {
    expect(searchRecipes(recipes, '五花肉').map((item) => item.id)).toEqual(['pork'])
    expect(searchRecipes(recipes, '农家').map((item) => item.id)).toEqual(['pork'])
    expect(searchRecipes(recipes, '清淡').map((item) => item.id)).toEqual(['greens'])
  })

  it('prefers two quick dishes on weekdays without duplicates', () => {
    const result = recommendRecipes(recipes, 'chen', new Date('2026-06-15T04:00:00Z'))
    expect(result).toHaveLength(2)
    expect(new Set(result.map((item) => item.id)).size).toBe(2)
    expect(result.every((item) => item.minutes <= 40)).toBe(true)
  })

  it('allows a more complex dish on weekends', () => {
    const result = recommendRecipes(recipes, 'chen', new Date('2026-06-20T04:00:00Z'))
    expect(result.some((item) => item.difficulty >= 3 || item.minutes > 40)).toBe(true)
  })

  it('returns the same quote for the same Shanghai date', () => {
    const first = dailyQuote(new Date('2026-06-15T01:00:00Z'))
    const second = dailyQuote(new Date('2026-06-15T12:00:00Z'))
    expect(first).toBe(second)
  })
})
