/**
 * Markdown 安全渲染:先转义全部 HTML,再做有限 Markdown 转换。
 * 不透传任何原始 HTML(v-html 前的唯一入口);不支持的语法按纯文本展示。
 * 围栏代码块逐行识别,不使用占位符替换,避免与正文内容冲突。
 */

const ESC: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

export function renderSafeMd(src: string): string {
  if (!src) return ''
  // 1) 转义全部 HTML 字符,任何注入内容都变成纯文本
  const escaped = src.replace(/[&<>"']/g, (c) => ESC[c])

  const out: string[] = []
  let inFence = false
  let fenceBuf: string[] = []

  for (const line of escaped.split('\n')) {
    // 2) 围栏代码块:内容原样保留(已转义),不做行内 Markdown 处理
    if (line.startsWith('```')) {
      if (inFence) {
        out.push('<pre class="md-code"><code>' + fenceBuf.join('\n') + '</code></pre>')
        fenceBuf = []
        inFence = false
      } else {
        inFence = true
      }
      continue
    }
    if (inFence) {
      fenceBuf.push(line)
      continue
    }
    // 3) 行内代码 / 粗体(内容已转义,不会引入标签)
    out.push(
      line
        .replace(/`([^`]+)`/g, '<code class="md-inline">$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>'),
    )
  }
  // 未闭合围栏按代码块收尾,内容不丢失
  if (fenceBuf.length) {
    out.push('<pre class="md-code"><code>' + fenceBuf.join('\n') + '</code></pre>')
  }

  // 4) 段落换行保留
  return out.join('<br>')
}
