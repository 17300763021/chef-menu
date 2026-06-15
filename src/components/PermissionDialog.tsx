import { useEffect } from 'react'

export function PermissionDialog({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <section
        className="permission-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="permission-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-icon">👨‍🍳</div>
        <span className="eyebrow">KITCHEN LAW</span>
        <h2 id="permission-title">看菜可以，点菜不行。</h2>
        <p>后厨兵权只归掌勺人。你负责挑菜流口水，今日菜单由大厨亲自定夺。</p>
        <button className="primary-button" type="button" onClick={onClose}>遵命，继续看菜</button>
      </section>
    </div>
  )
}
