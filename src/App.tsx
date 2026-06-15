import { HashRouter, NavLink, Route, Routes } from 'react-router-dom'
import { ChefSwitcher } from './components/ChefSwitcher'
import { HistoryPage } from './pages/HistoryPage'
import { HomePage } from './pages/HomePage'
import { RecipeDetailPage } from './pages/RecipeDetailPage'
import { RecipesPage } from './pages/RecipesPage'
import { TodayMenuPage } from './pages/TodayMenuPage'
import { AdminPage } from './pages/AdminPage'
import './App.css'

function App() {
  return (
    <HashRouter>
      <div className="site-shell">
        <header className="site-header">
          <NavLink to="/" className="brand"><span>{'{陈}'}</span><div><b>今晚谁掌勺？</b><small>永州胃 · 上海灶</small></div></NavLink>
          <nav>
            <NavLink to="/">今晚吃啥</NavLink>
            <NavLink to="/recipes">自己点菜</NavLink>
            <NavLink to="/today">今日菜单</NavLink>
            <NavLink to="/history">做菜日历</NavLink>
          </nav>
          <div className="header-actions">
            <div className="desktop-chef-switcher"><ChefSwitcher /></div>
            <NavLink to="/admin" className="kitchen-door">后厨重地</NavLink>
          </div>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/recipes" element={<RecipesPage />} />
            <Route path="/recipes/:id" element={<RecipeDetailPage />} />
            <Route path="/today" element={<TodayMenuPage />} />
            <Route path="/history" element={<HistoryPage />} />
            <Route path="/admin" element={<AdminPage />} />
          </Routes>
        </main>
        <footer><b>陈大厨菜单</b><span>写完代码，也要认真吃饭。</span></footer>
      </div>
    </HashRouter>
  )
}

export default App
