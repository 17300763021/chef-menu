import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App from '../App'
import { LocalRepository } from '../data/repository'
import { AppProvider } from './AppContext'

describe('visitor permissions', () => {
  it('shows a dialog instead of adding a recipe', async () => {
    window.location.hash = '#/recipes'
    const { container } = render(
      <AppProvider repository={new LocalRepository()}><App /></AppProvider>,
    )
    const addButton = await new Promise<HTMLButtonElement>((resolve) => {
      const timer = window.setInterval(() => {
        const button = container.querySelector<HTMLButtonElement>('.recipe-card .small-button')
        if (button) {
          window.clearInterval(timer)
          resolve(button)
        }
      }, 10)
    })
    fireEvent.click(addButton)
    expect(await screen.findByRole('dialog')).toHaveTextContent('看菜可以，点菜不行')
  })

  it('uses a separate admin entrance', async () => {
    window.location.hash = '#/'
    const { container } = render(
      <AppProvider repository={new LocalRepository()}><App /></AppProvider>,
    )
    expect(container.querySelector('nav a[href="#/admin"]')).toBeNull()
    expect(await screen.findByRole('link', { name: '后厨重地' })).toHaveAttribute('href', '#/admin')
  })
})
