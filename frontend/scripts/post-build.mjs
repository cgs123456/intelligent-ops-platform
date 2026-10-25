import { copyFileSync, mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

// Vite 产物 index.html 直接作为 Flask 模板（产物内已是 /static/dist/ 绝对路径）
// 依赖仓库同构布局：frontend/ 与 static/、templates/ 同级（Docker 构建阶段 WORKDIR /repo/frontend 同构）
const repoRoot = resolve(import.meta.dirname, '../..')
const src = resolve(repoRoot, 'static/dist/index.html')
const dst = resolve(repoRoot, 'templates/index.html')
mkdirSync(resolve(repoRoot, 'templates'), { recursive: true })
copyFileSync(src, dst)
console.log(`templates/index.html updated from vite build (root: ${repoRoot})`)
