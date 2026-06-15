import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import App from '../App'
import { AppProvider } from './AppContext'
import { LocalRepository } from '../data/repository'

describe('chef menu application', () => {
  it('switches chefs and shows two daily recommendations', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)
    expect(await screen.findByText('今天推荐这两道')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /加入今日菜单/ })).toHaveLength(2)
    await userEvent.click(screen.getAllByRole('button', { name: /金大厨/ })[0])
    expect(screen.getByText(/金大厨今日掌勺/)).toBeInTheDocument()
  })

  it('searches recipes and adds one to today menu', async () => {
    render(<AppProvider repository={new LocalRepository()}><App /></AppProvider>)
    await userEvent.click((await screen.findAllByRole('link', { name: '自己点菜' }))[0])
    await userEvent.type(screen.getByRole('searchbox'), '五花肉')
    expect(await screen.findByText('辣椒炒肉')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /加入今日菜单/ }))
    await userEvent.click(screen.getByRole('link', { name: /今日菜单/ }))
    await waitFor(() => expect(screen.getByText('辣椒炒肉')).toBeInTheDocument())
  })
})
