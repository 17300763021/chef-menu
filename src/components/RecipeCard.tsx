import { Link } from 'react-router-dom'
import type { Recipe } from '../domain/types'

export function RecipeCard({
  recipe,
  onAdd,
  inMenu = false,
}: {
  recipe: Recipe
  onAdd?: (id: string) => void
  inMenu?: boolean
}) {
  return (
    <article className="recipe-card">
      <Link to={`/recipes/${recipe.id}`} className="recipe-visual" aria-label={`查看${recipe.name}教程`}>
        <span>{recipe.coverUrl || '🍲'}</span>
        <small>{recipe.category}</small>
      </Link>
      <div className="recipe-card-body">
        <div className="eyebrow">{recipe.minutes} 分钟 · 难度 {recipe.difficulty}</div>
        <h3><Link to={`/recipes/${recipe.id}`}>{recipe.name}</Link></h3>
        <p>{'🌶️'.repeat(recipe.spicyLevel) || '清淡'} · {recipe.keywords.slice(0, 2).join(' · ')}</p>
        {onAdd && (
          <button
            type="button"
            className="small-button"
            disabled={inMenu}
            onClick={() => onAdd(recipe.id)}
            aria-label={`${inMenu ? '已加入' : '加入今日菜单'}：${recipe.name}`}
          >
            {inMenu ? '已在菜单' : '＋ 加入今日菜单'}
          </button>
        )}
      </div>
    </article>
  )
}
