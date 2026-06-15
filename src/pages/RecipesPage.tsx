import { useMemo, useState } from 'react'
import { useApp } from '../app/AppContext'
import { RecipeCard } from '../components/RecipeCard'
import { RECIPE_CATEGORIES } from '../domain/types'
import { searchRecipes } from '../domain/menu'

export function RecipesPage() {
  const { recipes, todayMenu, addToMenu } = useApp()
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('')
  const filtered = useMemo(
    () => searchRecipes(recipes, search).filter((recipe) => !category || recipe.category === category),
    [recipes, search, category],
  )

  return (
    <section className="section-block page-top">
      <div className="section-heading">
        <div><span className="eyebrow">RECIPE LIBRARY</span><h1>自己点菜</h1></div>
        <span>{filtered.length} 道菜</span>
      </div>
      <div className="filter-panel">
        <label>
          <span>搜索菜名或食材</span>
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="例如：鸡蛋、五花肉、下饭菜"
          />
        </label>
        <div className="category-list">
          <button type="button" className={!category ? 'selected' : ''} onClick={() => setCategory('')}>全部</button>
          {RECIPE_CATEGORIES.map((item) => (
            <button type="button" className={category === item ? 'selected' : ''} key={item} onClick={() => setCategory(item)}>{item}</button>
          ))}
        </div>
      </div>
      {filtered.length ? (
        <div className="recipe-grid">
          {filtered.map((recipe) => (
            <RecipeCard
              key={recipe.id}
              recipe={recipe}
              onAdd={addToMenu}
              inMenu={todayMenu?.recipeIds.includes(recipe.id)}
            />
          ))}
        </div>
      ) : <div className="page-state">没有找到这道菜，换个食材试试。</div>}
    </section>
  )
}
