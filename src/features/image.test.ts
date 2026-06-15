import { describe, expect, it } from 'vitest'
import { imagePath, validateImage } from './image'

describe('image utilities', () => {
  it('accepts common image files up to 10MB', () => {
    const file = new File(['image'], 'dish.jpg', { type: 'image/jpeg' })
    expect(validateImage(file)).toBeNull()
  })

  it('rejects non-image files', () => {
    const file = new File(['text'], 'notes.txt', { type: 'text/plain' })
    expect(validateImage(file)).toContain('图片')
  })

  it('creates a stable storage folder with a webp extension', () => {
    expect(imagePath('recipe-1', 'dish.jpg', 123)).toBe('recipe-1/123-dish.webp')
  })
})
