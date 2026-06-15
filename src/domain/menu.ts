import type { Recipe } from './types'

const quotes = [
  '先解决温饱问题，再解决线上问题。',
  '代码可以重构，晚饭不能回滚。',
  '今天的需求很复杂，菜就做简单一点。',
  '空腹写代码，容易把 bug 当特性。',
  '人是铁，饭是钢，外卖偶尔也得下线。',
  '厨房没有缓存，香味每次都要重新计算。',
  '下班后的最高优先级：开火，吃饭。',
  '一荤一素不是妥协，是稳定版本。',
]

function shanghaiParts(date: Date) {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    weekday: 'long',
  }).formatToParts(date)

  return Object.fromEntries(parts.map(({ type, value }) => [type, value]))
}

export function shanghaiDateKey(date = new Date()) {
  const parts = shanghaiParts(date)
  return `${parts.year}-${parts.month}-${parts.day}`
}

export function formatShanghaiDate(date = new Date()) {
  const parts = shanghaiParts(date)
  return `${parts.year} 年 ${Number(parts.month)} 月 ${Number(parts.day)} 日 · ${parts.weekday}`
}

export function isShanghaiWeekend(date = new Date()) {
  const weekday = shanghaiParts(date).weekday
  return weekday === '星期六' || weekday === '星期日'
}

function normalizedRecipeText(recipe: Recipe) {
  return [
    recipe.name,
    ...recipe.aliases,
    recipe.category,
    ...recipe.keywords,
    ...recipe.ingredients.flatMap((item) => [item.name, item.amount]),
  ]
    .join(' ')
    .toLocaleLowerCase('zh-CN')
}

export function searchRecipes(recipes: Recipe[], keyword: string) {
  const normalized = keyword.trim().toLocaleLowerCase('zh-CN')
  if (!normalized) return recipes
  return recipes.filter((recipe) => normalizedRecipeText(recipe).includes(normalized))
}

function dateHash(value: string) {
  return [...value].reduce((total, character) => (total * 31 + character.charCodeAt(0)) >>> 0, 7)
}

function rotate<T>(items: T[], offset: number) {
  if (!items.length) return []
  const index = offset % items.length
  return [...items.slice(index), ...items.slice(0, index)]
}

export function recommendRecipes(
  recipes: Recipe[],
  chefId: string,
  date = new Date(),
  excludedIds: string[] = [],
) {
  const available = recipes.filter((recipe) => recipe.published && !excludedIds.includes(recipe.id))
  const weekend = isShanghaiWeekend(date)
  const key = `${shanghaiDateKey(date)}-${chefId}`
  const hash = dateHash(key)

  const preferred = available.filter((recipe) => recipe.chefId === chefId)
  const others = available.filter((recipe) => recipe.chefId !== chefId)
  const ordered = rotate([...preferred, ...others], hash)

  if (weekend) {
    const complex = ordered.filter((recipe) => recipe.difficulty >= 3 || recipe.minutes > 40)
    const first = complex[0] ?? ordered[0]
    const second = ordered.find((recipe) => recipe.id !== first?.id)
    return [first, second].filter((recipe): recipe is Recipe => Boolean(recipe))
  }

  const quick = ordered.filter((recipe) => recipe.minutes <= 40 && recipe.difficulty <= 2)
  const balanced = [
    quick.find((recipe) => recipe.category !== '蔬菜菌菇'),
    quick.find((recipe) => recipe.category === '蔬菜菌菇'),
    ...quick,
  ].filter((recipe): recipe is Recipe => Boolean(recipe))

  return [...new Map(balanced.map((recipe) => [recipe.id, recipe])).values()].slice(0, 2)
}

export function dailyQuote(date = new Date()) {
  return quotes[dateHash(shanghaiDateKey(date)) % quotes.length]
}
