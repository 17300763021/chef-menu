import { useApp } from '../app/AppContext'

export function HistoryPage() {
  const { history, chefs, recipes } = useApp()
  return (
    <section className="section-block page-top">
      <div className="section-heading"><div><span className="eyebrow">COOKING CALENDAR</span><h1>做菜日历</h1></div></div>
      {history.length ? (
        <div className="history-grid">
          {history.map(({ menu, record }) => (
            <article key={menu.id}>
              <time>{menu.menuDate}</time>
              <h3>{chefs.find((chef) => chef.id === menu.chefId)?.name}</h3>
              <p>{recipes.filter((recipe) => menu.recipeIds.includes(recipe.id)).map((recipe) => recipe.name).join(' · ')}</p>
              <strong>{'★'.repeat(record?.rating ?? 0)}</strong>
              <blockquote>{record?.reflection || '这一天认真吃过饭。'}</blockquote>
            </article>
          ))}
        </div>
      ) : <div className="page-state">完成第一顿菜单后，记录会出现在这里。</div>}
    </section>
  )
}
