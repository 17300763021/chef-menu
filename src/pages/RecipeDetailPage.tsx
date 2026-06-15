import { Link, useParams } from 'react-router-dom'
import { useApp } from '../app/AppContext'

export function RecipeDetailPage() {
  const { id } = useParams()
  const { recipes, addToMenu, todayMenu } = useApp()
  const recipe = recipes.find((item) => item.id === id)
  if (!recipe) return <div className="page-state">没有找到这份菜谱。</div>
  const inMenu = todayMenu?.recipeIds.includes(recipe.id)

  return (
    <article className="recipe-detail page-top">
      <Link to="/recipes" className="back-link">← 返回菜谱库</Link>
      <div className="detail-hero">
        <div className="detail-emoji">{recipe.coverUrl || '🍲'}</div>
        <div>
          <span className="eyebrow">{recipe.category} · {recipe.minutes} 分钟</span>
          <h1>{recipe.name}</h1>
          <p>{recipe.tutorialNote}</p>
          <button className="primary-button" disabled={inMenu} onClick={() => void addToMenu(recipe.id)}>
            {inMenu ? '已加入今日菜单' : '＋ 加入今日菜单'}
          </button>
        </div>
      </div>
      <div className="detail-columns">
        <section>
          <h2>准备食材</h2>
          <ul className="ingredient-list">
            {recipe.ingredients.map((item) => <li key={item.name}><span>{item.name}</span><b>{item.amount}</b></li>)}
          </ul>
        </section>
        <section>
          <h2>固定教程</h2>
          <ol className="step-list">
            {recipe.steps.map((step, index) => <li key={step}><span>{index + 1}</span><p>{step}</p></li>)}
          </ol>
          <div className="source-box">
            来源：{recipe.tutorialPlatform} · {recipe.tutorialAuthor}
            {recipe.tutorialUrl && <a href={recipe.tutorialUrl} target="_blank" rel="noreferrer">查看原教程</a>}
          </div>
        </section>
      </div>
    </article>
  )
}
