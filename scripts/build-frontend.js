#!/usr/bin/env node
/**
 * 前端构建脚本（P2-16）
 *
 * 目标：
 *   - 读取 static/css/style.css 与 static/js/app.js
 *   - 输出压缩后的 static/dist/style.min.css 与 static/dist/app.min.js
 *   - 生成 static/dist/manifest.json（源文件→压缩文件映射 + 版本号，用于缓存破坏）
 *
 * 容错策略：
 *   - 若 node_modules 中存在 terser / clean-css，则调用其 API 进行高质量压缩；
 *   - 否则回退到简易正则压缩（去注释/去多余空白），并打印警告，保证脚本可独立运行。
 *
 * 不改动已有的前端源代码，仅做产物生成。
 */

const fs = require('fs');
const path = require('path');

// ===== 路径配置 =====
const ROOT = path.resolve(__dirname, '..');
const SRC_CSS = path.join(ROOT, 'static', 'css', 'style.css');
const SRC_JS = path.join(ROOT, 'static', 'js', 'app.js');
const DIST_DIR = path.join(ROOT, 'static', 'dist');
const OUT_CSS = path.join(DIST_DIR, 'style.min.css');
const OUT_JS = path.join(DIST_DIR, 'app.min.js');
const MANIFEST = path.join(DIST_DIR, 'manifest.json');

// 版本号：优先读取 package.json 的 version，回退到固定值
const VERSION = (function readVersion() {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
    return pkg.version || '1.0.0';
  } catch (_) {
    return '1.0.0';
  }
})();

// 构建时间戳（ISO 8601）
const BUILD_TIME = new Date().toISOString();

// ===== 工具函数 =====
function ensureDir(dir) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function readText(file) {
  return fs.readFileSync(file, 'utf8');
}

// 尝试加载可选依赖
function tryRequire(name) {
  try {
    return require(name);
  } catch (_) {
    return null;
  }
}

// ===== 简易正则压缩（回退方案） =====
function naiveMinifyCSS(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')   // 去块注释
    .replace(/\/\/.*$/gm, '')            // 去行注释（CSS 中少见，但保留处理）
    .replace(/\s+/g, ' ')                // 折叠空白
    .replace(/\s*([{}:;,])\s*/g, '$1')   // 去标点周围空白
    .replace(/;}/g, '}')                 // 去最后一个分号
    .trim();
}

function naiveMinifyJS(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')          // 去块注释
    .replace(/(^|[^:\\])\/\/.*$/gm, '$1')       // 去行注释（保护字符串内的 //）
    .replace(/^\s*[\r\n]/gm, '')                // 去空行
    .replace(/[ \t]+/g, ' ')                    // 折叠空格/制表符
    .replace(/\s*([=+\-*/{}();,:<>!&|?])\s*/g, '$1') // 去标点周围空白
    .trim();
}

// ===== 主流程 =====
function main() {
  ensureDir(DIST_DIR);

  // 读取源文件
  if (!fs.existsSync(SRC_CSS)) {
    console.error(`[构建] 未找到源文件：${SRC_CSS}`);
    process.exit(1);
  }
  if (!fs.existsSync(SRC_JS)) {
    console.error(`[构建] 未找到源文件：${SRC_JS}`);
    process.exit(1);
  }

  const cssSrc = readText(SRC_CSS);
  const jsSrc = readText(SRC_JS);

  // ----- CSS 压缩 -----
  let cssOut;
  const CleanCSS = tryRequire('clean-css');
  if (CleanCSS) {
    const result = new CleanCSS({ returnPromise: false }).minify(cssSrc);
    cssOut = result.styles || naiveMinifyCSS(cssSrc);
    if (result.errors && result.errors.length) {
      console.warn(`[CSS] clean-css 报告错误：${result.errors.join('; ')}`);
    }
  } else {
    console.warn('[CSS] 未检测到 clean-css，回退到简易正则压缩（生产环境建议安装依赖）。');
    cssOut = naiveMinifyCSS(cssSrc);
  }

  // ----- JS 压缩 -----
  let jsOut;
  const terser = tryRequire('terser');
  if (terser) {
    // terser 为异步 API
    terser.minify(jsSrc, { compress: true, mangle: true })
      .then((result) => {
        if (result.error) {
          console.warn(`[JS] terser 错误：${result.error.message}，回退到简易压缩。`);
          jsOut = naiveMinifyJS(jsSrc);
        } else {
          jsOut = result.code;
        }
        writeOutputs(cssOut, jsOut);
      })
      .catch((err) => {
        console.warn(`[JS] terser 异常：${err.message}，回退到简易压缩。`);
        jsOut = naiveMinifyJS(jsSrc);
        writeOutputs(cssOut, jsOut);
      });
  } else {
    console.warn('[JS] 未检测到 terser，回退到简易正则压缩（生产环境建议安装依赖）。');
    jsOut = naiveMinifyJS(jsSrc);
    writeOutputs(cssOut, jsOut);
  }
}

// ===== 写入产物 + 生成 manifest =====
function writeOutputs(cssOut, jsOut) {
  fs.writeFileSync(OUT_CSS, cssOut, 'utf8');
  fs.writeFileSync(OUT_JS, jsOut, 'utf8');

  const manifest = {
    version: VERSION,
    builtAt: BUILD_TIME,
    files: {
      'static/css/style.css': 'static/dist/style.min.css',
      'static/js/app.js': 'static/dist/app.min.js'
    }
  };
  fs.writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2), 'utf8');

  // 构建结果摘要
  console.log('========== 前端构建完成 ==========');
  console.log(`版本号     : ${VERSION}`);
  console.log(`构建时间   : ${BUILD_TIME}`);
  console.log(`CSS  源大小: ${Buffer.byteLength(readText(SRC_CSS), 'utf8')} bytes`);
  console.log(`CSS  产物 : ${OUT_CSS} (${Buffer.byteLength(cssOut, 'utf8')} bytes)`);
  console.log(`JS   源大小: ${Buffer.byteLength(readText(SRC_JS), 'utf8')} bytes`);
  console.log(`JS   产物 : ${OUT_JS} (${Buffer.byteLength(jsOut, 'utf8')} bytes)`);
  console.log(`清单文件  : ${MANIFEST}`);
  console.log('==================================');
}

main();
