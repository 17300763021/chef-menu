import { Link } from 'react-router-dom'
import { useApp } from '../app/AppContext'
import { ChefSwitcher } from '../components/ChefSwitcher'
import { RecipeCard } from '../components/RecipeCard'
import { formatShanghaiDate, isShanghaiWeekend } from '../domain/menu'

export function HomePage() {
  const {
    selectedChef,
    recipes,
    todayMenu,
    recommendationIds,
    addToMenu,
    replaceRecommendations,
    loading,
  } = useApp()
  const recommendations = recommendationIds
    .map((id) => recipes.find((recipe) => recipe.id === id))
    .filter((recipe): recipe is NonNullable<typeof recipe> => Boolean(recipe))

  if (loading || !selectedChef) return <div className="page-state">正在准备今天的菜单…</div>

  return (
    <>
      <section className={`hero-panel theme-${selectedChef.theme}`}>
        <div className="hero-copy">
          <ChefSwitcher />
          <div className="eyebrow">{formatShanghaiDate()} · {isShanghaiWeekend() ? '周末丰盛菜单' : '工作日家常菜单'}</div>
          <h1>{selectedChef.name}今日掌勺</h1>
          <p className="hero-quote">“{todayMenu?.quote}”</p>
          <p>{selectedChef.bio}</p>
          <div className="hero-actions">
            <Link className="primary-button" to="/recipes">🍽️ 自己点菜</Link>
            <button className="ghost-button" type="button" onClick={() => void replaceRecommendations()}>🎲 换一组</button>
          </div>
        </div>
        <img className="hero-avatar" src={selectedChef.avatarUrl} alt={`${selectedChef.name}动漫头像`} />
      </section>

      <section className="section-block">
        <div className="section-heading">
          <div><span className="eyebrow">TODAY'S PICK</span><h2>今天推荐这两道</h2></div>
          <Link to="/today">查看今日菜单 →</Link>
        </div>
        <div className="recipe-grid two">
          {recommendations.map((recipe) => (
            <RecipeCard
              key={recipe.id}
              recipe={recipe}
              onAdd={addToMenu}
              inMenu={todayMenu?.recipeIds.includes(recipe.id)}
            />
          ))}
        </div>
      </section>

      <section className="feature-grid">
        <Link to="/recipes"><b>按食材找菜</b><span>八大分类，支持菜名和食材搜索</span></Link>
        <Link to="/today"><b>今晚菜单</b><span>自由组合，跟着固定教程做饭</span></Link>
        <Link to="/history"><b>做菜日历</b><span>照片、评分和心得都留下来</span></Link>
      </section>
    </>
  )
}
