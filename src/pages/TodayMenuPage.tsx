import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../app/AppContext'
import { uploadImage } from '../features/image'
import { supabase } from '../lib/supabase'

export function TodayMenuPage() {
  const { recipes, todayMenu, selectedChef, removeFromMenu, completeTodayMenu } = useApp()
  const [reflection, setReflection] = useState('')
  const [rating, setRating] = useState(5)
  const [photo, setPhoto] = useState<File | null>(null)
  const [message, setMessage] = useState('')
  const menuRecipes = recipes.filter((recipe) => todayMenu?.recipeIds.includes(recipe.id))

  return (
    <section className="section-block page-top">
      <div className="section-heading">
        <div><span className="eyebrow">{selectedChef?.name} · {todayMenu?.menuDate}</span><h1>今日菜单</h1></div>
        <Link to="/recipes">＋ 再点一道</Link>
      </div>
      {menuRecipes.length ? (
        <div className="menu-list">
          {menuRecipes.map((recipe, index) => (
            <article key={recipe.id}>
              <span className="menu-index">{String(index + 1).padStart(2, '0')}</span>
              <span className="menu-emoji">{recipe.coverUrl}</span>
              <div><h3>{recipe.name}</h3><p>{recipe.minutes} 分钟 · {recipe.category}</p></div>
              <Link to={`/recipes/${recipe.id}`}>看教程</Link>
              <button type="button" onClick={() => void removeFromMenu(recipe.id)}>移除</button>
            </article>
          ))}
        </div>
      ) : <div className="page-state">菜单还是空的，先去点两道菜吧。</div>}

      <form className="completion-card" onSubmit={async (event) => {
        event.preventDefault()
        try {
          const photoUrls = photo && supabase
            ? [await uploadImage(supabase, 'cooking-records', todayMenu?.menuDate ?? 'today', photo)]
            : []
          await completeTodayMenu({ rating, reflection, photoUrls })
          setMessage('今天的菜单已经记进做菜日历。')
        } catch (error) {
          setMessage(error instanceof Error ? error.message : '打卡失败')
        }
      }}>
        <div><span className="eyebrow">DINNER DONE</span><h2>吃完来打个卡</h2><p>照片上传在管理员页面开放，先留下评分和心得。</p></div>
        <label>评分
          <select value={rating} onChange={(event) => setRating(Number(event.target.value))}>
            {[5, 4, 3, 2, 1].map((item) => <option key={item} value={item}>{item} 星</option>)}
          </select>
        </label>
        <label>做菜心得
          <textarea value={reflection} onChange={(event) => setReflection(event.target.value)} placeholder="今天哪里做得特别好？" />
        </label>
        <label>成品照片
          <input type="file" accept="image/*" onChange={(event) => setPhoto(event.target.files?.[0] ?? null)} />
        </label>
        <button className="primary-button" type="submit" disabled={!menuRecipes.length}>完成今日菜单</button>
        {message && <p className="form-message">{message}</p>}
      </form>
    </section>
  )
}
