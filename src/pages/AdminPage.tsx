import { useEffect, useState, type FormEvent } from 'react'
import { useApp } from '../app/AppContext'
import { demoChefs, demoRecipes } from '../data/demoData'
import { appRepository } from '../data/supabaseRepository'
import { RECIPE_CATEGORIES, type RecipeCategory } from '../domain/types'
import { getAdminUser, signIn, signOut } from '../features/auth'
import { uploadImage } from '../features/image'
import { supabase } from '../lib/supabase'

export function AdminPage() {
  const { chefs, refresh } = useApp()
  const [userEmail, setUserEmail] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void getAdminUser().then((user) => setUserEmail(user?.email ?? ''))
  }, [])

  const login = async (event: FormEvent) => {
    event.preventDefault()
    setMessage('')
    try {
      const user = await signIn(email, password)
      setUserEmail(user.email ?? '')
      setPassword('')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '登录失败')
    }
  }

  const seedCloud = async () => {
    if (!supabase) return
    setSaving(true)
    setMessage('')
    try {
      const chefRows = demoChefs.map((chef) => ({
        name: chef.name,
        slug: chef.slug,
        avatar_url: chef.avatarUrl,
        theme: chef.theme,
        bio: chef.bio,
        specialties: chef.specialties,
      }))
      const { data: savedChefs, error: chefError } = await supabase
        .from('chefs').upsert(chefRows, { onConflict: 'slug' }).select()
      if (chefError) throw chefError
      const ids = new Map(savedChefs.map((chef) => [chef.slug, chef.id]))
      const recipeRows = demoRecipes.map((recipe) => ({
        chef_id: ids.get(recipe.chefId === 'chen' ? 'chen-chef' : 'jin-chef'),
        name: recipe.name,
        aliases: recipe.aliases,
        category: recipe.category,
        cover_url: recipe.coverUrl,
        ingredients: recipe.ingredients,
        steps: recipe.steps,
        keywords: recipe.keywords,
        spicy_level: recipe.spicyLevel,
        difficulty: recipe.difficulty,
        minutes: recipe.minutes,
        tutorial_platform: recipe.tutorialPlatform,
        tutorial_author: recipe.tutorialAuthor,
        tutorial_url: recipe.tutorialUrl,
        tutorial_note: recipe.tutorialNote,
        is_published: true,
      }))
      const { error: recipeError } = await supabase.from('recipes').insert(recipeRows)
      if (recipeError) throw recipeError
      setMessage('云端基础数据已建立。')
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '初始化失败')
    } finally {
      setSaving(false)
    }
  }

  const saveRecipe = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSaving(true)
    setMessage('')
    const form = new FormData(event.currentTarget)
    try {
      let coverUrl = String(form.get('emoji') || '🍲')
      const image = form.get('cover')
      if (image instanceof File && image.size && supabase) {
        coverUrl = await uploadImage(supabase, 'recipe-images', crypto.randomUUID(), image)
      }
      const ingredientNames = String(form.get('ingredients')).split('\n').map((item) => item.trim()).filter(Boolean)
      const steps = String(form.get('steps')).split('\n').map((item) => item.trim()).filter(Boolean)
      await appRepository.saveRecipe({
        chefId: String(form.get('chefId')),
        name: String(form.get('name')),
        aliases: [],
        category: String(form.get('category')) as RecipeCategory,
        coverUrl,
        ingredients: ingredientNames.map((name) => ({ name, amount: '适量' })),
        steps,
        keywords: String(form.get('keywords')).split(',').map((item) => item.trim()).filter(Boolean),
        spicyLevel: Number(form.get('spicyLevel')),
        difficulty: Number(form.get('difficulty')),
        minutes: Number(form.get('minutes')),
        tutorialPlatform: String(form.get('tutorialPlatform')),
        tutorialAuthor: String(form.get('tutorialAuthor')),
        tutorialUrl: String(form.get('tutorialUrl')),
        tutorialNote: String(form.get('tutorialNote')),
        published: true,
      })
      event.currentTarget.reset()
      setMessage('菜谱已保存，教程内容已经固定。')
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  if (!userEmail) {
    return (
      <section className="section-block page-top admin-intro">
        <span className="eyebrow">OWNER ONLY</span>
        <h1>大厨管理台</h1>
        <p>只有站长账号可以新增菜谱、固定教程和上传照片。</p>
        <form className="login-card" onSubmit={login}>
          <label>管理员邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <button className="primary-button" type="submit">登录</button>
          {message && <p className="form-message">{message}</p>}
        </form>
      </section>
    )
  }

  return (
    <section className="section-block page-top">
      <div className="section-heading">
        <div><span className="eyebrow">SIGNED IN · {userEmail}</span><h1>大厨管理台</h1></div>
        <button className="ghost-button" type="button" onClick={() => void signOut().then(() => setUserEmail(''))}>退出登录</button>
      </div>
      <div className="admin-tools">
        <div className="admin-seed">
          <h2>第一次使用</h2>
          <p>如果 Supabase 还是空表，先把双大厨和 16 道演示菜同步到云端。</p>
          <button className="primary-button" type="button" disabled={saving} onClick={() => void seedCloud()}>初始化云端基础数据</button>
        </div>
        <form className="recipe-form" onSubmit={saveRecipe}>
          <h2>新增固定菜谱</h2>
          <div className="form-grid">
            <label>菜名<input name="name" required /></label>
            <label>所属大厨<select name="chefId" required>{chefs.map((chef) => <option key={chef.id} value={chef.id}>{chef.name}</option>)}</select></label>
            <label>食材分类<select name="category">{RECIPE_CATEGORIES.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label>耗时（分钟）<input name="minutes" type="number" defaultValue="20" min="1" required /></label>
            <label>辣度<select name="spicyLevel"><option value="0">不辣</option><option value="1">微辣</option><option value="2">中辣</option><option value="3">很辣</option></select></label>
            <label>难度<select name="difficulty"><option value="1">简单</option><option value="2">普通</option><option value="3">复杂</option></select></label>
            <label>菜品符号<input name="emoji" defaultValue="🍲" /></label>
            <label>封面照片<input name="cover" type="file" accept="image/*" /></label>
          </div>
          <label>食材（每行一个）<textarea name="ingredients" required placeholder={'五花肉 250克\n青椒 4个'} /></label>
          <label>步骤（每行一步）<textarea name="steps" required placeholder={'五花肉切片煸香\n加入青椒翻炒'} /></label>
          <label>标签（英文逗号分隔）<input name="keywords" placeholder="家常,下饭,工作日" /></label>
          <div className="form-grid">
            <label>教程平台<input name="tutorialPlatform" placeholder="小红书 / B站 / 自制" /></label>
            <label>原作者<input name="tutorialAuthor" /></label>
          </div>
          <label>教程原链接<input name="tutorialUrl" type="url" placeholder="https://..." /></label>
          <label>个人笔记<textarea name="tutorialNote" placeholder="这份教程以后固定显示。" /></label>
          <button className="primary-button" type="submit" disabled={saving}>{saving ? '保存中…' : '保存固定教程'}</button>
        </form>
      </div>
      {message && <p className="form-message sticky-message">{message}</p>}
    </section>
  )
}
