import { supabase } from '../lib/supabase'

export async function signIn(email: string, password: string) {
  if (!supabase) throw new Error('还没有配置 Supabase 环境变量。')
  const { data, error } = await supabase.auth.signInWithPassword({ email, password })
  if (error) throw error
  const { data: isAdmin, error: adminError } = await supabase.rpc('is_admin')
  if (adminError || !isAdmin) {
    await supabase.auth.signOut()
    throw new Error('这个账号不是网站管理员。')
  }
  return data.user
}

export async function signOut() {
  await supabase?.auth.signOut()
}

export async function getAdminUser() {
  if (!supabase) return null
  const { data } = await supabase.auth.getUser()
  if (!data.user) return null
  const { data: isAdmin } = await supabase.rpc('is_admin')
  return isAdmin ? data.user : null
}
