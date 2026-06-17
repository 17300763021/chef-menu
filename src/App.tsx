import { HashRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { ChefSwitcher } from './components/ChefSwitcher'
import { HistoryPage } from './pages/HistoryPage'
import { HomePage } from './pages/HomePage'
import { RecipeDetailPage } from './pages/RecipeDetailPage'
import { RecipesPage } from './pages/RecipesPage'
import { TodayMenuPage } from './pages/TodayMenuPage'
import { AdminPage } from './pages/AdminPage'
import StockDashboard from './features/stocks/StockDashboard'
import './App.css'

function AppRoutes() {
  const location = useLocation()

  if (location.pathname === '/stocks') {
    return (
      <main className="stock-standalone-main">
        <Routes>
          <Route path="/stocks" element={<StockDashboard />} />
        </Routes>
      </main>
    )
  }

  return (
    <div className="site-shell">
      <header className="site-header">
        <NavLink to="/" className="brand">
          <span>厨</span>
          <div>
            <b>今晚谁掌勺？</b>
            <small>家常菜单助手</small>
          </div>
        </NavLink>
        <nav>
          <NavLink to="/">今晚吃啥</NavLink>
          <NavLink to="/recipes">自己点菜</NavLink>
          <NavLink to="/today">今日菜单</NavLink>
          <NavLink to="/history">做菜日历</NavLink>
        </nav>
        <div className="header-actions">
          <div className="desktop-chef-switcher"><ChefSwitcher /></div>
          <NavLink to="/stocks" className="stock-door">股票助手</NavLink>
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
  )
}

function App() {
  return (
    <HashRouter>
      <AppRoutes />
    </HashRouter>
  )
}

export default App
