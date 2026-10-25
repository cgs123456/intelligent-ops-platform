import { copyFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Vite 产物 index.html 直接作为 Flask 模板（产物内已是 /static/dist/ 绝对路径）
const src = resolve(import.meta.dirname, '../../static/dist/index.html')
const dst = resolve(import.meta.dirname, '../../templates/index.html')
copyFileSync(src, dst)
console.log('templates/index.html updated from vite build')
