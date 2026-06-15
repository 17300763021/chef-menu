export function validateImage(file: File) {
  if (!file.type.startsWith('image/')) return '请选择 JPG、PNG 或 WebP 图片。'
  if (file.size > 10 * 1024 * 1024) return '原图不能超过 10 MB。'
  return null
}

export function imagePath(folder: string, filename: string, timestamp = Date.now()) {
  const safeName = filename
    .replace(/\.[^.]+$/, '')
    .replace(/[^a-zA-Z0-9_-]+/g, '-')
    .replace(/^-|-$/g, '') || 'photo'
  return `${folder}/${timestamp}-${safeName}.webp`
}

export async function compressImage(file: File, maxEdge = 1600, quality = 0.82) {
  const error = validateImage(file)
  if (error) throw new Error(error)
  const bitmap = await createImageBitmap(file)
  const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(bitmap.width * scale)
  canvas.height = Math.round(bitmap.height * scale)
  const context = canvas.getContext('2d')
  if (!context) throw new Error('浏览器无法处理这张图片。')
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((result) => result ? resolve(result) : reject(new Error('图片压缩失败。')), 'image/webp', quality)
  })
  bitmap.close()
  return new File([blob], file.name.replace(/\.[^.]+$/, '.webp'), { type: 'image/webp' })
}

export async function uploadImage(
  client: NonNullable<typeof import('../lib/supabase').supabase>,
  bucket: string,
  folder: string,
  file: File,
) {
  const compressed = await compressImage(file)
  const path = imagePath(folder, compressed.name)
  const { error } = await client.storage.from(bucket).upload(path, compressed, {
    contentType: 'image/webp',
    upsert: false,
  })
  if (error) throw error
  return client.storage.from(bucket).getPublicUrl(path).data.publicUrl
}
