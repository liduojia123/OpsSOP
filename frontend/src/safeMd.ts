/**
 * Markdown 安全渲染:先转义全部 HTML,再做有限的块级/行内转换。
 * - 不透传任何原始 HTML(v-html 前的唯一入口);
 * - 链接只允许 http(s)://、站内绝对路径 / 与锚点 #,其他协议按纯文本;
 * - 支持标题、列表、表格、引用、分隔线、围栏代码块与常见行内语法;
 * - 流式输出中的未闭合结构(代码块/表格)按当前内容收尾,不丢文本。
 */

const ESC: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

const FENCE = /^\s*```/
const HEADING = /^(#{1,6})\s+(.*)$/
const HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/
const UL = /^\s*[-*+]\s+(.*)$/
const OL = /^\s*\d{1,3}[.)]\s+(.*)$/
const QUOTE = /^\s*&gt;\s?(.*)$/

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (char) => ESC[char])
}

function splitRow(line: string): string[] {
  let text = line.trim()
  if (text.startsWith('|')) text = text.slice(1)
  if (text.endsWith('|')) text = text.slice(0, -1)
  return text.split('|').map((cell) => cell.trim())
}

function isTableSeparator(line: string): boolean {
  if (!line.includes('|')) return false
  const cells = splitRow(line)
  return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell))
}

function alignmentOf(cell: string): string {
  const left = cell.startsWith(':')
  const right = cell.endsWith(':')
  if (left && right) return 'center'
  if (right) return 'right'
  if (left) return 'left'
  return ''
}

function alignClass(align: string): string {
  return align ? ` class="md-align-${align}"` : ''
}

/** 行内语法:行内代码优先,其次链接/粗体/删除线/斜体(输入已转义)。 */
function inline(text: string): string {
  const parts = text.split(/(`[^`]*`)/g)
  return parts
    .map((part) => {
      if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
        return `<code class="md-inline">${part.slice(1, -1)}</code>`
      }
      return part
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label: string, url: string) => {
          const raw = url.replace(/&amp;/g, '&')
          if (!/^(https?:\/\/|\/|#)/i.test(raw)) return label
          return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
        })
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/__([^_]+)__/g, '<strong>$1</strong>')
        .replace(/~~([^~]+)~~/g, '<del>$1</del>')
        .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    })
    .join('')
}

function startsBlock(line: string, next: string | undefined): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    HR.test(line) ||
    QUOTE.test(line) ||
    UL.test(line) ||
    OL.test(line) ||
    (line.includes('|') && next !== undefined && isTableSeparator(next))
  )
}

/** 仅行内渲染:用于放在 span/标签旁的短字段,不生成段落等块级标签。 */
export function renderSafeInlineMd(src: string): string {
  if (!src) return ''
  return inline(escapeHtml(src))
}

export function renderSafeMd(src: string): string {
  if (!src) return ''
  const lines = escapeHtml(src).split('\n')
  const out: string[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index += 1
      continue
    }

    // 围栏代码块:内容原样保留(已转义),不做其他 Markdown 处理
    if (FENCE.test(line)) {
      const buffer: string[] = []
      index += 1
      while (index < lines.length && !FENCE.test(lines[index])) {
        buffer.push(lines[index])
        index += 1
      }
      if (index < lines.length) index += 1 // 跳过闭合围栏
      out.push(`<pre class="md-code"><code>${buffer.join('\n')}</code></pre>`)
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      const level = heading[1].length
      out.push(`<h${level} class="md-heading">${inline(heading[2].trim())}</h${level}>`)
      index += 1
      continue
    }

    if (HR.test(line)) {
      out.push('<hr class="md-hr">')
      index += 1
      continue
    }

    // 表格:当前行含 | 且下一行是分隔行
    if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const header = splitRow(line)
      const aligns = splitRow(lines[index + 1]).map(alignmentOf)
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        rows.push(splitRow(lines[index]))
        index += 1
      }
      const head = header
        .map((cell, column) => `<th${alignClass(aligns[column])}>${inline(cell)}</th>`)
        .join('')
      const body = rows
        .map(
          (row) =>
            '<tr>' +
            header
              .map(
                (_cell, column) =>
                  `<td${alignClass(aligns[column])}>${inline(row[column] ?? '')}</td>`,
              )
              .join('') +
            '</tr>',
        )
        .join('')
      out.push(
        `<div class="md-table-wrap"><table class="md-table"><thead><tr>${head}</tr></thead>` +
          `<tbody>${body}</tbody></table></div>`,
      )
      continue
    }

    // 引用:连续 > 行合并为一个块
    if (QUOTE.test(line)) {
      const buffer: string[] = []
      while (index < lines.length && QUOTE.test(lines[index])) {
        buffer.push(QUOTE.exec(lines[index])![1])
        index += 1
      }
      out.push(`<blockquote class="md-quote">${inline(buffer.join('<br>'))}</blockquote>`)
      continue
    }

    // 列表:连续同类项;缩进的子项按同级展示(不生成嵌套结构)
    const ordered = !UL.test(line) && OL.test(line)
    const itemPattern = ordered ? OL : UL
    if (UL.test(line) || ordered) {
      const items: string[] = []
      while (index < lines.length) {
        const match = itemPattern.exec(lines[index])
        if (!match) break
        items.push(inline(match[1]))
        index += 1
      }
      const tag = ordered ? 'ol' : 'ul'
      out.push(
        `<${tag} class="md-list">${items.map((item) => `<li>${item}</li>`).join('')}</${tag}>`,
      )
      continue
    }

    // 段落:收集到空行或下一个块级结构
    const paragraph: string[] = []
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index], lines[index + 1])) {
      paragraph.push(inline(lines[index].trim()))
      index += 1
    }
    out.push(`<p class="md-p">${paragraph.join('<br>')}</p>`)
  }

  return out.join('')
}