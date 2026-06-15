import { useMemo, useState, type FormEvent } from 'react'
import { useApp } from '../app/AppContext'
import { appRepository } from '../data/supabaseRepository'
import { searchRecipes } from '../domain/menu'
import { RECIPE_CATEGORIES, type Recipe, type RecipeCategory, type RecipeDraft } from '../domain/types'
import { signIn } from '../features/auth'

function RecipeForm({
  recipe,
  onCancel,
  onSaved,
}: {
  recipe: Recipe | null
  onCancel: () => void
  onSaved: (message: string) => Promise<void>
}) {
  const { chefs } = useApp()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSaving(true)
    setError('')
    const form = new FormData(event.currentTarget)
    const draft: RecipeDraft = {
      chefId: String(form.get('chefId')),
      name: String(form.get('name')).trim(),
      aliases: String(form.get('aliases')).split(',').map((item) => item.trim()).filter(Boolean),
      category: String(form.get('category')) as RecipeCategory,
      coverUrl: String(form.get('coverUrl') || '🍲'),
      ingredients: String(form.get('ingredients')).split('\n').map((line) => {
        const [name, ...amount] = line.trim().split(/\s+/)
        return { name, amount: amount.join(' ') || '适量' }
      }).filter((item) => item.name),
      steps: String(form.get('steps')).split('\n').map((item) => item.trim()).filter(Boolean),
      keywords: String(form.get('keywords')).split(',').map((item) => item.trim()).filter(Boolean),
      spicyLevel: Number(form.get('spicyLevel')),
      difficulty: Number(form.get('difficulty')),
      minutes: Number(form.get('minutes')),
      tutorialPlatform: String(form.get('tutorialPlatform')),
      tutorialAuthor: String(form.get('tutorialAuthor')),
      tutorialUrl: String(form.get('tutorialUrl')),
      tutorialNote: String(form.get('tutorialNote')),
      published: form.get('published') === 'on',
    }
    try {
      if (recipe) await appRepository.updateRecipe(recipe.id, draft)
      else await appRepository.saveRecipe(draft)
      await onSaved(recipe ? '菜谱已重新掌勺。' : '新菜已经入册。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="dialog-backdrop">
      <form className="recipe-form admin-editor" onSubmit={submit}>
        <div className="section-heading">
          <div><span className="eyebrow">RECIPE ORDER</span><h2>{recipe ? '编辑菜品' : '新增菜品'}</h2></div>
          <button className="ghost-button" type="button" onClick={onCancel}>关闭</button>
        </div>
        <div className="form-grid">
          <label>菜名<input name="name" defaultValue={recipe?.name} required /></label>
          <label>所属大厨<select name="chefId" defaultValue={recipe?.chefId}>{chefs.map((chef) => <option key={chef.id} value={chef.id}>{chef.name}</option>)}</select></label>
          <label>分类<select name="category" defaultValue={recipe?.category}>{RECIPE_CATEGORIES.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label>封面 Emoji<input name="coverUrl" defaultValue={recipe?.coverUrl || '🍲'} /></label>
          <label>耗时（分钟）<input name="minutes" type="number" min="1" defaultValue={recipe?.minutes ?? 20} required /></label>
          <label>辣度<input name="spicyLevel" type="number" min="0" max="3" defaultValue={recipe?.spicyLevel ?? 0} /></label>
          <label>难度<input name="difficulty" type="number" min="1" max="3" defaultValue={recipe?.difficulty ?? 1} /></label>
          <label>别名（逗号分隔）<input name="aliases" defaultValue={recipe?.aliases.join(',')} /></label>
        </div>
        <label>食材（每行：名称 数量）<textarea name="ingredients" required defaultValue={recipe?.ingredients.map((item) => `${item.name} ${item.amount}`).join('\n')} /></label>
        <label>步骤（每行一步）<textarea name="steps" required defaultValue={recipe?.steps.join('\n')} /></label>
        <label>关键词（逗号分隔）<input name="keywords" defaultValue={recipe?.keywords.join(',')} /></label>
        <div className="form-grid">
          <label>教程平台<input name="tutorialPlatform" defaultValue={recipe?.tutorialPlatform} /></label>
          <label>教程作者<input name="tutorialAuthor" defaultValue={recipe?.tutorialAuthor} /></label>
        </div>
        <label>教程链接<input name="tutorialUrl" type="url" defaultValue={recipe?.tutorialUrl} /></label>
        <label>个人笔记<textarea name="tutorialNote" defaultValue={recipe?.tutorialNote} /></label>
        <label className="checkbox-label"><input name="published" type="checkbox" defaultChecked={recipe?.published ?? true} /> 对外发布</label>
        {error && <p className="form-message error-message">{error}</p>}
        <button className="primary-button" type="submit" disabled={saving}>{saving ? '后厨忙碌中…' : '保存菜品'}</button>
      </form>
    </div>
  )
}

export function AdminPage() {
  const { chefs, recipes, refresh, adminEmail, refreshAdminUser, adminSignOut } = useApp()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<Recipe | null | undefined>(undefined)
  const [deleting, setDeleting] = useState<Recipe | null>(null)
  const [message, setMessage] = useState('')
  const filtered = useMemo(() => searchRecipes(recipes, search), [recipes, search])

  const login = async (event: FormEvent) => {
    event.preventDefault()
    setMessage('')
    try {
      await signIn(email, password)
      await refreshAdminUser()
      setPassword('')
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '登录失败')
    }
  }

  const saved = async (nextMessage: string) => {
    await refresh()
    setEditing(undefined)
    setMessage(nextMessage)
  }

  const remove = async () => {
    if (!deleting) return
    try {
      await appRepository.deleteRecipe(deleting.id)
      setDeleting(null)
      await refresh()
      setMessage('军令已下，这道菜撤了。')
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '撤菜失败')
    }
  }

  if (!adminEmail) {
    return (
      <section className="section-block page-top admin-intro">
        <span className="eyebrow">OWNER ONLY</span>
        <h1>后厨重地</h1>
        <p>闲人免进。只有掌勺人能调兵遣菜。</p>
        <form className="login-card" onSubmit={login}>
          <label>管理员邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <button className="primary-button" type="submit">持令进后厨</button>
          {message && <p className="form-message error-message">{message}</p>}
        </form>
      </section>
    )
  }

  return (
    <section className="section-block page-top">
      <div className="section-heading">
        <div><span className="eyebrow">KITCHEN COMMAND</span><h1>后厨重地</h1><p>菜谱生杀大权，尽在此处。</p></div>
        <div className="admin-account"><small>{adminEmail}</small><button className="ghost-button" type="button" onClick={() => void adminSignOut()}>退出登录</button></div>
      </div>
      <div className="admin-summary">
        <strong>{recipes.length}<span>全部菜品</span></strong>
        <strong>{recipes.filter((recipe) => recipe.published).length}<span>已发布</span></strong>
        <strong>{filtered.length}<span>搜索结果</span></strong>
      </div>
      <div className="admin-toolbar">
        <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="模糊搜索菜名、食材、关键词…" />
        <button className="primary-button" type="button" onClick={() => setEditing(null)}>＋ 新增菜品</button>
      </div>
      <div className="admin-recipe-grid">
        {filtered.map((recipe) => (
          <article className="admin-recipe-card" key={recipe.id}>
            <span className="admin-recipe-emoji">{recipe.coverUrl || '🍲'}</span>
            <div>
              <small>{chefs.find((chef) => chef.id === recipe.chefId)?.name || '未分配'} · {recipe.category}</small>
              <h3>{recipe.name}</h3>
              <p>{recipe.minutes} 分钟 · {recipe.published ? '已发布' : '未发布'}</p>
            </div>
            <div className="admin-card-actions">
              <button className="ghost-button" type="button" onClick={() => setEditing(recipe)}>编辑</button>
              <button className="danger-button" type="button" onClick={() => setDeleting(recipe)}>删除</button>
            </div>
          </article>
        ))}
      </div>
      {!filtered.length && <div className="page-state">后厨翻遍了，也没找到这道菜。</div>}
      {message && <p className="form-message sticky-message">{message}</p>}
      {editing !== undefined && <RecipeForm key={editing?.id || 'new'} recipe={editing} onCancel={() => setEditing(undefined)} onSaved={saved} />}
      {deleting && (
        <div className="dialog-backdrop">
          <section className="permission-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <div className="dialog-icon">🍽️</div>
            <h2 id="delete-title">这道菜真要撤下？</h2>
            <p>删掉后，菜单里就再也点不到它了。后厨军令如山，请确认。</p>
            <div className="dialog-actions">
              <button className="ghost-button" type="button" onClick={() => setDeleting(null)}>先留着</button>
              <button className="danger-button" type="button" onClick={() => void remove()}>确认撤菜</button>
            </div>
          </section>
        </div>
      )}
    </section>
  )
}
