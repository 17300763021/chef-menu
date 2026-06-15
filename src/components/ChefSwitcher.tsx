import { useApp } from '../app/AppContext'

export function ChefSwitcher() {
  const { chefs, selectedChefId, setSelectedChefId } = useApp()
  return (
    <div className="chef-switcher" aria-label="选择今日大厨">
      {chefs.map((chef) => (
        <button
          key={chef.id}
          className={`chef-pill ${chef.theme} ${selectedChefId === chef.id ? 'active' : ''}`}
          onClick={() => setSelectedChefId(chef.id)}
          type="button"
        >
          <img src={chef.avatarUrl} alt="" />
          <span>{chef.name}</span>
        </button>
      ))}
    </div>
  )
}
